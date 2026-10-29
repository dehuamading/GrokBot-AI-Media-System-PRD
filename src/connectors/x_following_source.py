"""连接器层（PRD 9.1）：X 关注名单读取适配器（降级模式）。

任务02 决定：Pi Agent 不访问 X；关注列表由 Grok Bot 读取并以文件交付
（inputs/x_following.json，135 个账号：username/name/description/followers）。
X 帖子内容当前不可得 —— 账号名单仅作为情报源注册表，内容级素材为已知缺口。
"""

from __future__ import annotations

import json
from pathlib import Path

from src.normalize import normalize_x_account


def load_following(path: str | Path) -> list[dict]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError("x_following.json 应为账号数组")
    accounts = [normalize_x_account(raw) for raw in data]
    seen: set[str] = set()
    out = []
    for acc in accounts:
        if acc["username"] in seen:
            continue
        seen.add(acc["username"])
        out.append(acc)
    return out
