"""规则2落地 + 阶段4文案生成回归测试（全部 mock：无真实 Notion 写/LLM 调用）。

覆盖：
- 国外新工具国内暂不可用 → 国内适用性中性 1 分，卡上标注“国外新机会”；
- 敏感开通步骤过滤、收益承诺拦截；
- “未选”反馈、5 类受众映射、“待验证：待验证”去重；
- 偏好注入包含一般反馈（弱认可）、未选（弱信号）与全部正在使用规则；
- draft 输出 3 标题/2 开头/600–1200 字口播/演示/核验/来源，停在 GATE-2；
- 生成失败回退模板并在生成引擎如实标注。
"""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.analysis.fields import (audience_user, normalize_china_usability,
                                 parse_audience_categories)
from src.analysis.preference import (VALID_PICKS, build_preference_prompt,
                                     load_recent_preferences, record_feedback)
from src.analysis.safety import (forbidden_steps_check, income_promise_check,
                                 scrub_forbidden_steps)
from src.analysis.scoring import score_item_v3
from src.llm.base import Draft, DraftClient
from src.llm.draft_writer import DraftWriter, finalize_draft
from src.llm.template_client import TemplateClient
from src.notion_store.bootstrap import ensure_kb, ensure_kb_upgrades
from src.workflows import run
from src.workflows.state import WorkflowState
from tests.fakes import FakeNotionClient

REPO = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 11, 3, 12, tzinfo=timezone.utc)
MARKER = "国外新机会（国内暂不可用，只讲能力不讲开通）"


def _item(text: str, **kw) -> dict:
    base = {
        "item_id": "aihot:rule2-1",
        "source_type": "aihot",
        "source_url": "https://example.com/rule2",
        "author_or_publisher": "示例来源",
        "published_at": NOW.isoformat(),
        "collected_at": NOW.isoformat(),
        "title_or_summary": text,
        "claims": ["示例声明（mock）"],
        "evidence_status": "verified",
        "processing_status": "new",
        "comments": [],
        "extra": {},
    }
    base.update(kw)
    return base


def _card(item_id="aihot:rule2-1", *, platform="抖音"):
    return {
        "topic_id": "TX-20261103-V01",
        "拟定标题": "国外新工具｜只讲能力",
        "建议平台": platform,
        "用户指定平台": "",
        "面向谁": "想了解 AI 新工具的内容创作者",
        "面向用户": "AI内容创作者",
        "核心价值": "讲清工具能力和适用人群",
        "为什么现在": "刚发布，处于关注窗口",
        "关联素材": [item_id],
        "需求证据": "待验证：无直接评论证据",
        "可信度与风险": "官方发布，仍需核验",
        "最强反证": "国内暂不可用，不能演示开通",
        "差异与难度": "低",
        "推荐级别": "备选",
        "推荐理由": "v3 总分 10/18",
        "选题类型": "机会型",
        "原始来源": "https://example.com/rule2",
        "国内可用": "待验证",
        "是否付费": "待验证",
        "能否实际验证": "可验证：有原始出处链接",
        "生成引擎": "LLM",
        "_source_urls": ["https://example.com/rule2"],
    }


# ---------------------------------------------------------------- A 规则2评分
def test_overseas_new_tool_is_neutral_not_zero_and_not_lower_than_pending():
    text = "国外刚发布的新工具，新能力降低门槛，免费开源，手把手教程"
    overseas = score_item_v3(
        _item(text, china_usability="国外新工具，国内暂不可用，需要梯子/外币卡"),
        now=NOW)
    pending = score_item_v3(
        _item(text, china_usability="国内可用性待验证"), now=NOW)
    dim = next(d for d in overseas["dimensions"] if d["name"] == "国内适用性")
    assert dim["points"] == 1
    assert overseas["total"] > 0
    assert overseas["total"] >= pending["total"]
    assert overseas["overseas_new_opportunity"] is True
    assert "国外新机会" in dim["reason"]
    assert "只讲能力" in dim["reason"]
    # 新技术机会维度不受“国内不可用”拖累，仍可正常给分。
    tech = next(d for d in overseas["dimensions"] if d["name"] == "新技术机会")
    assert tech["points"] >= 1


def test_overseas_card_marks_only_capability_and_scrubs_signup_steps():
    mat = _item("国外新工具发布，新能力降低门槛，国内暂不可用，需要梯子")
    card = _card()
    card.update({
        "国内可用": "需要梯子",
        "切入角度": "先注册账号，再翻墙用 VPN，绑定外币卡开通",
        "推荐理由": "国外新工具值得关注；注册 / 翻墙步骤略",
    })
    merged = run._postprocess_round_cards(
        [card], [mat], now=NOW, profile=None, max_cards=3)
    assert merged
    got = merged[0]
    assert MARKER in got["国内可用"]
    assert forbidden_steps_check(json.dumps(got, ensure_ascii=False)) == []


def test_build_topic_cards_overseas_scrubs_signup_steps_too():
    from src.analysis.topics import build_topic_cards
    mat = _item("国外新工具发布，新能力降低门槛，国内暂不可用，需要梯子")
    mat["title_or_summary"] += "；先注册账号，再翻墙用 VPN，外币卡开通"
    cards, _ = build_topic_cards([mat], top_k=1, now=NOW)
    assert cards
    assert MARKER in cards[0]["国内可用"]
    assert forbidden_steps_check(json.dumps(cards[0], ensure_ascii=False)) == []


# ---------------------------------------------------------------- A safety
def test_forbidden_steps_check_and_scrub():
    text = ("先注册账号，再翻墙用 VPN；没有梯子就找代充，"
            "或者用虚拟卡/外币卡开通，绕过地区限制。最后讲清工具能力。")
    hits = forbidden_steps_check(text)
    for expected in ("注册", "翻墙", "VPN", "梯子", "代充", "虚拟卡",
                     "外币卡", "绕过地区限制"):
        assert expected in hits
    cleaned, removed = scrub_forbidden_steps(text)
    assert removed and forbidden_steps_check(cleaned) == []
    assert "讲清工具能力" in cleaned
    assert forbidden_steps_check("这个工具能做什么、为什么值得关注") == []


def test_income_promise_check_and_scrub():
    text = "学会后月入过万，稳赚不赔，保证收益，一定能赚钱。工具本身免费。"
    assert {"月入", "稳赚", "保证", "一定能"} <= set(income_promise_check(text))
    from src.analysis.safety import scrub_income_promises
    cleaned, removed = scrub_income_promises(text)
    assert removed and income_promise_check(cleaned) == []
    assert "工具本身免费" in cleaned


def test_no_paid_chain_excludes_deepseek_without_network():
    from src.llm.provider_chain import Provider, ProviderChain
    deepseek = Provider("deepseek", "https://deepseek.example.com/v1",
                        "fake-key", "mock", paid=True)
    chain = ProviderChain(providers=[deepseek], exclude_paid=True)
    assert chain._providers == []
    assert any(s["provider"] == "deepseek" and "no-paid" in s["reason"]
               for s in chain.skipped)


# ---------------------------------------------------------------- B1 未选
def test_unpicked_feedback_is_valid_but_not_approved_or_rejected():
    assert "未选" in VALID_PICKS
    client = FakeNotionClient()
    db_ids = ensure_kb(client, "root")
    ensure_kb_upgrades(client, db_ids)
    out = record_feedback(client, db_ids, {
        "topic_id": "TX-NONE-1", "原始选题": "未被选中的方向",
        "推荐理由": "备选", "用户选择": "未选", "原话反馈": "",
        "轮次": 1, "time": "2026-11-03T10:00:00+00:00",
    })
    assert out["approved_material_id"] is None
    prefs = load_recent_preferences(client, db_ids)
    assert [r["topic_id"] for r in prefs["unpicked"]] == ["TX-NONE-1"]
    assert all(r["用户选择"] != "未选"
               for r in prefs["approved"] + prefs["rejected"])
    text = build_preference_prompt(prefs)
    assert "未被选中（无理由，弱信号）" in text
    assert "未被选中的方向" in text


# ---------------------------------------------------------------- B2 受众
def test_audience_categories_are_whitelisted_and_join_with_dunhao():
    cats = parse_audience_categories("AI普通使用者、AI内容创作者、随便写的")
    assert cats == ["AI普通使用者", "AI内容创作者"]
    text = audience_user("AI普通使用者、AI内容创作者", "AI副业")
    assert "AI普通使用者" in text and "AI内容创作者" in text
    assert "、" in text
    inferred = audience_user("无法识别", "AI 副业接单变现教程")
    assert "AI副业与创业探索者" in inferred


class AudienceMockChat:
    model = "mock-audience"
    last_provider = "mock"
    last_model = "mock-audience"

    def __init__(self, target_user):
        self.target_user = target_user
        self.calls = 0
        self.seen = []
        self.usage_log = []

    def chat_json(self, system, user, **kw):  # noqa: ARG002
        self.calls += 1
        self.seen.append((system, user))
        return {"cards": [{
            "title": "受众卡片", "why_fit": "适合", "hook": "钩子",
            "refs": [1], "china": "国内可用", "risk": "风险",
            "counter": "反证", "difficulty": "低", "platform": "抖音",
            "score": 7, "target_user": self.target_user,
        }]}


def _material_for_cards():
    return _item("国内可用的免费开源 AI 教程，零基础手把手步骤，适合内容创作者",
                 item_id="x_posts:aud-1", source_type="x_posts",
                 platform="x", source_url="https://x.com/example_aud/status/1",
                 author_or_publisher="Example (@example_aud)",
                 evidence_status="self_reported")


def test_llm_target_user_whitelist_and_rule_fallback():
    from src.analysis.audience import load_profile
    from src.analysis.llm_topics import glm_build_cards
    profile = load_profile(REPO / "config" / "audience_profile.yaml")
    mat = _material_for_cards()

    ok_chat = AudienceMockChat("AI普通使用者、AI内容创作者")
    cards, _ = glm_build_cards(ok_chat, [mat], profile, now=NOW)
    assert cards
    assert cards[0]["面向用户"] == "AI普通使用者、AI内容创作者"

    bad_chat = AudienceMockChat("完全不在白名单里的受众")
    cards, _ = glm_build_cards(bad_chat, [mat], profile, now=NOW)
    assert cards
    assert any(name in cards[0]["面向用户"] for name in (
        "AI普通使用者", "AI内容创作者", "AI编程与应用开发者",
        "AI副业与创业探索者", "国外AI工具的国内使用者"))
    system = next(s for s, _ in bad_chat.seen if "选题卡" in s)
    assert "、" in system and "白名单" in system


# ---------------------------------------------------------------- B3 去重
def test_pending_prefix_is_not_duplicated():
    assert normalize_china_usability("待验证：待验证") == "待验证"
    assert normalize_china_usability("待验证：待验证：待核实") == "待验证：待核实"
    assert normalize_china_usability("不可用：不可用") == "不可用"


# ---------------------------------------------------------------- B4/B5 偏好
def _seed_05_row(client, db_ids, record_id, typ, status, content,
                 pick=None, say="", when="2026-11-03T09:00:00+00:00"):
    props = {
        "类型": {"select": {"name": typ}},
        "状态": {"select": {"name": status}},
        "内容": {"rich_text": [{"text": {"content": content}}]},
        "原始选题": {"rich_text": [{"text": {"content": content}}]},
        "原话反馈": {"rich_text": [{"text": {"content": say}}]},
        "反馈时间": {"date": {"start": when}},
    }
    if pick:
        props["用户选择"] = {"select": {"name": pick}}
    client.upsert_row(db_ids["05_经验与复盘"], "record_id", record_id, props)


def test_preference_prompt_has_neutral_weak_signal_and_all_active_rules_only():
    client = FakeNotionClient()
    db_ids = ensure_kb(client, "root")
    ensure_kb_upgrades(client, db_ids)
    record_feedback(client, db_ids, {
        "topic_id": "TX-G-1", "原始选题": "一般方向", "推荐理由": "中",
        "用户选择": "一般", "原话反馈": "说不上好也说不上坏",
        "轮次": 1, "time": "2026-11-03T11:00:00+00:00",
    })
    for i in range(1, 6):
        _seed_05_row(client, db_ids, f"RULE-T-00{i}", "规则", "正在使用",
                     f"正在使用规则{i}")
    _seed_05_row(client, db_ids, "CAND-MERGED", "候选规则", "已合并",
                 "已合并候选不应注入")

    prefs = load_recent_preferences(client, db_ids)
    assert len(prefs["rules"]) == 5
    assert all("已合并" not in (r.get("内容") or "") for r in prefs["rules"])
    text = build_preference_prompt(prefs)
    assert "一般反馈（弱认可）" in text
    assert "说不上好也说不上坏" in text
    for i in range(1, 6):
        assert f"正在使用规则{i}" in text
    assert "已合并候选不应注入" not in text


# ---------------------------------------------------------------- C/D 文案
class MockDraftChat:
    model = "mock-draft"
    last_provider = "mock"
    last_model = "mock-draft"

    def __init__(self, *, script=None, titles=None, openings=None):
        self.calls = 0
        self.seen = []
        self.usage_log = [{
            "provider": "mock", "model": "mock-draft",
            "prompt_tokens": 100, "completion_tokens": 200,
            "cost": 0.002, "cost_basis": "接口返回",
        }]
        self.script = script or ("这是一条 mock 口播稿。" * 80)
        self.titles = titles or ["标题一", "标题二", "标题三"]
        self.openings = openings or ["开头一", "开头二"]

    def chat_json(self, system, user, **kw):  # noqa: ARG002
        self.calls += 1
        self.seen.append((system, user))
        return {
            "titles": self.titles,
            "openings": self.openings,
            "script": self.script,
            "demo_suggestions": ["演示：只展示能力界面，不演示注册/开通"],
            "fact_checklist": [
                {"claim": "示例声明", "status": "待验证",
                 "source": "https://example.com/rule2"},
            ],
            "sources": ["https://evil.example.net/fabricated"],
        }


class FailingDraftChat:
    model = "mock-fail"
    last_provider = "mock"
    last_model = "mock-fail"
    calls = 0
    usage_log = []

    def chat_json(self, system, user, **kw):  # noqa: ARG002
        self.calls += 1
        raise RuntimeError("mock llm down")


def test_draft_writer_full_structure_and_original_sources_only():
    mat = _item("国外新工具发布，新能力降低门槛")
    card = _card()
    chat = MockDraftChat()
    draft = DraftWriter(chat=chat, fallback=TemplateClient()).generate(
        "抖音", card, [mat])

    assert len(draft.titles) == 3
    assert len(draft.openings) == 2
    assert 600 <= len(draft.body) <= 1200
    assert draft.demo_suggestions
    assert draft.fact_checklist
    assert all(("已核实" in c or "待验证" in c) and "来源" in c
               for c in draft.fact_checklist)
    assert draft.sources == ["https://example.com/rule2"]
    assert "evil.example.net" not in json.dumps(draft.to_text(),
                                                ensure_ascii=False)
    assert "只讲能力" in chat.seen[0][0]


def test_draft_writer_blocks_income_promises_and_forbidden_steps():
    mat = _item("国外新工具发布，新能力降低门槛")
    bad_script = ("先注册账号，再翻墙用 VPN，找不到就找代充或用外币卡开通。"
                  "学会后月入过万，稳赚不赔，保证收益，一定能赚钱。"
                  + "这只是能力介绍。" * 60)
    chat = MockDraftChat(
        script=bad_script,
        titles=["月入过万标题", "注册翻墙标题", "正常标题"],
        openings=["先翻墙再注册", "稳赚不赔"])
    draft = DraftWriter(chat=chat, fallback=TemplateClient()).generate(
        "抖音", _card(), [mat])

    blob = json.dumps({
        "titles": draft.titles, "openings": draft.openings,
        "body": draft.body, "demo": draft.demo_suggestions,
    }, ensure_ascii=False)
    assert forbidden_steps_check(blob) == []
    assert income_promise_check(blob) == []
    assert any("敏感步骤" in w for w in draft.unverified_warnings)
    assert any("收益承诺" in w for w in draft.unverified_warnings)
    assert 600 <= len(draft.body) <= 1200


def test_draft_writer_failure_falls_back_to_template_and_marks_engine():
    mat = _item("国外新工具发布，新能力降低门槛")
    draft = DraftWriter(chat=FailingDraftChat(),
                        fallback=TemplateClient()).generate(
        "小红书", _card(platform="两者"), [mat])
    assert "template" in draft.engine
    assert "回退" in draft.engine
    assert len(draft.titles) == 3 and len(draft.openings) == 2
    assert 600 <= len(draft.body) <= 1200


def _draft_args(tmp_path, **overrides):
    base = dict(
        state=str(tmp_path / "state.json"), page_id="root",
        topic=["TX-20261103-V01"], llm="chain", no_paid=True,
        export_dir=str(tmp_path / "exports"),
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _seed_topic_card(client, db_ids, card):
    props = {
        "拟定标题": {"rich_text": [{"text": {"content": card["拟定标题"]}}]},
        "建议平台": {"select": {"name": card["建议平台"]}},
        "用户指定平台": {"select": None},
        "面向谁": {"rich_text": [{"text": {"content": card["面向谁"]}}]},
        "面向用户": {"rich_text": [{"text": {"content": card["面向用户"]}}]},
        "核心价值": {"rich_text": [{"text": {"content": card["核心价值"]}}]},
        "为什么现在": {"rich_text": [{"text": {"content": card["为什么现在"]}}]},
        "关联素材": {"rich_text": [{"text": {"content": "、".join(card["关联素材"])}}]},
        "需求证据": {"rich_text": [{"text": {"content": card["需求证据"]}}]},
        "可信度与风险": {"rich_text": [{"text": {"content": card["可信度与风险"]}}]},
        "最强反证": {"rich_text": [{"text": {"content": card["最强反证"]}}]},
        "差异与难度": {"rich_text": [{"text": {"content": card["差异与难度"]}}]},
        "推荐级别": {"select": {"name": card["推荐级别"]}},
        "推荐理由": {"rich_text": [{"text": {"content": card["推荐理由"]}}]},
        "用户选择结果": {"select": {"name": "已选定"}},
        "选题类型": {"rich_text": [{"text": {"content": "机会型"}}]},
        "原始来源": {"url": card["原始来源"]},
        "国内可用": {"rich_text": [{"text": {"content": "待验证"}}]},
        "生成引擎": {"rich_text": [{"text": {"content": "LLM"}}]},
    }
    client.create_row_if_absent(db_ids["03_选题决策"], "topic_id",
                                card["topic_id"], props)


def test_draft_command_stops_at_gate2_writes_04_and_exports_markdown(
        tmp_path, capsys):
    card = _card()
    mat = _item("国外新工具发布，新能力降低门槛")
    client = FakeNotionClient()
    db_ids = ensure_kb(client, "root")
    ensure_kb_upgrades(client, db_ids)
    _seed_topic_card(client, db_ids, card)
    before = [dict(r["properties"]) for r in
              client.query_database(db_ids["03_选题决策"])]

    pool = tmp_path / "materials.json"
    pool.write_text(json.dumps([mat], ensure_ascii=False), encoding="utf-8")
    state = WorkflowState(tmp_path / "state.json")
    state.update(db_ids=db_ids, page_root="root",
                 last_collect={"snapshot": str(pool)})
    state.save()

    chat = MockDraftChat()
    args = _draft_args(tmp_path)
    rc = run.cmd_draft(args, client=client, chat=chat)
    out = capsys.readouterr().out

    assert rc == 0
    assert "⏸ GATE-2：等待用户审核，未发布" in out
    rows = client.query_database(db_ids["04_文案与版本"])
    assert len(rows) == 1  # 两者=一份通用稿（默认平台抖音这里仍是一份）
    draft_id = f"D-{card['topic_id']}-{card['建议平台']}-v1"
    row = client.row("04_文案与版本", "draft_id", draft_id)
    assert FakeNotionClient.plain(row["properties"]["status"]) == "待审核"
    assert FakeNotionClient.plain(row["properties"]["topic_id"]) == card["topic_id"]
    assert FakeNotionClient.plain(row["properties"]["platform"]) == "抖音"
    body = json.dumps(row["children"], ensure_ascii=False)
    for needle in ("标题候选", "开头候选", "完整口播稿", "操作演示建议",
                   "事实核验清单", "原始来源链接", "不会自动发布"):
        assert needle in body
    # 不改 03
    after = [dict(r["properties"]) for r in
             client.query_database(db_ids["03_选题决策"])]
    assert before == after
    # 注入文本与 markdown 导出
    inject = tmp_path / f"draft_injection_{card['topic_id']}.txt"
    assert inject.exists()
    assert "正在使用规则" in inject.read_text(encoding="utf-8")
    export = tmp_path / "exports" / f"{card['topic_id']}.md"
    assert export.exists()
    md = export.read_text(encoding="utf-8")
    assert "完整口播稿" in md and "GATE-2" in md
    # 成本日志：套餐额度折算口径明确
    log = (tmp_path / "cost_log.jsonl").read_text(encoding="utf-8").splitlines()
    rec = json.loads(log[-1])
    assert "套餐额度折算" in rec["note"] or "未知" in rec["note"]


def test_draft_command_both_platforms_one_general_draft(tmp_path):
    card = _card(platform="两者")
    card["用户指定平台"] = "两者"
    mat = _item("国外新工具发布，新能力降低门槛")
    client = FakeNotionClient()
    db_ids = ensure_kb(client, "root")
    ensure_kb_upgrades(client, db_ids)
    _seed_topic_card(client, db_ids, card)
    pool = tmp_path / "materials.json"
    pool.write_text(json.dumps([mat], ensure_ascii=False), encoding="utf-8")
    state = WorkflowState(tmp_path / "state.json")
    state.update(db_ids=db_ids, page_root="root",
                 last_collect={"snapshot": str(pool)})
    state.save()

    args = _draft_args(tmp_path)
    assert run.cmd_draft(args, client=client, chat=MockDraftChat()) == 0
    rows = client.query_database(db_ids["04_文案与版本"])
    assert len(rows) == 1
    draft_id = f"D-{card['topic_id']}-两者-v1"
    row = client.row("04_文案与版本", "draft_id", draft_id)
    assert FakeNotionClient.plain(row["properties"]["platform"]) == "两者"
