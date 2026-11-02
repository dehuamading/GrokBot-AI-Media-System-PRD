"""成本日志测试：未知金额必须写“未知”，绝不能用 0 冒充未知。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.cost_log import UNKNOWN, append_cost


def _read(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_unknown_cost_is_not_zero(tmp_path):
    path = tmp_path / "nested" / "cost_log.jsonl"
    rec = append_cost(path, {
        "command": "collect --analyze llm",
        "provider": "opencode-go",
        "model": "deepseek-v4.1-flash",
        "calls": 2,
        "prompt_tokens": None,
        "completion_tokens": None,
        "tokens": None,
        "cost": None,
        "external_api_calls": {"github": 0, "aihot": 1, "official": 0},
    })

    assert rec["cost"] == UNKNOWN
    assert rec["金额"] == UNKNOWN
    assert rec["cost"] != 0
    line = _read(path)[0]
    assert line["cost"] == UNKNOWN and line["金额"] == UNKNOWN
    assert line["external_api_calls"]["aihot"] == 1


def test_explicit_zero_cost_is_preserved(tmp_path):
    path = tmp_path / "cost_log.jsonl"
    rec = append_cost(path, {"command": "collect", "cost": 0})
    assert rec["cost"] == 0
    assert _read(path)[0]["cost"] == 0


def test_append_multiple_records_and_default_fields(tmp_path):
    path = tmp_path / "cost_log.jsonl"
    append_cost(path, {"command": "a", "provider": "p", "model": "m", "cost": 0.1})
    append_cost(path, {"command": "b", "cost": "未知"})
    rows = _read(path)
    assert len(rows) == 2
    assert rows[0]["command"] == "a" and rows[0]["cost"] == 0.1
    assert rows[1]["command"] == "b" and rows[1]["cost"] == "未知"
    assert rows[0]["time"] and rows[0]["时间"] == rows[0]["time"]
    assert rows[1]["provider"] == UNKNOWN
