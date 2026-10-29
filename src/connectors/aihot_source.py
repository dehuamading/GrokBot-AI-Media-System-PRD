"""连接器层（PRD 9.1）：aihot.news /api/v1 读取适配器。

站点公开声明（llms.txt）：全部匿名只读、无需 API Key；robots.txt 放行 /api/v1/。
注意：旧 /api/public/* 已于 2026-10-31 停服，必须使用 /api/v1/*。
"""

from __future__ import annotations

import sys
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from src.normalize import normalize_aihot_item  # noqa: E402

DEFAULT_BASE = "https://aihot.news"
UA = "GrokBot-AI-Media-System/0.1 (+public repo: dehuamading/GrokBot-AI-Media-System-PRD)"


def fetch_selected(base_url: str = DEFAULT_BASE, limit: int = 20,
                   window: str = "7d", timeout: int = 30) -> list[dict]:
    """拉取精选条目并归一化。limit 上限遵守任务02约束（≤20）。"""
    if limit > 20:
        raise ValueError("任务02 约束：单轮拉取 ≤20 条（P1 小规模样例）")
    url = f"{base_url}/api/v1/items"
    resp = requests.get(
        url,
        params={"mode": "selected", "limit": limit, "window": window},
        headers={"User-Agent": UA},
        timeout=timeout,
    )
    resp.raise_for_status()
    payload = resp.json()
    items = payload.get("items") or []
    return [normalize_aihot_item(raw) for raw in items]


def load_from_file(path: str | Path) -> list[dict]:
    """从已保存的 /api/v1 响应 JSON 文件加载（离线重放/断点恢复用）。"""
    import json

    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    items = payload.get("items") or payload
    return [normalize_aihot_item(raw) for raw in items]
