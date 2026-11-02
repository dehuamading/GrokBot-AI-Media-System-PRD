"""成本日志：每次运行追加一行 JSONL 到 data/state/cost_log.jsonl（已 gitignore）。

字段：时间、命令、provider、model、调用次数、tokens、金额、外部 API 调用计数。
金额未知时写字符串“未知”，绝不用 0 冒充未知；接口明确返回 0 则保留 0。
同时写出中英文键名，便于人读与程序消费。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

UNKNOWN = "未知"
DEFAULT_COST_LOG = "data/state/cost_log.jsonl"


def _pick(record: dict, *keys, default=UNKNOWN):
    for key in keys:
        if key in record and record[key] is not None:
            return record[key]
    return default


def _normalize_cost(value):
    if value is None or value == "":
        return UNKNOWN
    return value


def append_cost(path: str | Path = DEFAULT_COST_LOG,
                record: dict | None = None, **fields) -> dict:
    """追加一条成本记录并返回实际写入的 dict。"""
    data = dict(record or {})
    data.update(fields)
    timestamp = _pick(data, "time", "时间",
                      default=datetime.now(timezone.utc).isoformat(timespec="seconds"))
    command = _pick(data, "command", "命令", default="")
    provider = _pick(data, "provider", default=UNKNOWN)
    model = _pick(data, "model", default=UNKNOWN)
    calls = _pick(data, "calls", "调用次数", default=0)
    prompt_tokens = _pick(data, "prompt_tokens", default=UNKNOWN)
    completion_tokens = _pick(data, "completion_tokens", default=UNKNOWN)
    tokens = _pick(data, "tokens", default=UNKNOWN)
    cost = _normalize_cost(_pick(data, "cost", "金额", "amount"))
    external = _pick(data, "external_api_calls", "外部API调用", "external", default={})
    note = _pick(data, "note", "备注", default="")

    out = {
        # 规范字段（英文，便于程序消费）
        "time": timestamp,
        "command": command,
        "provider": provider,
        "model": model,
        "calls": calls,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "tokens": tokens,
        "cost": cost,
        "external_api_calls": external,
        "note": note,
        # 中文列名（与任务书字段一一对应）
        "时间": timestamp,
        "命令": command,
        "调用次数": calls,
        "金额": cost,
        "外部API调用": external,
        "备注": note,
    }

    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(out, ensure_ascii=False) + "\n")
    return out
