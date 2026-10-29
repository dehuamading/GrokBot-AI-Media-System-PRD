"""工作流状态机：阶段流转、关口快照、断点恢复。

状态文件默认 data/state/workflow_state.json（.gitignore 已屏蔽整个 data/state/，
因其中含游标与真实 item_id，属本地运行数据，不入公开库）。
"""

from __future__ import annotations

import json
from pathlib import Path

STAGES = ["init", "collected", "waiting_gate1", "drafted", "waiting_gate2"]

TRANSITIONS = {  # 允许的阶段流转（含幂等重跑）；任意阶段都可重新 collect 开新一轮
    "init": {"init", "collected"},
    "collected": {"collected", "waiting_gate1"},
    "waiting_gate1": {"waiting_gate1", "drafted", "collected"},
    "drafted": {"drafted", "waiting_gate2", "collected"},
    "waiting_gate2": {"waiting_gate2", "collected"},
}

# 「collected」是唯一可从任意阶段进入的阶段（重跑一轮新采集）
for _s in TRANSITIONS:
    TRANSITIONS[_s].add("collected")


class WorkflowState:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.data: dict = {"stage": "init"}

    # ------------------------------------------------------------------
    def load(self) -> "WorkflowState":
        if self.path.exists():
            self.data = json.loads(self.path.read_text(encoding="utf-8"))
        return self

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data, ensure_ascii=False, indent=2),
                             encoding="utf-8")

    # ------------------------------------------------------------------
    @property
    def stage(self) -> str:
        return self.data.get("stage", "init")

    def set_stage(self, new_stage: str) -> None:
        if new_stage not in STAGES:
            raise ValueError(f"未知阶段：{new_stage}")
        allowed = TRANSITIONS.get(self.stage, set())
        if new_stage not in allowed:
            raise ValueError(f"非法流转：{self.stage} → {new_stage}（允许：{sorted(allowed)}）")
        self.data["stage"] = new_stage

    def update(self, **kw) -> None:
        self.data.update(kw)

    def get(self, key: str, default=None):
        return self.data.get(key, default)
