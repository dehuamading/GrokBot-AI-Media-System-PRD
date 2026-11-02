"""GitHub 连接器测试（mock requests，无真实网络/无 token 泄漏）。"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.connectors import github_source
from src.connectors.github_source import compute_stars_growth, fetch_github


class FakeResponse:
    def __init__(self, status=200, payload=None, text=""):
        self.status_code = status
        self._payload = payload
        self.text = text

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")


def _repo(n, stars=1000, desc="AI agent tooling（假数据）", topics=None):
    return {
        "full_name": f"example-org/repo-{n}",
        "name": f"repo-{n}",
        "owner": {"login": "example-org"},
        "html_url": f"https://github.com/example-org/repo-{n}",
        "description": desc,
        "stargazers_count": stars,
        "pushed_at": "2026-10-29T08:00:00Z",
        "license": {"spdx_id": "MIT"},
        "topics": topics or ["mcp", "ai"],
        "language": "Python",
    }


def _install_fake(monkeypatch, *, calls=None, fail_theme=False, token_holder=None):
    calls = calls if calls is not None else []

    def fake_get(url, params=None, headers=None, timeout=None, **kw):
        calls.append({"url": url, "params": params, "headers": headers})
        if token_holder is not None and headers:
            token_holder.append(headers.get("Authorization"))
        if fail_theme and url.endswith("/search/repositories"):
            raise RuntimeError("search boom")
        if url.endswith("/search/repositories"):
            q = (params or {}).get("q", "")
            if "mcp" in q:
                repos = [_repo(1, 5000, "MCP server kit（假数据）", ["mcp"])]
            elif "agent" in q:
                repos = [_repo(2, 3000, "AI agent workflow（假数据）", ["ai-agents"])]
            else:
                repos = [_repo(3, 2000), _repo(4, 1500), _repo(5, 1200)]
            return FakeResponse(payload={"items": repos})
        if url.endswith("/releases/latest"):
            repo = url.split("/repos/")[1].split("/releases")[0]
            if repo.endswith("repo-4"):
                return FakeResponse(status=404, payload={"message": "Not Found"})
            return FakeResponse(payload={
                "tag_name": "v1.2.0",
                "published_at": "2026-10-28T00:00:00Z",
                "html_url": f"https://github.com/{repo}/releases/tag/v1.2.0",
            })
        if url.endswith("/readme"):
            return FakeResponse(text="## Install\n\n```bash\npip install demo\n```")
        return FakeResponse(status=404, payload={})

    monkeypatch.setattr(github_source.requests, "get", fake_get)
    return calls


def test_fetch_fields_complete_and_unknowns_marked(tmp_path, monkeypatch):
    _install_fake(monkeypatch)
    out = fetch_github(top=2, snapshots_path=tmp_path / "snap.json",
                       token="ghp_TESTTOKEN123", include_readme=True)
    assert out["api_calls"] > 0
    assert len(out["items"]) == 2
    it = out["items"][0]
    gh = it["extra"]["github"]
    assert it["platform"] == "github"
    assert it["origin_agent"] == "pi"
    assert it["source_type"] == "github"
    assert it["metrics"]["stars"] == 5000
    assert it["license_status"] == "公开+许可证:MIT"
    assert it["source_url"].startswith("https://github.com/example-org/")
    for key in ("name", "url", "purpose", "stars", "last_update",
                "latest_release", "license", "install_difficulty",
                "needs_paid_api", "windows_support", "china_usability",
                "target_users", "worth_video"):
        assert key in gh
    assert gh["latest_release"].startswith("v1.2.0")
    assert "推测" in gh["install_difficulty"]
    assert gh["windows_support"] == "待验证"
    assert gh["china_usability"] == "待验证"
    assert gh["target_users"]
    assert "推测" in gh["worth_video"]


def test_release_metadata_does_not_overwrite_repo_url(tmp_path, monkeypatch):
    _install_fake(monkeypatch)
    out = fetch_github(top=1, snapshots_path=tmp_path / "snap.json",
                       include_readme=False)
    gh = out["items"][0]["extra"]["github"]
    assert gh["url"] == "https://github.com/example-org/repo-1"
    assert gh["release_url"] == (
        "https://github.com/example-org/repo-1/releases/tag/v1.2.0")
    assert gh["release_tag"] == "v1.2.0"
    assert gh["release_published_at"] == "2026-10-28T00:00:00Z"


def test_no_previous_snapshot_does_not_count_growth(tmp_path, monkeypatch):
    _install_fake(monkeypatch)
    snap = tmp_path / "snap.json"
    out = fetch_github(top=1, snapshots_path=snap)
    gh = out["items"][0]["extra"]["github"]
    assert gh["stars_delta"] == "无历史快照，不计增长"
    assert gh["stars_delta_days"] is None
    saved = json.loads(snap.read_text(encoding="utf-8"))
    assert "example-org/repo-1" in saved["repos"]
    assert saved["repos"]["example-org/repo-1"][-1]["stars"] == 5000


def test_previous_snapshot_counts_growth_and_days():
    snapshots = {"repos": {"o/r": [
        {"collected_at": "2026-10-20T00:00:00+00:00", "stars": 900},
    ]}}
    now = datetime(2026, 10, 30, 0, 0, tzinfo=timezone.utc)
    growth = compute_stars_growth("o/r", 1100, snapshots, now=now)
    assert growth["stars_delta"] == 200
    assert growth["stars_delta_days"] == 10
    assert "无历史" not in growth["stars_growth_note"]


def test_top_n_is_configurable(tmp_path, monkeypatch):
    _install_fake(monkeypatch)
    out = fetch_github(top=3, snapshots_path=tmp_path / "s.json",
                       include_readme=False)
    assert len(out["items"]) == 3


def test_token_never_appears_in_errors_or_result(tmp_path, monkeypatch):
    token = "ghp_SUPERSECRETVALUE123456789"
    seen = []
    _install_fake(monkeypatch, fail_theme=True, token_holder=seen)
    out = fetch_github(top=2, snapshots_path=tmp_path / "s.json", token=token)
    blob = json.dumps(out, ensure_ascii=False)
    assert token not in blob
    assert out["errors"]
    assert seen and all(a == f"Bearer {token}" for a in seen)


def test_unknown_paid_api_is_pending_not_invented(tmp_path, monkeypatch):
    _install_fake(monkeypatch)
    out = fetch_github(top=1, snapshots_path=tmp_path / "s.json",
                       include_readme=False)
    gh = out["items"][0]["extra"]["github"]
    assert gh["needs_paid_api"] in {"是（推测）", "否（推测）", "待验证"}


def test_theme_queries_run_independently_and_merge_by_full_name(tmp_path, monkeypatch):
    """queries 列表：每条独立搜索，同一 full_name 合并去重。"""
    calls = []

    def fake_get(url, params=None, headers=None, timeout=None, **kw):
        q = (params or {}).get("q", "")
        calls.append(q)
        if url.endswith("/search/repositories"):
            if "topic:ai-coding" in q:
                return FakeResponse(payload={"items": [
                    _repo(1, 5000, "coding topic（假数据）", ["ai-coding"]),
                    _repo(2, 3000, "coding topic two（假数据）", ["ai-coding"]),
                ]})
            if '"AI coding"' in q:
                return FakeResponse(payload={"items": [
                    _repo(1, 5000, "coding topic（假数据）", ["ai-coding"]),
                    _repo(6, 2000, "coding description（假数据）", ["ai"]),
                ]})
            return FakeResponse(payload={"items": []})
        if url.endswith("/releases/latest"):
            return FakeResponse(status=404, payload={"message": "Not Found"})
        if url.endswith("/readme"):
            return FakeResponse(text="")
        return FakeResponse(status=404, payload={})

    monkeypatch.setattr(github_source.requests, "get", fake_get)
    themes = [{"id": "ai_coding", "label": "AI coding", "queries": [
        "topic:ai-coding", '"AI coding" in:description']}]
    out = fetch_github(top=5, themes=themes,
                       snapshots_path=tmp_path / "s.json", include_readme=False)
    search_calls = [q for q in calls if q]
    assert len(search_calls) == 2
    assert out["per_theme"]["ai_coding"] == 3
    names = [it["extra"]["github"]["full_name"] for it in out["items"]]
    assert len(names) == len(set(names))
    assert len(names) == 3


def test_theme_missing_id_does_not_raise_keyerror(tmp_path, monkeypatch):
    def fake_get(url, params=None, headers=None, timeout=None, **kw):
        if url.endswith("/search/repositories"):
            raise RuntimeError("search boom")
        return FakeResponse(status=404, payload={})

    monkeypatch.setattr(github_source.requests, "get", fake_get)
    out = fetch_github(top=1, themes=[{"label": "无ID主题",
                                       "queries": ["topic:x"]}],
                       snapshots_path=tmp_path / "s.json", include_readme=False)
    assert out["per_theme"]["无ID主题"] == 0
    assert "无ID主题" in out["errors"][0]


def test_missing_latest_release_404_is_normal_not_error(tmp_path, monkeypatch):
    _install_fake(monkeypatch)
    out = fetch_github(top=5, snapshots_path=tmp_path / "s.json",
                       include_readme=False)
    repo4 = next(it for it in out["items"]
                 if it["extra"]["github"]["full_name"] == "example-org/repo-4")
    assert repo4["extra"]["github"]["latest_release"] == "无"
    assert not any("repo-4" in err for err in out["errors"])


def test_load_config_supports_queries_and_legacy_query(tmp_path):
    path = tmp_path / "sources.yaml"
    path.write_text(
        "github:\n"
        "  themes:\n"
        "    - id: legacy\n"
        "      label: Legacy\n"
        "      query: topic:legacy\n"
        "    - id: legacy_or\n"
        "      label: Legacy OR\n"
        "      query: 'topic:a OR topic:b'\n"
        "    - id: split\n"
        "      label: Split\n"
        "      queries: ['topic:a', '\"b\" in:description']\n",
        encoding="utf-8")
    cfg = github_source.load_config(path)
    by_id = {t["id"]: t for t in cfg["themes"]}
    assert by_id["legacy"]["queries"] == ["topic:legacy"]
    assert by_id["legacy_or"]["queries"] == ["topic:a", "topic:b"]
    assert by_id["split"]["queries"] == ["topic:a", '"b" in:description']
    example = github_source.load_config("config/sources.example.yaml")
    assert not any(" OR " in query for theme in example["themes"]
                   for query in theme["queries"])


def test_existing_snapshot_yields_numeric_stars_delta(tmp_path, monkeypatch):
    _install_fake(monkeypatch)
    snap = tmp_path / "s.json"
    snap.write_text(json.dumps({"repos": {"example-org/repo-1": [{
        "collected_at": "2026-10-20T00:00:00+00:00", "stars": 4000}]}}),
        encoding="utf-8")
    now = datetime(2026, 10, 30, 0, 0, tzinfo=timezone.utc)
    out = fetch_github(top=1, snapshots_path=snap, include_readme=False, now=now)
    gh = out["items"][0]["extra"]["github"]
    assert gh["stars_delta"] == 1000
    assert gh["stars_delta_days"] == 10
