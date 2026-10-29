"""端到端工作流测试（离线）：collect → analyze → write → GATE-1 → draft → GATE-2。

使用假数据 + FakeNotionClient + 离线模板引擎；断言两道人工关口不可绕过、
无任何发布路径、不访问 X。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.llm.template_client import TemplateClient
from src.notion_store.bootstrap import ensure_kb, ensure_kb_upgrades
from src.workflows import pipeline
from src.workflows.state import WorkflowState
from tests.fakes import FakeNotionClient

FAKE = Path(__file__).resolve().parents[1] / "data" / "examples" / "aihot_fake_sample.json"
XF = Path(__file__).resolve().parents[1] / "data" / "examples" / "x_following.example.json"


def _run_to_gate1(tmp_path):
    state = WorkflowState(tmp_path / "state.json")
    out = pipeline.step_collect(state, aihot_file=FAKE, x_following_file=XF,
                                snapshot_dir=tmp_path / "raw")
    ana = pipeline.step_analyze(state, out["materials"], top_k=8)
    client = FakeNotionClient()
    db_ids = ensure_kb(client, "root")
    ensure_kb_upgrades(client, db_ids)
    written = pipeline.step_write_topics(client, db_ids, ana["cards"], out["materials"])
    return state, client, db_ids, out, ana, written


def test_full_workflow_with_gates(tmp_path):
    state, client, db_ids, out, ana, written = _run_to_gate1(tmp_path)

    # S1/S2/S3
    assert len(out["materials"]) == 8 and len(out["accounts"]) >= 1
    assert 5 <= len(ana["cards"]) <= 10
    assert written["topics_written"] == len(ana["cards"])
    assert state.stage == "waiting_gate1"

    # GATE-1：未选择 → 等待
    assert pipeline.step_gate1(client, db_ids) == []

    # 用户在 Notion 中选定 2 张（1-3 合法）
    client.set_select("03_选题决策", "topic_id", ana["cards"][0]["topic_id"],
                      "用户选择结果", "已选定")
    client.set_select("03_选题决策", "topic_id", ana["cards"][1]["topic_id"],
                      "用户选择结果", "已选定")
    selections = pipeline.step_gate1(client, db_ids)
    assert len(selections) == 2

    # S5 draft：每题×每平台恰好 1 份初稿
    res = pipeline.step_draft(client, db_ids, selections, out["materials"],
                              TemplateClient())
    drafts = res["drafts"]
    assert drafts and all(d["created"] for d in drafts)
    per_topic = {}
    for d in drafts:
        per_topic.setdefault(d["topic_id"], []).append(d["platform"])
    for tid, plats in per_topic.items():
        card_platform = next(s for s in selections if s["topic_id"] == tid)["platforms"]
        assert sorted(plats) == sorted(card_platform)  # 平台覆盖且不重复

    # 04 库行内容：状态 V1 待审；正文块含红线提示
    for d in drafts:
        row = client.row("04_文案与版本", "draft_id", d["draft_id"])
        assert FakeNotionClient.plain(row["properties"]["status"]) == "V1"
        body_text = json.dumps(row["children"], ensure_ascii=False)
        assert "不会自动发布" in body_text

    # 状态机推进到 GATE-2
    state.set_stage("drafted")
    state.set_stage("waiting_gate2")
    state.update(gate2={"status": "waiting", "drafts": drafts})
    state.save()
    assert WorkflowState(tmp_path / "state.json").load().stage == "waiting_gate2"

    # GATE-2 观测（只读）：1 份已确认、其余待审
    client.set_select("04_文案与版本", "draft_id", drafts[0]["draft_id"], "status", "已确认")
    g2 = pipeline.step_gate2(client, db_ids)
    assert g2["confirmed"] == 1 and g2["pending"] == len(drafts) - 1


def test_gate1_blocks_draft_when_no_selection(tmp_path):
    """关口语义：用户未在 Notion 选择时，draft 不产生任何文案。"""
    state, client, db_ids, out, ana, _ = _run_to_gate1(tmp_path)
    selections = pipeline.step_gate1(client, db_ids)
    assert selections == []
    # 即使误调用 step_draft（空选择），也不会生成任何内容
    res = pipeline.step_draft(client, db_ids, selections, out["materials"],
                              TemplateClient())
    assert res["drafts"] == []
    assert "04_文案与版本" not in client.databases or \
        not client.databases["04_文案与版本"]["rows"]


def test_draft_one_per_platform_idempotent_rerun(tmp_path):
    """重跑 draft：同一 draft_id 只更新不新建（幂等，PRD 6.1 只有一份稿）。"""
    _, client, db_ids, out, ana, _ = _run_to_gate1(tmp_path)
    client.set_select("03_选题决策", "topic_id", ana["cards"][0]["topic_id"],
                      "用户选择结果", "已选定")
    sel = pipeline.step_gate1(client, db_ids)
    r1 = pipeline.step_draft(client, db_ids, sel, out["materials"], TemplateClient())
    r2 = pipeline.step_draft(client, db_ids, sel, out["materials"], TemplateClient())
    assert r1["drafts"][0]["created"] is True
    assert r2["drafts"][0]["created"] is False  # 幂等更新
    assert len(client.databases["04_文案与版本"]["rows"]) == len(r2["drafts"])


def test_live_fetch_never_called_by_default(tmp_path, monkeypatch):
    """默认（非 --live）绝不触发真实抓取：注入会爆炸的 fetcher 验证未被调用。"""

    def boom(**kw):
        raise AssertionError("真实抓取被调用！默认必须离线回放")

    state = WorkflowState(tmp_path / "s.json")
    pipeline.step_collect(state, aihot_file=FAKE, x_following_file=XF,
                          snapshot_dir=None, fetcher=boom)
