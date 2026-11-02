"""成本汇总与成本日志测试：cost_basis 必须随金额一起进入日志。"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.cost_log import UNKNOWN
from src.workflows.run import _aggregate_usage, _log_collect_cost

OPENCODE_BASIS = "opencode-go套餐额度折算(美元,非实付)"


class FakeChat:
    def __init__(self, usage_log):
        self.usage_log = usage_log
        self.calls = len(usage_log)
        self.last_provider = "opencode-go"
        self.last_model = "deepseek-v4.1-flash"


def test_aggregate_usage_keeps_cost_basis():
    chat = FakeChat([{
        "provider": "opencode-go",
        "model": "deepseek-v4.1-flash",
        "prompt_tokens": 10,
        "completion_tokens": 5,
        "cost": 0.0012,
        "cost_basis": OPENCODE_BASIS,
    }])
    agg = _aggregate_usage(chat)
    assert agg["cost"] == 0.0012
    assert agg["cost_basis"] == OPENCODE_BASIS


def test_aggregate_usage_without_usage_marks_cost_basis_unknown():
    agg = _aggregate_usage(FakeChat([]))
    assert agg["cost"] == UNKNOWN
    assert agg["cost_basis"] == UNKNOWN


def test_dry_run_with_live_still_writes_cost_log(tmp_path, monkeypatch):
    from src.workflows import run

    out = {"materials": [], "dropped": [], "x_posts": 0, "accounts": [],
           "per_source": {}, "source_errors": {},
           "external_calls": {"github": 0, "aihot": 1, "official": 0,
                              "x": 0, "import": 0},
           "validation_error_counts": {}}
    monkeypatch.setattr(run.pipeline, "step_collect", lambda *a, **kw: out)
    monkeypatch.setattr(run.pipeline, "step_analyze",
                        lambda *a, **kw: {"cards": [], "details": []})
    args = SimpleNamespace(
        state=str(tmp_path / "state.json"), dry_run=True, live=True,
        audience=None, aihot_file=None, x_following=None, x_posts=None,
        sources="aihot", github_top=5, official_limit=3, import_file=None,
        config=None, analyze="heuristic", shortlist=60, top_k=8,
        page_id="root")
    assert run.cmd_collect(args) == 0
    assert (tmp_path / "cost_log.jsonl").exists()


def test_collect_cost_log_writes_cost_basis(tmp_path):
    chat = FakeChat([{
        "provider": "opencode-go",
        "model": "deepseek-v4.1-flash",
        "prompt_tokens": 10,
        "completion_tokens": 5,
        "cost": 0.0012,
        "cost_basis": OPENCODE_BASIS,
    }])
    args = SimpleNamespace(state=str(tmp_path / "state.json"),
                           sources="github", live=False)

    _log_collect_cost(args, {}, chat, "llm")

    line = json.loads((tmp_path / "cost_log.jsonl").read_text(encoding="utf-8"))
    assert line["cost"] == 0.0012
    assert line["cost_basis"] == OPENCODE_BASIS
    assert line["成本口径"] == OPENCODE_BASIS
