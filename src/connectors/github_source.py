"""GitHub 连接器（阶段2-B1）：官方 REST 只读、默认 top 5、可配置主题。

红线：
- token 只从环境变量 GITHUB_TOKEN 读取（可无 token 降级），绝不打印/写日志；
- 只用公开元数据（search/repositories、releases/latest、可选 README API），不克隆仓库；
- Stars 增长只与本仓库历史快照比较；没有快照就写“无历史快照，不计增长”，
  绝不把 Trending 当作增长。
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
import yaml

from src.normalize import normalize_github_item

API_BASE = "https://api.github.com"
UA = "GrokBot-AI-Media-System/0.2 (+manual collection)"
DEFAULT_SNAPSHOTS = "data/state/github_snapshots.json"

# 主题与查询可从 config/sources.example.yaml 覆盖；这里只放默认值（不含外部 URL）。
DEFAULT_THEMES = [
    {"id": "mcp", "label": "MCP", "query": "topic:mcp"},
    {"id": "ai_agent", "label": "AI agent", "query": "topic:ai-agents"},
    {"id": "ai_coding", "label": "AI coding tools",
     "query": 'topic:ai-coding OR "AI coding" in:description'},
    {"id": "ai_automation", "label": "AI automation",
     "query": 'topic:automation AI in:description'},
    {"id": "ai_video", "label": "AI video generation",
     "query": 'topic:ai-video OR "video generation" in:description'},
    {"id": "ai_app_dev", "label": "AI app dev",
     "query": 'topic:vibe-coding OR "app builder" in:description'},
    {"id": "side_hustle", "label": "side-hustle tools（副业工具）",
     "query": 'topic:side-hustle OR "side project" in:description'},
    {"id": "beginner", "label": "beginner-usable open source",
     "query": 'topic:no-code OR "beginner friendly" in:description'},
]

PAID_KEYWORDS = ["paid", "api key", "openai api", "anthropic api", "subscription",
                 "credits", "billing", "付费", "订阅", "收费", "充值", "按量计费"]
FREE_KEYWORDS = ["free", "open source", "open-source", "self-host", "self host",
                 "local-first", "免费", "开源", "本地运行"]
CONTENT_KEYWORDS = ["video", "image", "audio", "writing", "creator", "content",
                    "短视频", "视频", "剪辑", "配音", "数字人", "写作", "绘画",
                    "文案", "自媒体"]
CODING_KEYWORDS = ["code", "coding", "developer", "sdk", "api", "mcp", "agent",
                   "framework", "ide", "编程", "开发", "小程序", "网站"]
SIDE_KEYWORDS = ["side hustle", "side-hustle", "business", "monetization",
                 "startup", "earn", "副业", "赚钱", "变现", "接单", "创业"]
DOMESTIC_KEYWORDS = ["china", "chinese", "domestic", "self-host", "self host",
                     "local", "offline", "国内", "中文", "本地", "离线"]


def load_config(path: str | Path | None = None) -> dict:
    """读取 config 中的 github 段；缺失时回退默认主题（不联网）。"""
    data: dict = {}
    if path:
        p = Path(path)
        if p.exists():
            raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
            data = raw.get("github") or {}
    themes = data.get("themes") or DEFAULT_THEMES
    normalized = []
    for t in themes:
        if isinstance(t, dict) and t.get("query"):
            normalized.append({
                "id": t.get("id") or t.get("label") or "theme",
                "label": t.get("label") or t.get("id") or "theme",
                "query": str(t["query"]),
            })
    return {
        "top": data.get("top"),
        "since_days": int(data.get("since_days") or 30),
        "themes": normalized or DEFAULT_THEMES,
    }


def _headers(token: str | None, accept: str = "application/vnd.github+json") -> dict:
    headers = {"Accept": accept, "User-Agent": UA,
               "X-GitHub-Api-Version": "2022-11-28"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _request_json(url: str, *, params=None, token=None, timeout=20):
    resp = requests.get(url, params=params, headers=_headers(token), timeout=timeout)
    resp.raise_for_status()
    return resp.json()


def _request_text(url: str, *, token=None, timeout=20) -> str:
    resp = requests.get(url, headers=_headers(token, "application/vnd.github.raw"),
                        timeout=timeout)
    resp.raise_for_status()
    return resp.text or ""


def _theme_query(theme: dict, since: str) -> str:
    query = theme["query"]
    if "pushed:" not in query:
        query = f"{query} pushed:>{since}"
    return query


def _round_robin(candidates: list[tuple[dict, list[dict]]], top: int):
    """主题间轮询，保证多样性；同一仓库只出现一次。"""
    selected = []
    seen: set[str] = set()
    queues = [(theme, list(repos)) for theme, repos in candidates]
    while len(selected) < top:
        progressed = False
        for theme, repos in queues:
            while repos:
                repo = repos.pop(0)
                full_name = str(repo.get("full_name") or "")
                if not full_name or full_name in seen:
                    continue
                seen.add(full_name)
                selected.append((theme, repo))
                progressed = True
                break
            if len(selected) >= top:
                break
        if not progressed:
            break
    return selected


def infer_target_users(text: str) -> list[str]:
    lowered = (text or "").lower()
    users = []
    if any(k in lowered for k in CONTENT_KEYWORDS):
        users.append("AI内容创作者")
    if any(k in lowered for k in CODING_KEYWORDS):
        users.append("AI编程与应用开发者（含零基础）")
    if any(k in lowered for k in SIDE_KEYWORDS):
        users.append("AI副业与创业探索者")
    if any(k in lowered for k in DOMESTIC_KEYWORDS):
        users.append("国外AI工具的国内使用者")
    if not users:
        users.append("AI普通使用者")
    return users


def infer_install_difficulty(text: str, has_readme: bool) -> str:
    lowered = (text or "").lower()
    if any(k in lowered for k in ("pip install", "npm install", "npx ", "docker run",
                                  "docker compose", "brew install", "uvx ")):
        return "简单（推测）"
    if "install" in lowered or "docker" in lowered or "setup" in lowered:
        return "中等（推测）"
    return "较难（推测）" if has_readme else "待验证（推测）"


def infer_needs_paid_api(text: str) -> str:
    lowered = (text or "").lower()
    if any(k in lowered for k in PAID_KEYWORDS):
        return "是（推测）"
    if any(k in lowered for k in FREE_KEYWORDS):
        return "否（推测）"
    return "待验证"


def infer_worth_video(stars, target_users: list[str]) -> str:
    try:
        star_count = float(stars)
    except (TypeError, ValueError):
        star_count = 0
    if star_count >= 1000:
        return f"是（推测）：stars={int(star_count)}，且有明确受众；仍需人工验证"
    if target_users and target_users != ["AI普通使用者"]:
        return "是（推测）：受众匹配，但 stars 较低；仍需人工验证"
    return "否（推测）：缺少明确受众或热度证据；待人工复核"


def load_snapshots(path: str | Path = DEFAULT_SNAPSHOTS) -> dict:
    p = Path(path)
    if not p.exists():
        return {"repos": {}}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"repos": {}}
    if not isinstance(data, dict) or not isinstance(data.get("repos"), dict):
        return {"repos": {}}
    return data


def save_snapshots(path: str | Path, data: dict) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def compute_stars_growth(full_name: str, stars, snapshots: dict,
                         now: datetime | None = None) -> dict:
    """只有存在本仓库历史快照时才计算增长；否则显式说明不计增长。"""
    history = ((snapshots or {}).get("repos") or {}).get(full_name) or []
    if not history:
        return {"stars_delta": "无历史快照，不计增长",
                "stars_delta_days": None,
                "stars_growth_note": "首次采集，不把 Trending 当增长"}
    previous = history[-1]
    previous_stars = previous.get("stars")
    if not isinstance(previous_stars, (int, float)) or not isinstance(stars, (int, float)):
        return {"stars_delta": "历史快照缺少 stars，不计增长",
                "stars_delta_days": None,
                "stars_growth_note": "历史快照字段不完整"}
    try:
        prev_at = datetime.fromisoformat(
            str(previous.get("collected_at") or "").replace("Z", "+00:00"))
    except ValueError:
        return {"stars_delta": "历史快照时间无法解析，不计增长",
                "stars_delta_days": None,
                "stars_growth_note": "历史快照字段不完整"}
    current = now or datetime.now(timezone.utc)
    if prev_at.tzinfo is None:
        prev_at = prev_at.replace(tzinfo=timezone.utc)
    days = max(0, (current - prev_at).days)
    return {"stars_delta": int(stars) - int(previous_stars),
            "stars_delta_days": days,
            "stars_growth_note": f"与 {previous.get('collected_at')} 快照比较"}


def fetch_github(top: int = 5, themes: list[dict] | None = None, *,
                 token: str | None = None, timeout: int = 20,
                 since_days: int = 30,
                 snapshots_path: str | Path = DEFAULT_SNAPSHOTS,
                 include_readme: bool = True,
                 now: datetime | None = None) -> dict:
    """检索主题仓库 → 轮询取前 N → 补 latest release/README → 写快照。

    返回 {items, api_calls, errors, per_theme, query_since, snapshots_path}。
    单主题/单仓库失败只记录 errors，不中断其他请求。
    """
    if top < 1:
        return {"items": [], "api_calls": 0, "errors": ["top 必须 ≥ 1"],
                "per_theme": {}, "query_since": "", "snapshots_path": str(snapshots_path)}
    token = token if token is not None else os.environ.get("GITHUB_TOKEN")
    themes = themes or DEFAULT_THEMES
    now = now or datetime.now(timezone.utc)
    since = (now - timedelta(days=since_days)).date().isoformat()
    errors: list[str] = []
    api_calls = 0
    candidates: list[tuple[dict, list[dict]]] = []
    per_theme: dict[str, int] = {}

    for theme in themes:
        query = _theme_query(theme, since)
        try:
            payload = _request_json(
                f"{API_BASE}/search/repositories",
                params={"q": query, "sort": "stars", "order": "desc",
                        "per_page": max(top, 5)},
                token=token, timeout=timeout)
            api_calls += 1
            repos = payload.get("items") if isinstance(payload, dict) else []
            repos = [r for r in (repos or []) if isinstance(r, dict)]
            per_theme[theme["id"]] = len(repos)
            candidates.append((theme, repos))
        except Exception as exc:  # noqa: BLE001 - 单主题失败不影响其他主题
            errors.append(f"主题 {theme.get('label') or theme.get('id')} 检索失败："
                          f"{type(exc).__name__}: {exc}")
            per_theme[theme["id"]] = 0

    selected = _round_robin(candidates, top)
    snapshots = load_snapshots(snapshots_path)
    snapshot_repos = snapshots.setdefault("repos", {})
    items: list[dict] = []

    for theme, repo in selected:
        full_name = str(repo.get("full_name") or "")
        latest_release = "无"
        release_extra: dict = {}
        try:
            release = _request_json(
                f"{API_BASE}/repos/{full_name}/releases/latest",
                token=token, timeout=timeout)
            api_calls += 1
            tag = release.get("tag_name") or release.get("name") or ""
            published = (release.get("published_at") or "")[:10]
            if tag:
                latest_release = f"{tag}（{published or '日期未知'}）"
            release_extra = {
                "tag": tag,
                "published_at": release.get("published_at"),
                "url": release.get("html_url"),
            }
        except Exception as exc:  # noqa: BLE001 - 无 release 是正常情况
            api_calls += 1
            if "404" not in str(exc):
                errors.append(f"{full_name} latest release 获取失败："
                              f"{type(exc).__name__}: {exc}")

        readme_text = ""
        if include_readme:
            try:
                readme_text = _request_text(
                    f"{API_BASE}/repos/{full_name}/readme",
                    token=token, timeout=timeout)
                api_calls += 1
            except Exception:  # noqa: BLE001 - README 仅用于启发式，可缺
                api_calls += 1

        description = repo.get("description") or ""
        topics = [str(t) for t in (repo.get("topics") or []) if t]
        heuristic_text = " ".join([description, " ".join(topics), readme_text[:4000]])
        target_users = infer_target_users(heuristic_text)
        stars = repo.get("stargazers_count") or 0
        growth = compute_stars_growth(full_name, stars, snapshots, now=now)
        github_extra = {
            "purpose": description or "待验证",
            "latest_release": latest_release,
            "install_difficulty": infer_install_difficulty(
                heuristic_text, bool(readme_text)),
            "needs_paid_api": infer_needs_paid_api(heuristic_text),
            "windows_support": "待验证",
            "china_usability": "待验证",
            "target_users": target_users,
            "worth_video": infer_worth_video(stars, target_users),
            "topic": theme.get("label"),
            "topic_id": theme.get("id"),
            **growth,
            **release_extra,
        }
        items.append(normalize_github_item(repo, github_extra))

        snapshot_repos.setdefault(full_name, []).append({
            "collected_at": now.isoformat(timespec="seconds"),
            "stars": int(stars),
        })

    save_snapshots(snapshots_path, snapshots)
    return {"items": items, "api_calls": api_calls, "errors": errors,
            "per_theme": per_theme, "query_since": since,
            "snapshots_path": str(snapshots_path)}
