"""阶段2-B5 接线测试：collect 来源选择、GitHub 合并、sources-check/github 子命令。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.normalize import normalize_github_item, normalize_official_item
from src.workflows import pipeline, run
from src.workflows.state import WorkflowState

REPO = Path(__file__).resolve().parents[1]
IMPORT_SAMPLE = REPO / "tests" / "fixtures" / "import_sample_masked.json"


def _gh_item():
    raw = {
        "full_name": "example-org/demo-repo",
        "name": "demo-repo",
        "owner": {"login": "example-org"},
        "html_url": "https://github.com/example-org/demo-repo",
        "description": "AI agent demo（假数据）",
        "stargazers_count": 1200,
        "pushed_at": "2026-10-30T10:00:00Z",
        "license": {"spdx_id": "MIT"},
        "topics": ["ai-agents"],
        "language": "Python",
    }
    return normalize_github_item(raw, {"full_name": "example-org/demo-repo",
                                       "topic": "AI agent"})


def _official_item():
    raw = {
        "id": "rel-1",
        "url": "https://github.com/example-org/demo-repo/releases/tag/v1.0",
        "title": "demo-repo v1.0（假数据）",
        "summary": "release notes",
        "published_at": "2026-10-30T10:00:00Z",
    }
    return normalize_official_item(raw, {"id": "demo_rel", "name": "示例 Releases",
                                         "type": "github_releases"})


def test_collect_selects_new_sources_and_merges_github_repo(tmp_path):
    def fake_github(**kw):
        return {"items": [_gh_item()], "api_calls": 7, "errors": []}

    def fake_official(**kw):
        return {"items": [_official_item()], "api_calls": 2, "errors": []}

    state = WorkflowState(tmp_path / "state.json")
    out = pipeline.step_collect(
        state, snapshot_dir=None, sources=["github", "official", "import"],
        import_file=IMPORT_SAMPLE, github_fetcher=fake_github,
        official_fetcher=fake_official,
        import_errors_path=tmp_path / "errors.jsonl")
    assert out["per_source"] == {"github": 1, "official": 1, "import": 2}
    assert out["external_calls"]["github"] == 7
    assert out["external_calls"]["official"] == 2
    # GitHub 连接器与官方 Releases 命中同一仓库 → 合并 sources，不丢弃数据
    assert len(out["materials"]) == 3
    assert len(out["dropped"]) == 1
    merged = next(m for m in out["materials"] if m["source_type"] == "github")
    assert {s["source_type"] for s in merged["sources"]} == {"github", "official"}
    assert out["materials"]
    assert all(m["source_type"] != "aihot" for m in out["materials"])


def test_collect_rejects_unknown_source(tmp_path):
    state = WorkflowState(tmp_path / "state.json")
    with pytest.raises(ValueError):
        pipeline.step_collect(state, sources=["unknown"], snapshot_dir=None)


def test_sources_check_writes_summary_and_cost(tmp_path, monkeypatch, capsys):
    out = {
        "materials": [],
        "accounts": [],
        "dropped": [{"item_id": "official:rel-1",
                     "duplicate_of": "github:example-org/demo-repo"}],
        "snapshot": None,
        "x_posts": 0,
        "per_source": {"aihot": 1, "github": 1, "official": 1, "x": 1,
                       "import": 1},
        "validation_error_counts": {"import": 1},
        "source_errors": {"github": ["示例错误"]},
        "external_calls": {"github": 9, "aihot": 1, "official": 5,
                           "x": 0, "import": 0},
        "sources": ["aihot", "github", "official", "x", "import"],
    }
    monkeypatch.setattr(run.pipeline, "step_collect", lambda *a, **kw: out)
    args = argparse.Namespace(state=str(tmp_path / "state.json"),
                              x_file="x.json", import_file="i.json",
                              github_top=5, official_limit=3, aihot_limit=3,
                              config="config/sources.example.yaml")
    assert run.cmd_sources_check(args) == 0
    files = list(tmp_path.glob("sources_check_*.json"))
    assert len(files) == 1
    payload = json.loads(files[0].read_text(encoding="utf-8"))
    assert payload["totals"]["fetched"] == 5
    assert payload["per_source"]["official"]["deduped"] == 1
    cost = [json.loads(line) for line in
            (tmp_path / "cost_log.jsonl").read_text(encoding="utf-8").splitlines()]
    assert cost[-1]["command"] == "sources-check"
    assert cost[-1]["provider"] == "无" and cost[-1]["cost"] == 0
    assert "合计" in capsys.readouterr().out


def test_github_command_writes_json_and_cost(tmp_path, monkeypatch, capsys):
    item = _gh_item()
    fake_out = {"items": [item], "api_calls": 4, "errors": [],
                "query_since": "2026-10-03", "per_theme": {"mcp": 1}}
    monkeypatch.setattr(run, "fetch_github", lambda **kw: fake_out)
    args = argparse.Namespace(state=str(tmp_path / "state.json"), top=1,
                              config=str(tmp_path / "missing.yaml"),
                              snapshots=str(tmp_path / "snap.json"))
    assert run.cmd_github(args) == 0
    files = list(tmp_path.glob("github_*.json"))
    assert len(files) == 1
    payload = json.loads(files[0].read_text(encoding="utf-8"))
    assert payload["items"][0]["source_type"] == "github"
    cost = [json.loads(line) for line in
            (tmp_path / "cost_log.jsonl").read_text(encoding="utf-8").splitlines()]
    assert cost[-1]["external_api_calls"]["github"] == 4
    assert cost[-1]["provider"] == "无" and cost[-1]["cost"] == 0
    assert "GitHub 请求 4 次" in capsys.readouterr().out
