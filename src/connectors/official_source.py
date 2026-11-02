"""官方源连接器（阶段2-B2）：RSS/Atom + GitHub Releases，单源失败隔离。

来源 URL 一律放在 config/sources.example.yaml 的 official_sources 段，不写死在 src。
每源默认最近 3 条（可配）；只用标准库 xml.etree 解析 RSS/Atom。
"""

from __future__ import annotations

import html
import os
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

import requests
import yaml

from src.normalize import normalize_official_item

API_BASE = "https://api.github.com"
UA = "GrokBot-AI-Media-System/0.2 (+manual collection)"
ATOM_NS = {"a": "http://www.w3.org/2005/Atom"}


def load_sources(path: str | Path) -> list[dict]:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"来源配置文件不存在：{p}")
    raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    sources = raw.get("official_sources") or []
    if not isinstance(sources, list):
        raise ValueError("official_sources 应为数组")
    return [s for s in sources if isinstance(s, dict) and s.get("enabled", True)]


def _strip_html(text: str) -> str:
    text = html.unescape(text or "")
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _iso(value: str) -> str:
    text = (value or "").strip()
    if not text:
        return ""
    try:
        return parsedate_to_datetime(text).isoformat(timespec="seconds")
    except (TypeError, ValueError, OverflowError):
        pass
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).isoformat(
            timespec="seconds")
    except ValueError:
        return text


def _text(node, path: str, namespaces: dict | None = None) -> str:
    found = node.find(path, namespaces or {})
    if found is None or found.text is None:
        return ""
    return _strip_html(found.text)


def _parse_rss(xml_text: str) -> list[dict]:
    root = ET.fromstring(xml_text)
    entries = []
    if root.tag.endswith("feed"):
        for entry in root.findall("a:entry", ATOM_NS):
            title = _text(entry, "a:title", ATOM_NS)
            link = ""
            for candidate in entry.findall("a:link", ATOM_NS):
                if candidate.get("rel") in (None, "alternate"):
                    link = candidate.get("href") or ""
                    if link:
                        break
            summary = (_text(entry, "a:summary", ATOM_NS)
                       or _text(entry, "a:content", ATOM_NS))
            published = (_text(entry, "a:published", ATOM_NS)
                         or _text(entry, "a:updated", ATOM_NS))
            entries.append({"title": title, "url": link, "summary": summary,
                            "published_at": _iso(published),
                            "id": entry.findtext("a:id", default="", namespaces=ATOM_NS)})
    else:
        for item in root.findall("./channel/item"):
            entries.append({
                "title": _text(item, "title"),
                "url": (_text(item, "link") or _text(item, "guid")),
                "summary": (_text(item, "description") or _text(item, "summary")),
                "published_at": _iso(_text(item, "pubDate")),
                "id": _text(item, "guid"),
            })
    return entries


def _fetch_rss(source: dict, limit: int,
               timeout: int) -> tuple[list[dict], int, list[str]]:
    url = source.get("url")
    if not url:
        raise ValueError("rss 源缺少 url")
    resp = requests.get(url, headers={"User-Agent": UA}, timeout=timeout)
    resp.raise_for_status()
    # requests 对无 charset 的响应默认 ISO-8859-1，会把中文解成乱码。
    encoding = getattr(resp, "encoding", None)
    if not encoding or str(encoding).lower() in {"iso-8859-1", "latin-1", "latin1"}:
        resp.encoding = getattr(resp, "apparent_encoding", None) or "utf-8"
    entries = _parse_rss(resp.text)[:limit]
    warnings = [] if entries else ["解析到0条"]
    items = []
    for entry in entries:
        entry.setdefault("extra", {})
        entry["extra"].update({"feed_type": "rss"})
        items.append(normalize_official_item(entry, source))
    return items, 1, warnings


def _fetch_github_releases(source: dict, limit: int, timeout: int,
                           token: str | None) -> tuple[list[dict], int]:
    owner = source.get("owner")
    repo = source.get("repo")
    if not owner or not repo:
        raise ValueError("github_releases 源缺少 owner/repo")
    headers = {"Accept": "application/vnd.github+json", "User-Agent": UA,
               "X-GitHub-Api-Version": "2022-11-28"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    url = f"{API_BASE}/repos/{owner}/{repo}/releases"
    resp = requests.get(url, params={"per_page": limit}, headers=headers,
                        timeout=timeout)
    resp.raise_for_status()
    releases = resp.json()
    if not isinstance(releases, list):
        raise ValueError("GitHub releases 返回格式异常")
    items = []
    for rel in releases[:limit]:
        tag = rel.get("tag_name") or rel.get("name") or ""
        title = f"{repo} {tag}".strip()
        body = _strip_html(rel.get("body") or "")
        raw = {
            "id": f"{owner}/{repo}:{rel.get('id') or tag}",
            "title": title,
            "url": rel.get("html_url") or "",
            "summary": body[:600],
            "published_at": _iso(rel.get("published_at") or ""),
            "extra": {"tag": tag, "release_name": rel.get("name"),
                      "published_at": rel.get("published_at")},
        }
        items.append(normalize_official_item(raw, source))
    return items, 1


def fetch_official(sources: list[dict] | None = None, *,
                   config_path: str | Path | None = None,
                   default_limit: int | None = None, timeout: int = 20,
                   token: str | None = None) -> dict:
    """抓取官方源；单源超时/失败只记 errors，不影响其他源。

    default_limit 显式传入时覆盖每个源配置里的 limit（CLI --official-limit
    必须真正生效）。未传入时才使用源自身配置，再回退 3。
    """
    if sources is None:
        sources = load_sources(config_path) if config_path else []
    token = token if token is not None else os.environ.get("GITHUB_TOKEN")
    items: list[dict] = []
    errors: list[dict] = []
    per_source: dict[str, dict] = {}
    api_calls = 0

    for source in sources:
        sid = str(source.get("id") or source.get("name") or "unknown")
        if default_limit is not None:
            limit = max(0, int(default_limit))
        else:
            limit = int(source.get("limit") or 3)
        try:
            kind = source.get("type")
            warnings: list[str] = []
            if kind == "rss":
                got, calls, warnings = _fetch_rss(source, limit, timeout)
            elif kind == "github_releases":
                got, calls = _fetch_github_releases(source, limit, timeout, token)
            else:
                raise ValueError(f"未知官方源类型：{kind}")
            items.extend(got)
            api_calls += calls
            for warning in warnings:
                errors.append({"source": source.get("name") or sid,
                               "source_id": sid, "error": warning})
            per_source[sid] = {"items": len(got), "errors": len(warnings),
                               "name": source.get("name")}
        except Exception as exc:  # noqa: BLE001 - 单源失败隔离
            name = source.get("name") or sid
            errors.append({"source": name, "source_id": sid,
                           "error": f"{type(exc).__name__}: {exc}"})
            per_source[sid] = {"items": 0, "errors": 1, "name": name}

    return {"items": items, "errors": errors, "per_source": per_source,
            "api_calls": api_calls, "fetched_at":
            datetime.now(timezone.utc).isoformat(timespec="seconds")}
