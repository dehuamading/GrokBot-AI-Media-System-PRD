"""状态机 / 人工关口 / Notion 只增列 测试（FakeNotionClient，离线）。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.notion_store.bootstrap import (DATABASE_SPECS, DB_UPGRADES,
                                        ensure_kb, ensure_kb_upgrades)
from src.notion_store.gates import (read_gate1_selections, read_gate2_status,
                                    validate_gate1, write_draft_row)
from src.llm.base import Draft
from src.workflows.state import WorkflowState
from tests.fakes import FakeNotionClient


# ---------------------------------------------------------------- 状态机
def test_state_transitions(tmp_path):
    st = WorkflowState(tmp_path / "s.json")
    st.set_stage("collected")
    st.set_stage("waiting_gate1")
    st.set_stage("drafted")
    st.set_stage("waiting_gate2")
    st.save()
    st2 = WorkflowState(tmp_path / "s.json").load()
    assert st2.stage == "waiting_gate2"


def test_state_illegal_transition(tmp_path):
    st = WorkflowState(tmp_path / "s.json")
    st.set_stage("collected")
    with pytest.raises(ValueError):
        st.set_stage("waiting_gate2")  # 跳关


def test_state_recollect_allowed(tmp_path):
    st = WorkflowState(tmp_path / "s.json")
    st.set_stage("collected")
    st.set_stage("waiting_gate1")
    st.set_stage("drafted")
    st.set_stage("waiting_gate2")
    st.set_stage("collected")  # 任意阶段可重开新一轮采集
    st.set_stage("waiting_gate1")


# ---------------------------------------------------------------- 建库/升级
def test_ensure_kb_idempotent_and_upgrades_add_only():
    c = FakeNotionClient()
    ids = ensure_kb(c, "root-page")
    assert set(ids) == set(DATABASE_SPECS)
    assert ensure_kb(c, "root-page") == ids  # 幂等
    # 全新建库已含升级列 → 无需升级
    added = ensure_kb_upgrades(c, ids)
    assert added == {"03_选题决策": [], "04_文案与版本": []}

    # 遗留旧库（无新列，模拟任务02已建的 Notion 库）→ 只增列升级
    legacy = FakeNotionClient()
    legacy.databases["03_选题决策"] = {"id": "db03", "properties": {k: v for k, v in
        DATABASE_SPECS["03_选题决策"][1].items() if k != "用户指定平台"}, "rows": []}
    legacy.databases["04_文案与版本"] = {"id": "db04", "properties": {k: v for k, v in
        DATABASE_SPECS["04_文案与版本"][1].items() if k != "生成引擎"}, "rows": []}
    for name, spec in DATABASE_SPECS.items():
        if name not in legacy.databases:
            legacy.create_database("root", name, spec[1])
    ladded = ensure_kb_upgrades(legacy, {n: d["id"] for n, d in legacy.databases.items()})
    assert set(ladded["03_选题决策"]) == {"用户指定平台"}
    assert set(ladded["04_文案与版本"]) == {"生成引擎"}
    # 只增列：不删除既有属性
    props03 = legacy.get_database("db03")["properties"]
    assert "用户选择结果" in props03 and "topic_id" in props03


def test_db_upgrades_subset_of_specs():
    """升级列必须同时存在于全新建库 spec 中（两种路径结构一致）。"""
    for db, cols in DB_UPGRADES.items():
        for col in cols:
            assert col in DATABASE_SPECS[db][1]


# ---------------------------------------------------------------- GATE-1
def _setup_gate1_db():
    c = FakeNotionClient()
    ids = ensure_kb(c, "root")
    ensure_kb_upgrades(c, ids)
    for i, (tid, status, plat) in enumerate([
            ("T-001", "待用户选择", ""), ("T-002", "已选定", "抖音"),
            ("T-003", "已选定", "两者"), ("T-004", "未选用", "")]):
        c.upsert_row(ids["03_选题决策"], "topic_id", tid, {
            "拟定标题": {"rich_text": [{"text": {"content": f"标题{i}"}}]},
            "建议平台": {"select": {"name": "抖音"}},
            "面向谁": {"rich_text": [{"text": {"content": "职场提效者"}}]},
            "核心价值": {"rich_text": [{"text": {"content": "v"}}]},
            "为什么现在": {"rich_text": [{"text": {"content": "n"}}]},
            "关联素材": {"rich_text": [{"text": {"content": f"aihot:fake-00{i + 1}"}}]},
            "需求证据": {"rich_text": [{"text": {"content": "待验证：无直接证据"}}]},
            "可信度与风险": {"rich_text": [{"text": {"content": "r"}}]},
            "最强反证": {"rich_text": [{"text": {"content": "c"}}]},
            "差异与难度": {"rich_text": [{"text": {"content": "d"}}]},
            "推荐级别": {"select": {"name": "推荐"}},
            "推荐理由": {"rich_text": [{"text": {"content": "y"}}]},
            "用户选择结果": {"select": {"name": status}},
            "用户指定平台": {"select": {"name": plat} if plat else None},
        })
    return c, ids


def test_gate1_reads_only_selected_and_respects_user_platform():
    c, ids = _setup_gate1_db()
    sel = read_gate1_selections(c, ids)
    assert [s["topic_id"] for s in sel] == ["T-002", "T-003"]  # 只认已选定
    assert sel[0]["platforms"] == ["抖音"]      # 用户指定平台优先
    assert sel[1]["platforms"] == ["抖音", "小红书"]  # 两者 → 两份稿
    assert sel[0]["需求证据"].startswith("待验证")
    assert sel[0]["关联素材"] == ["aihot:fake-002"]


def test_gate1_validation_bounds():
    validate_gate1([{"topic_id": "T-1"}])
    validate_gate1([{"topic_id": f"T-{i}"} for i in range(5)])
    with pytest.raises(ValueError):
        validate_gate1([])
    with pytest.raises(ValueError):
        validate_gate1([{"topic_id": f"T-{i}"} for i in range(6)])


# ---------------------------------------------------------------- 文案写入 + GATE-2
def test_write_draft_row_and_gate2():
    c, ids = _setup_gate1_db()
    draft = Draft(topic_id="T-002", platform="抖音", title="标题",
                  body="正文", fact_checklist=["f1"], sources=["https://example.com/a"],
                  unverified_warnings=["w1"])
    draft.draft_id = "D-T-002-抖音-v1"
    pid, created = write_draft_row(c, ids, draft, "T-002")
    assert created
    row = c.row("04_文案与版本", "draft_id", "D-T-002-抖音-v1")
    assert FakeNotionClient.plain(row["properties"]["status"]) == "V1"   # 待用户确认
    assert FakeNotionClient.plain(row["properties"]["生成引擎"]) == "template@v1"
    kinds = [b["type"] for b in row["children"]]
    assert "heading_2" in kinds and "paragraph" in kinds
    assert any("未证实" in json_dumps(b) for b in row["children"])
    assert any("不会自动发布" in json_dumps(b) for b in row["children"])  # 红线提示写入正文

    c.set_select("04_文案与版本", "draft_id", "D-T-002-抖音-v1", "status", "已确认")
    g2 = read_gate2_status(c, ids)
    assert g2["confirmed"] == 1 and g2["pending"] == 0
    assert "无发布能力" in g2["note"]


def json_dumps(b):
    import json
    return json.dumps(b, ensure_ascii=False)
