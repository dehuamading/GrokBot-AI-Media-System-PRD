"""偏好学习闭环测试（全部 mock Notion / mock LLM，无真实写入与网络）。

覆盖：写反馈+认可素材、绝不改 03、读最近 5+5、注入文本进入 prompt、
3 轮出候选规则且状态=待确认、confirm 只提示不改状态。
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.analysis.audience import load_profile
from src.analysis.llm_topics import glm_score_posts
from src.analysis.preference import (build_preference_prompt,
                                     load_recent_preferences, propose_rules,
                                     record_feedback)
from src.notion_store.bootstrap import ensure_kb, ensure_kb_upgrades
from src.workflows import run
from tests.fakes import FakeNotionClient

REPO = Path(__file__).resolve().parents[1]
PROFILE = load_profile(REPO / "config" / "audience_profile.yaml")


def _db():
    client = FakeNotionClient()
    db_ids = ensure_kb(client, "root")
    ensure_kb_upgrades(client, db_ids)
    return client, db_ids


def _feedback(topic_id="TX-20261102-V01", pick="认可", round_no=1, say="这个方向对",
              title="AI 副业选题", why="用户痛点明确", reason="方向正确", time=None):
    return {
        "topic_id": topic_id, "原始选题": title, "推荐理由": why,
        "用户选择": pick, "原话反馈": say, "原因标签": reason,
        "轮次": round_no, "下轮参考": "继续同类",
        "time": time or "2026-11-02T12:00:00+00:00",
    }


def test_record_feedback_writes_feedback_and_approved_material_without_touching_03():
    client, db_ids = _db()
    client.upsert_row(db_ids["03_选题决策"], "topic_id", "TX-20261102-V01", {
        "用户选择结果": {"select": {"name": "待用户选择"}},
    })
    before_03 = [dict(r["properties"]) for r in client.query_database(db_ids["03_选题决策"])]

    out = record_feedback(client, db_ids, _feedback(pick="认可"))
    assert out["feedback_id"] == "FB-1-TX-20261102-V01"
    assert out["approved_material_id"] == "AM-TX-20261102-V01"

    fb = client.row("05_经验与复盘", "record_id", out["feedback_id"])
    assert FakeNotionClient.plain(fb["properties"]["类型"]) == "反馈"
    assert FakeNotionClient.plain(fb["properties"]["性质"]) == "用户偏好"
    assert FakeNotionClient.plain(fb["properties"]["状态"]) == "待验证"
    assert FakeNotionClient.plain(fb["properties"]["用户选择"]) == "认可"
    assert FakeNotionClient.plain(fb["properties"]["原话反馈"]) == "这个方向对"

    am = client.row("05_经验与复盘", "record_id", out["approved_material_id"])
    assert FakeNotionClient.plain(am["properties"]["类型"]) == "认可素材"
    assert FakeNotionClient.plain(am["properties"]["性质"]) == "用户偏好"
    assert "来源" in FakeNotionClient.plain(am["properties"]["内容"])

    after_03 = [dict(r["properties"]) for r in client.query_database(db_ids["03_选题决策"])]
    assert before_03 == after_03  # 绝不改 03 卡状态
    assert len(client.databases["03_选题决策"]["rows"]) == 1


def test_load_recent_preferences_returns_five_pos_and_five_neg_with_quotes():
    client, db_ids = _db()
    for i in range(6):
        record_feedback(client, db_ids, _feedback(
            topic_id=f"T-OK-{i}", pick="认可", round_no=i + 1,
            say=f"认可原话{i}", time=f"2026-11-02T12:0{i}:00+00:00"))
    for i in range(6):
        record_feedback(client, db_ids, _feedback(
            topic_id=f"T-NO-{i}", pick="否决", round_no=i + 1,
            say=f"否决原话{i}", time=f"2026-11-02T13:0{i}:00+00:00"))

    prefs = load_recent_preferences(client, db_ids, n_pos=5, n_neg=5)
    assert len(prefs["approved"]) == 5
    assert len(prefs["rejected"]) == 5
    assert all(p["原话反馈"] for p in prefs["approved"])
    assert all(p["原话反馈"] for p in prefs["rejected"])
    assert "认可原话5" in {p["原话反馈"] for p in prefs["approved"]}


def test_preference_text_is_injected_into_glm_prompt():
    class FakeChat:
        model = "mock-pref"

        def __init__(self):
            self.systems = []
            self.users = []

        def chat_json(self, system, user, **kw):  # noqa: ARG002
            self.systems.append(system)
            self.users.append(user)
            return {"results": [{"n": 1, "score": 8, "reason": "mock"}]}

    material = {
        "item_id": "mock:1", "source_url": "https://example.com/a",
        "author_or_publisher": "mock", "title_or_summary": "AI 教程与国内可用工具",
        "claims": [], "extra": {},
    }
    chat = FakeChat()
    glm_score_posts(chat, [material], PROFILE,
                    preference_text="【偏好注入】近期认可示例：AI 副业")
    blob = "\n".join(chat.systems + chat.users)
    assert "【偏好注入】" in blob
    assert "近期认可示例" in blob


def test_build_preference_prompt_marks_candidate_rules_as_reference_only():
    prefs = {
        "approved": [{"原始选题": "AI 副业", "原话反馈": "这个好", "原因标签": "方向对"}],
        "rejected": [{"原始选题": "空投", "原话反馈": "不相关", "原因标签": "受众无关"}],
        "rules": [{"内容": "优先国内可用工具"}],
        "candidate_rules": [{"内容": "候选：多做教程"}],
    }
    text = build_preference_prompt(prefs)
    assert "近期认可示例" in text
    assert "近期否决示例" in text
    assert "正在使用规则" in text
    assert "候选规则" in text
    assert "仅供参考" in text and "不得作为硬规则" in text
    assert "空投" in text and "不相关" in text


def test_three_rounds_propose_candidate_rules_pending_confirmation():
    client, db_ids = _db()
    for rnd in (1, 2, 3):
        record_feedback(client, db_ids, _feedback(
            topic_id=f"T-{rnd}", pick="认可" if rnd != 2 else "否决",
            round_no=rnd, say=f"第{rnd}轮原话", reason=f"原因{rnd}"))
    before_03 = len(client.databases["03_选题决策"]["rows"])

    out = propose_rules(client, db_ids, now=None)
    assert out["rounds"] == [1, 2, 3]
    assert 1 <= len(out["proposed"]) <= 10

    rows = client.query_database(db_ids["05_经验与复盘"])
    rules = [r for r in rows
             if FakeNotionClient.plain(r["properties"].get("类型", {})) == "候选规则"]
    assert 1 <= len(rules) <= 10
    for r in rules:
        assert FakeNotionClient.plain(r["properties"]["状态"]) == "待确认"
    assert not any(FakeNotionClient.plain(r["properties"].get("状态", {})) == "正在使用"
                   for r in rows)
    assert len(client.databases["03_选题决策"]["rows"]) == before_03  # 不改 03


def test_rules_confirm_only_prints_manual_instruction_and_does_not_change_status(
        monkeypatch, tmp_path, capsys):
    client, db_ids = _db()
    for rnd in (1, 2, 3):
        record_feedback(client, db_ids, _feedback(topic_id=f"T-{rnd}", round_no=rnd))
    proposed = propose_rules(client, db_ids)["proposed"]
    record_id = proposed[0]
    before = FakeNotionClient.plain(
        client.row("05_经验与复盘", "record_id", record_id)["properties"]["状态"])

    monkeypatch.setattr(run, "NotionClient", lambda: client)
    args = SimpleNamespace(state=str(tmp_path / "state.json"), page_id="root",
                           propose=False, confirm=record_id, llm="template")
    assert run.cmd_rules(args) == 0
    out = capsys.readouterr().out
    assert "请在 Notion 中由用户手动把状态改为正在使用" in out
    after = FakeNotionClient.plain(
        client.row("05_经验与复盘", "record_id", record_id)["properties"]["状态"])
    assert before == after == "待确认"
