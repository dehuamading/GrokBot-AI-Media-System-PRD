"""阶段4审查修复回归测试（全部 mock：无网络、无真实 LLM/Notion）。

覆盖审查 tasks/pi-p4-fix1.md 的 10 条修复。
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.analysis.safety import (forbidden_steps_check, income_promise_check,
                                 scrub_income_promises)
from src.analysis.scoring import (OVERSEAS_OPPORTUNITY_LABEL,
                                  score_item_v3)
from src.llm.base import Draft, DraftClient
from src.llm.draft_writer import (DraftWriter, _split_urls, collect_sources,
                                  finalize_draft)
from src.llm.template_client import TemplateClient
from src.notion_store.bootstrap import ensure_kb, ensure_kb_upgrades
from src.notion_store.gates import (_card_from_row, read_topic_cards_by_ids,
                                    write_draft_row_pending)
from src.workflows import pipeline, run
from src.workflows.state import WorkflowState
from tests.fakes import FakeNotionClient

NOW = datetime(2026, 11, 3, 12, tzinfo=timezone.utc)
MARKER = OVERSEAS_OPPORTUNITY_LABEL


# ---------------------------------------------------------------- helpers
def _item(text: str, **kw) -> dict:
    base = {
        "item_id": "aihot:fix1-1",
        "source_type": "aihot",
        "source_url": "https://example.com/fix1",
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


def _card(topic_id="TX-FIX1-V01", **kw) -> dict:
    card = {
        "topic_id": topic_id,
        "拟定标题": "AI 新工具｜只讲能力",
        "建议平台": "抖音",
        "用户指定平台": "",
        "面向谁": "想了解 AI 新工具的内容创作者",
        "面向用户": "AI内容创作者",
        "核心价值": "讲清工具能力和适用人群",
        "为什么现在": "刚发布，处于关注窗口",
        "关联素材": ["aihot:fix1-1"],
        "需求证据": "待验证：无直接评论证据",
        "可信度与风险": "官方发布，仍需核验",
        "最强反证": "国内暂不可用，不能演示开通",
        "差异与难度": "低",
        "推荐级别": "备选",
        "推荐理由": "v3 总分 10/18",
        "选题类型": "机会型",
        "原始来源": "https://example.com/fix1",
        "国内可用": "待验证",
        "是否付费": "待验证",
        "能否实际验证": "可验证：有原始出处链接",
        "生成引擎": "LLM",
        "_source_urls": ["https://example.com/fix1"],
    }
    card.update(kw)
    return card


def _dims(sc: dict) -> dict[str, dict]:
    return {d["name"]: d for d in sc["dimensions"]}


def _seed_topic(client, db_ids, card, status="已选定"):
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
        "用户选择结果": {"select": {"name": status}},
        "选题类型": {"rich_text": [{"text": {"content": "机会型"}}]},
        "原始来源": {"url": card["原始来源"]},
        "国内可用": {"rich_text": [{"text": {"content": "待验证"}}]},
        "生成引擎": {"rich_text": [{"text": {"content": "LLM"}}]},
    }
    client.create_row_if_absent(db_ids["03_选题决策"], "topic_id",
                                card["topic_id"], props)


def _draft_args(tmp_path, **overrides):
    base = dict(
        state=str(tmp_path / "state.json"), page_id="root",
        topic=["TX-FIX1-V01"], llm="chain", no_paid=True,
        export_dir=None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _write_state(tmp_path, db_ids, materials=None):
    state = WorkflowState(tmp_path / "state.json")
    data = {"db_ids": db_ids, "page_root": "root"}
    if materials is not None:
        pool = tmp_path / "materials.json"
        pool.write_text(json.dumps(materials, ensure_ascii=False),
                        encoding="utf-8")
        data["last_collect"] = {"snapshot": str(pool)}
    state.update(**data)
    state.save()
    return state


class _MockChat:
    last_provider = "mock"
    last_model = "mock-draft"
    model = "mock-draft"

    def __init__(self):
        self.calls = 0
        self.usage_log = [{
            "provider": "mock", "model": "mock-draft",
            "prompt_tokens": 10, "completion_tokens": 20,
            "cost": 0.001, "cost_basis": "接口返回",
        }]

    def chat_json(self, system, user, **kw):  # noqa: ARG002
        self.calls += 1
        return {
            "titles": ["标题一", "标题二", "标题三"],
            "openings": ["开头一", "开头二"],
            "script": "这是一条 mock 口播稿。" * 80,
            "demo_suggestions": ["演示：只展示能力界面"],
            "fact_checklist": [{"claim": "示例声明", "status": "待验证",
                                "source": "https://example.com/fix1"}],
            "sources": ["https://example.com/fix1"],
        }


# ---------------------------------------------------------------- 1 国内不可用
@pytest.mark.parametrize("explicit", [
    "不可用",
    "不可用：美区限制",
    "美区不可用",
    "安卓不可用",
])
def test_bare_unavailable_explicit_is_zero_not_two(explicit):
    sc = score_item_v3(_item("某工具能力介绍", china_usability=explicit),
                       now=NOW)
    dim = _dims(sc)["国内适用性"]
    assert dim["points"] == 0, explicit
    assert sc["overseas_new_opportunity"] is False
    assert sc["china_pending"] is False


def test_bare_unavailable_in_text_is_zero_not_two():
    sc = score_item_v3(_item("美区不可用，只讲能力"), now=NOW)
    assert _dims(sc)["国内适用性"]["points"] == 0
    assert sc["overseas_new_opportunity"] is False


def test_domestic_new_tool_hard_unavailable_is_still_zero():
    # “新工具”+“不可用”不等于“国外新品”：没有国外信号时必须判 0。
    sc = score_item_v3(_item("国内新工具上线，国内不可用"), now=NOW)
    dim = _dims(sc)["国内适用性"]
    assert dim["points"] == 0
    assert sc["overseas_new_opportunity"] is False


def test_overseas_new_unavailable_is_exactly_one_point():
    explicit = score_item_v3(
        _item("国外刚发布的新工具，新能力降低门槛",
              china_usability="国外新工具，国内不可用"),
        now=NOW)
    text_only = score_item_v3(
        _item("国外刚发布的新工具，国内不可用，新能力降低门槛"), now=NOW)
    for sc in (explicit, text_only):
        dim = _dims(sc)["国内适用性"]
        assert dim["points"] == 1
        assert sc["overseas_new_opportunity"] is True
        assert "国外新机会" in dim["reason"]


# ---------------------------------------------------------------- 2 显式字段优先
def test_explicit_available_is_not_downgraded_by_text_paywall_keywords():
    sc = score_item_v3(
        _item("国内可用，但需付费、需要订阅、受限",
              china_usability="国内可用"),
        now=NOW)
    dim = _dims(sc)["国内适用性"]
    assert dim["points"] == 2
    assert dim["reason"].startswith("素材标注：国内可用")


# ---------------------------------------------------------------- 3 国内付费新工具
def test_domestic_paid_new_tool_is_not_labelled_overseas():
    sc = score_item_v3(
        _item("国内新上线的新工具，需付费、需要订阅"), now=NOW)
    dim = _dims(sc)["国内适用性"]
    assert dim["points"] == 1
    assert sc["overseas_new_opportunity"] is False
    assert sc["china_label"] == ""
    assert MARKER not in dim["reason"]


# ---------------------------------------------------------------- 4 chat=None
def test_chat_none_fallback_goes_through_finalize_draft():
    card = _card(拟定标题="AI 工具｜月入过万",
                 核心价值="先注册账号，再翻墙用VPN，学会月入过万")
    mat = _item("国外新工具发布，新能力降低门槛")
    draft = DraftWriter(chat=None, fallback=TemplateClient()).generate(
        "抖音", card, [mat])

    assert len(draft.titles) == 3
    assert len(draft.openings) == 2
    assert 600 <= len(draft.body) <= 1200
    blob = json.dumps({
        "titles": draft.titles, "openings": draft.openings,
        "body": draft.body, "demo": draft.demo_suggestions,
    }, ensure_ascii=False)
    assert forbidden_steps_check(blob) == []
    assert income_promise_check(blob) == []
    assert any("敏感步骤" in w for w in draft.unverified_warnings)
    assert any("收益承诺" in w for w in draft.unverified_warnings)


# ---------------------------------------------------------------- 5 draft_id
def test_llm_draft_has_unique_id_and_two_cards_do_not_swallow_rows():
    client = FakeNotionClient()
    db_ids = ensure_kb(client, "root")
    ensure_kb_upgrades(client, db_ids)
    mat = _item("国外新工具发布，新能力降低门槛")
    writer = DraftWriter(chat=_MockChat(), fallback=TemplateClient())

    drafts = []
    for tid in ("TX-FIX1-V01", "TX-FIX1-V02"):
        card = _card(topic_id=tid)
        draft = writer.generate("抖音", card, [mat])
        assert draft.draft_id == f"D-{tid}-抖音-v1"
        assert draft.version == 1
        write_draft_row_pending(client, db_ids, draft, tid)
        drafts.append(draft)

    rows = client.query_database(db_ids["04_文案与版本"])
    assert len(rows) == 2
    for draft in drafts:
        row = client.row("04_文案与版本", "draft_id", draft.draft_id)
        assert FakeNotionClient.plain(row["properties"]["topic_id"]) == \
            draft.topic_id


# ---------------------------------------------------------------- 6 未选定拒绝
def test_read_topic_cards_by_ids_rejects_unselected_cards():
    client = FakeNotionClient()
    db_ids = ensure_kb(client, "root")
    ensure_kb_upgrades(client, db_ids)
    _seed_topic(client, db_ids, _card(), status="待用户选择")

    with pytest.raises(ValueError, match="未选定|已选定"):
        read_topic_cards_by_ids(client, db_ids, ["TX-FIX1-V01"])


def test_cmd_draft_rejects_unselected_topic_and_writes_nothing(
        tmp_path, capsys):
    client = FakeNotionClient()
    db_ids = ensure_kb(client, "root")
    ensure_kb_upgrades(client, db_ids)
    _seed_topic(client, db_ids, _card(), status="待用户选择")
    _write_state(tmp_path, db_ids)

    rc = run.cmd_draft(_draft_args(tmp_path), client=client, chat=_MockChat())
    out = capsys.readouterr().out
    assert rc == 1
    assert "未选定" in out and "已选定" in out
    assert client.query_database(db_ids["04_文案与版本"]) == []


# ---------------------------------------------------------------- 7 openai-compat
class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def _openai_payload():
    return {
        "choices": [{"message": {"content": json.dumps({
            "title": "AI 标题",
            "body": "只讲能力的正文。" * 80,
            "unverified_warnings": [],
        }, ensure_ascii=False)}}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 200,
                  "cost": 0.001},
    }


def _prepare_openai_cmd(tmp_path, monkeypatch):
    client = FakeNotionClient()
    db_ids = ensure_kb(client, "root")
    ensure_kb_upgrades(client, db_ids)
    _seed_topic(client, db_ids, _card())
    _write_state(tmp_path, db_ids, [_item("国外新工具发布，新能力降低门槛")])
    monkeypatch.setenv("LLM_API_BASE", "https://llm.example.com/v1")
    monkeypatch.setenv("LLM_API_KEY", "sk-test-DO-NOT-LEAK")
    monkeypatch.setenv("LLM_MODEL", "fake-model")
    return client, db_ids


def test_openai_compat_passes_injection_and_records_usage(
        tmp_path, monkeypatch, capsys):
    client, db_ids = _prepare_openai_cmd(tmp_path, monkeypatch)
    captured = {}

    def fake_post(url, headers=None, json=None, timeout=None):  # noqa: A002
        captured["body"] = json
        return _FakeResponse(_openai_payload())

    with mock.patch("src.llm.openai_compat.requests.post", side_effect=fake_post):
        rc = run.cmd_draft(_draft_args(tmp_path, llm="openai-compat"),
                           client=client, chat=None)

    assert rc == 0
    user_msg = captured["body"]["messages"][1]["content"]
    assert "【03 选题卡全部字段】" in user_msg
    assert "【该卡用户反馈原话】" in user_msg

    log = (tmp_path / "cost_log.jsonl").read_text(encoding="utf-8").splitlines()
    rec = json.loads(log[-1])
    assert rec["calls"] == 1
    assert rec["provider"] == "openai-compat"
    assert rec["model"] == "fake-model"
    assert rec["tokens"] == 300
    assert rec["cost"] == 0.001
    assert rec["cost_basis"] == "接口返回"
    assert client.query_database(db_ids["04_文案与版本"])


def test_openai_compat_failure_falls_back_to_template(
        tmp_path, monkeypatch, capsys):
    client, db_ids = _prepare_openai_cmd(tmp_path, monkeypatch)

    def fake_post(*args, **kwargs):  # noqa: ARG001
        raise RuntimeError("mock openai down")

    with mock.patch("src.llm.openai_compat.requests.post", side_effect=fake_post):
        rc = run.cmd_draft(_draft_args(tmp_path, llm="openai-compat"),
                           client=client, chat=None)

    out = capsys.readouterr().out
    assert rc == 0
    assert "回退" in out
    rows = client.query_database(db_ids["04_文案与版本"])
    assert len(rows) == 1
    engine = FakeNotionClient.plain(rows[0]["properties"]["生成引擎"])
    assert "template" in engine and "回退" in engine


def test_draft_no_paid_is_default_on(monkeypatch):
    captured = {}

    def fake_cmd_draft(args):
        captured.update(vars(args))
        return 0

    monkeypatch.setattr(run, "cmd_draft", fake_cmd_draft)
    monkeypatch.setattr(sys, "argv", ["run", "draft"])
    assert run.main() == 0
    assert captured["no_paid"] is True


# ---------------------------------------------------------------- 8 旧 --draft
class _BadDraftClient(DraftClient):
    def generate(self, platform: str, card: dict, materials: list[dict]) -> Draft:
        return Draft(
            topic_id=card["topic_id"], platform=platform,
            title="月入过万标题",
            body=("先注册账号，再翻墙用VPN，学会后月入过万。"
                  "工具本身只讲能力。" * 60),
            fact_checklist=["待验证：示例｜来源：https://example.com/fix1"],
            sources=["https://example.com/fix1"],
        )


def test_legacy_do_draft_applies_safety_and_income_scrub(
        tmp_path, monkeypatch):
    client = FakeNotionClient()
    db_ids = ensure_kb(client, "root")
    ensure_kb_upgrades(client, db_ids)
    card = _card()
    card["platforms"] = ["抖音"]
    state = WorkflowState(tmp_path / "state.json")
    state.update(db_ids=db_ids, page_root="root", stage="waiting_gate1")
    state.save()

    monkeypatch.setattr(run, "make_llm", lambda name: _BadDraftClient())
    args = SimpleNamespace(state=str(tmp_path / "state.json"), page_id="root",
                           llm="template")
    assert run._do_draft(args, state, client, db_ids, [card]) == 0

    rows = client.query_database(db_ids["04_文案与版本"])
    assert len(rows) == 1
    row = client.row("04_文案与版本", "draft_id", "D-TX-FIX1-V01-抖音-v1")
    blob = json.dumps(row["children"], ensure_ascii=False)
    assert forbidden_steps_check(blob) == []
    assert income_promise_check(blob) == []


# ---------------------------------------------------------------- 9 safety
def test_income_promise_includes_lie_zhuan_and_is_scrubbed():
    assert "躺赚" in income_promise_check("轻松躺赚，无需努力")
    cleaned, removed = scrub_income_promises("轻松躺赚，无需努力。工具免费。")
    assert "躺赚" in removed
    assert "躺赚" not in cleaned
    assert "工具免费" in cleaned


def test_signup_pattern_is_step_only_and_vpn_has_boundary():
    assert "注册" in forbidden_steps_check("注册账号后使用")
    assert "注册" in forbidden_steps_check("先注册再体验")
    assert "注册" in forbidden_steps_check("去注册一个账户")
    assert forbidden_steps_check("无需注册即可用，注册用户破百万") == []
    assert forbidden_steps_check("openvpn 是一个网络工具") == []
    assert "VPN" in forbidden_steps_check("用VPN访问")
    assert "VPN" in forbidden_steps_check("用 vpn 访问")


# ---------------------------------------------------------------- 10 URL split
def test_source_urls_split_all_chinese_delimiters_and_list_items():
    raw = ("https://a.example.com/1，https://b.example.com/2、"
           "https://c.example.com/3；https://d.example.com/4 "
           "https://e.example.com/5")
    expected = [f"https://{c}.example.com/{i}"
                for c, i in zip("abcde", range(1, 6))]
    assert _split_urls(raw) == expected
    assert _split_urls(["https://a.example.com/1，https://b.example.com/2"]) == \
        expected[:2]
    card = {"_source_urls": ["https://a.example.com/1，https://b.example.com/2"],
            "原始来源": raw}
    assert collect_sources(card, []) == expected


def test_gates_card_source_urls_split_chinese_delimiters():
    row = {
        "id": "row-1",
        "properties": {
            "topic_id": {"type": "title",
                         "title": [{"text": {"content": "T-URL"}}]},
            "原始来源": {"type": "url", "url": (
                "https://a.example.com/1，https://b.example.com/2、"
                "https://c.example.com/3；https://d.example.com/4 "
                "https://e.example.com/5")},
        },
    }
    card = _card_from_row(row)
    assert card["_source_urls"] == [
        f"https://{c}.example.com/{i}"
        for c, i in zip("abcde", range(1, 6))
    ]
