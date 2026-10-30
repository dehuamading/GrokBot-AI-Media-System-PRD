"""连接器层（任务05）：X 帖子文件读取适配器（降级模式，纯本地）。

任务05 约束：用户侧工具（Grok Bot）已把「关注的账号近 24h 帖子」导出为本地 JSON
（如 /workspace/ai-zimeiti/inputs/x_posts_24h.json）。本适配器只读该文件，
绝不访问 X 的任何网络接口——本模块不引入任何网络库，
无任何联网代码。字段：id,url,author_username,author_name,created_at,text,lang,
likes,reposts,replies,quotes,impressions,links。
"""

from __future__ import annotations

import json
from pathlib import Path

from src.normalize import normalize_x_post

REQUIRED_RAW_FIELDS = ["id", "url", "text", "created_at"]


def load_x_posts(path: str | Path) -> list[dict]:
    """读取 X 帖子文件并归一化为统一素材格式（纯本地，无网络）。"""
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"X 帖子文件不存在：{p}")
    data = json.loads(p.read_text(encoding="utf-8"))
    if isinstance(data, dict):  # 允许 {"_comment": ..., "posts": [...]} 包装（假样例）
        data = data.get("posts")
    if not isinstance(data, list):
        raise ValueError("X 帖子文件应为帖子数组（list）")
    out = []
    for i, raw in enumerate(data):
        if not isinstance(raw, dict):
            raise ValueError(f"第 {i} 条不是对象")
        for f in REQUIRED_RAW_FIELDS:
            if raw.get(f) in (None, ""):
                raise ValueError(f"第 {i} 条缺必填字段 {f}")
        out.append(normalize_x_post(raw))
    return out
