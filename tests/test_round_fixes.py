"""阶段3 真实 topics-round 暴露问题的回归测试（全部 mock/本地，无真实网络与密钥）。

覆盖：
1) topics-round 默认真实拉取；显式 --aihot-file 才回放；--write-notion 拒绝 fake/example 素材；
2) 最终推荐级别只按 v3-dual，LLM 高分不得越权；
3) 推荐理由使用实际 provider/model，不写死 GLM；
4) 新字段从旧字段回退，并按 5 类受众/国内可用口径归一；
5) 01 中 X 素材只写 ≤40 字概括，不写帖子正文/@handle；
6) --repair-round / --repair-ids 只更新本轮 topic_id 的 03 行并标记模拟样例。
"""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.analysis.audience import load_profile
from src.analysis.llm_topics import glm_build_cards
from src.analysis.scoring import level_v3, score_item_v3
from src.notion_store.bootstrap import ensure_kb, ensure_kb_upgrades
from src.workflows import pipeline, run
from tests.fakes import FakeNotionClient
from tests.test_topics_round import FakeRoundChat, _write_fixtures

REPO = Path(__file__).resolve().parents[1]
PROFILE = load_profile(REPO / "config" / "audience_profile.yaml")
NOW = datetime(2026, 11, 2, 12, tzinfo=timezone.utc)
MARKER = "[模拟样例·请忽略]"
FAKE_AIHOT = REPO / "data" / "examples" / "aihot_fake_sample.json"


def _material(item_id, source_type, text, *, url=None, platform=None,
              author="示例来源"):
    return {
        "item_id": item_id,
        "source_type": source_type,
        "platform": platform or ("x" if source_type == "x_posts" else source_type),
        "source_url": url or f"https://aihot.news/items/{item_id.split(':')[-1]}",
        "author_or_publisher": author,
        "published_at": NOW.isoformat(),
        "collected_at": NOW.isoformat(),
        "title_or_summary": text,
        "claims": [],
        "evidence_status": "self_reported" if source_type == "x_posts" else "verified",
        "processing_status": "new",
        "comments": [],
        "extra": {},
    }


def _args(tmp_path, **overrides):
    base = dict(
        state=str(tmp_path / "state.json"), page_id="root",
        sources="aihot,github,official,x", x_file=None,
        x_replies_file=None, aihot_file=None, import_file=None,
        github_top=5, aihot_limit=20, official_limit=3, max_cards=10,
        round_id="ROUND-FIX", write_notion=False, dry_run=True, live=False,
        config=str(REPO / "config" / "sources.example.yaml"),
        audience=str(REPO / "config" / "audience_profile.yaml"),
        shortlist=40, analyze="heuristic", x_following=None,
        repair_round=None, repair_ids=None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


# --------------------------------------------------------------------- 1) 来源
def test_topics_round_parser_defaults_to_live_and_no_fake_aihot_file(monkeypatch):
    captured = {}

    def fake_cmd(args):
        captured.update(vars(args))
        return 0

    monkeypatch.setattr(run, "cmd_topics_round", fake_cmd)
    monkeypatch.setattr(sys, "argv", ["run", "topics-round"])
    assert run.main() == 0
    assert captured["live"] is True
    assert captured["aihot_file"] is None


def test_explicit_aihot_file_is_replay_and_no_live(monkeypatch):
    captured = {}

    def fake_cmd(args):
        captured.update(vars(args))
        return 0

    monkeypatch.setattr(run, "cmd_topics_round", fake_cmd)
    monkeypatch.setattr(sys, "argv", [
        "run", "topics-round", "--aihot-file", "sample.json"])
    assert run.main() == 0
    assert captured["aihot_file"] == "sample.json"
    assert captured["live"] is False


def test_topics_round_write_notion_rejects_fake_or_example_sources(
        tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("NOTION_TOKEN", raising=False)
    # client=None 模拟 CLI 真实写入路径；测试注入 FakeNotion 的路径不受影响。
    args = _args(tmp_path, sources="aihot", aihot_file=str(FAKE_AIHOT),
                 live=False, analyze="heuristic", dry_run=False,
                 write_notion=True)
    assert run.cmd_topics_round(args, client=None, chat=None) == 1
    out = capsys.readouterr().out
    assert "拒绝" in out and ("fake" in out.lower() or "example" in out.lower())


def test_topics_round_guard_still_applies_when_token_creates_client(
        tmp_path, monkeypatch, capsys):
    # CLI 未注入 client；即使为读偏好创建了 NotionClient，也不能绕过 fake 护栏。
    fake = FakeNotionClient()
    monkeypatch.setenv("NOTION_TOKEN", "test-token-not-real")
    monkeypatch.setattr(run, "NotionClient", lambda: fake)
    args = _args(tmp_path, sources="aihot", aihot_file=str(FAKE_AIHOT),
                 live=False, analyze="heuristic", dry_run=False,
                 write_notion=True)
    assert run.cmd_topics_round(args, client=None, chat=None) == 1
    assert "拒绝写入" in capsys.readouterr().out


def test_topics_round_dry_run_with_fake_source_is_allowed(tmp_path, monkeypatch):
    monkeypatch.delenv("NOTION_TOKEN", raising=False)
    args = _args(tmp_path, sources="aihot", aihot_file=str(FAKE_AIHOT),
                 live=False, analyze="heuristic", dry_run=True,
                 write_notion=True)
    assert run.cmd_topics_round(args, client=None, chat=None) == 0


# ------------------------------------------------------------------ 2/3) 级别/文案
def _card_for(mat, **extra):
    card = {
        "关联素材": [mat["item_id"]],
        "_intended_type": "机会型",
        "拟定标题": "机会型｜AI 新能力",
        "核心价值": "讲清新能力",
        "为什么适合": "受众需要新工具",
        "为什么现在": "刚刚发布",
        "切入角度与钩子": "三分钟上手",
        "国内可用性": "需要梯子，外币卡订阅",
        "国内可用": "待验证",
        "是否付费": "待验证",
        "能否实际验证": "待验证",
        "面向谁": "想做 AI 短视频的内容创作者",
        "面向用户": "待验证",
        "痛点或机会": "待验证",
        "为何现在做": "待验证",
        "切入角度": "待验证",
        "_llm_score": 9.5,
        "_llm_service": "opencode-go/deepseek-v4.1-flash",
        "生成引擎": "LLM",
    }
    card.update(extra)
    return card


def test_high_llm_score_never_overrides_v3_recommendation_level():
    mat = _material("aihot:new-model", "aihot",
                    "OpenAI 发布新模型 V04，新能力值得关注")
    card = _card_for(mat)
    v3 = score_item_v3(mat, now=NOW, profile=PROFILE)
    assert v3["adjusted_total"] < 12
    merged = run._postprocess_round_cards(
        [card], [mat], now=NOW, profile=PROFILE, max_cards=5)
    assert merged
    got = merged[0]
    assert got["推荐级别"] == level_v3(v3["adjusted_total"],
                                       v3["adjusted_track_score"])
    assert got["推荐级别"] != "推荐"
    assert got["综合评分"] == v3["adjusted_total"]
    assert "18" in got["推荐理由"]
    assert "9.5/10" in got["推荐理由"]


def test_recommendation_reason_uses_actual_provider_model_not_hardcoded_glm():
    mat = _material("x_posts:1", "x_posts",
                    "国内可用的免费开源 AI 教程，零基础手把手步骤，实测新能力降低门槛",
                    url="https://x.com/example_ai/status/1",
                    author="Example (@example_ai)")
    chat = FakeRoundChat()
    cards, _ = glm_build_cards(chat, [mat], PROFILE, now=NOW)
    assert cards
    for card in cards:
        reason = card["推荐理由"]
        assert "mock/mock-round" in reason
        assert "GLM 综合评分" not in reason


def test_card_prompt_requests_new_fields():
    mat = _material("x_posts:1", "x_posts",
                    "国内可用的免费开源 AI 教程，零基础手把手步骤")
    chat = FakeRoundChat()
    glm_build_cards(chat, [mat], PROFILE, now=NOW)
    system = next(s for s, _ in chat.seen if "选题卡" in s)
    for field in ("target_user", "pain_or_opportunity", "why_now", "angle",
                  "paid", "verifiable"):
        assert field in system


class NewFieldsChat(FakeRoundChat):
    def chat_json(self, system, user, **kw):  # noqa: ARG002
        self.calls += 1
        self.seen.append((system, user))
        if "选题卡" in system:
            return {"cards": [{
                "title": "真实卡片", "why_fit": "旧为什么适合", "hook": "旧钩子",
                "refs": [1], "china": "需要梯子", "risk": "风险",
                "counter": "反证", "difficulty": "低", "platform": "两者",
                "score": 7, "target_user": "AI副业与创业探索者",
                "pain_or_opportunity": "LLM 痛点", "why_now": "LLM 时机",
                "angle": "LLM 角度", "paid": "需付费", "verifiable": "可验证",
            }]}
        return {"results": [{"n": 1, "score": 7, "reason": "mock"}]}


def test_llm_new_field_outputs_are_written_into_card():
    mat = _material("x_posts:1", "x_posts",
                    "国内可用的免费开源 AI 教程，零基础手把手步骤")
    chat = NewFieldsChat()
    cards, _ = glm_build_cards(chat, [mat], PROFILE, now=NOW)
    assert cards
    card = cards[0]
    assert "AI副业与创业探索者" in card["面向用户"]
    assert card["痛点或机会"] == "LLM 痛点"
    assert card["为何现在做"] == "LLM 时机"
    assert card["切入角度"] == "LLM 角度"
    assert card["是否付费"] == "需付费"
    assert card["能否实际验证"] == "可验证"
    assert card["国内可用"].startswith("需梯子")


# --------------------------------------------------------------------- 4) 新字段
def test_new_fields_fall_back_to_old_fields_and_normalize_labels():
    mat = _material("aihot:real-1", "aihot",
                    "AI 短视频教程，免费开源，国内可用")
    card = _card_for(mat)
    merged = run._postprocess_round_cards(
        [card], [mat], now=NOW, profile=PROFILE, max_cards=5)
    assert merged
    c = merged[0]
    assert c["面向用户"] != "待验证"
    assert any(name in c["面向用户"] for name in (
        "AI普通使用者", "AI内容创作者", "AI编程与应用开发者",
        "AI副业与创业探索者", "国外AI工具的国内使用者"))
    assert c["痛点或机会"] == "受众需要新工具"
    assert c["为何现在做"] == "刚刚发布"
    assert c["切入角度"] == "三分钟上手"
    assert c["国内可用"].startswith("需")
    assert any(c["国内可用"].startswith(x) for x in (
        "可用", "需梯子", "需外币卡", "不可用", "待验证"))


# --------------------------------------------------------------------- 5) 01
def test_topics_write_01_x_title_is_short_summary_without_handle_or_raw_text():
    raw = ("这是一段超过二十个字的X帖子正文原文，@alice 在正文里详细写了"
           "安装步骤和国内可用性，但这段全文不应进入 01 的标题字段。")
    mat = _material("x_posts:real-1", "x_posts", raw,
                    url="https://x.com/example_ai/status/1",
                    author="Example (@example_ai)")
    card = {
        "topic_id": "TX-20261102-V01",
        "拟定标题": "AI内容创作者｜教程实测",
        "建议平台": "两者", "面向谁": "内容创作者", "核心价值": "v",
        "为什么现在": "n", "需求证据": "待验证：无直接评论证据",
        "可信度与风险": "r", "最强反证": "c", "差异与难度": "d",
        "推荐级别": "观察", "推荐理由": "v3 总分 1/18，轨道分 0",
        "关联素材": [mat["item_id"]], "原始来源": mat["source_url"],
        "_source_urls": [mat["source_url"]], "选题类型": "机会型",
    }
    client = FakeNotionClient()
    db_ids = ensure_kb(client, "root")
    ensure_kb_upgrades(client, db_ids)
    pipeline.step_write_topics(client, db_ids, [card], [mat])

    row = client.row("01_原始素材", "item_id", mat["item_id"])
    title = FakeNotionClient.plain(row["properties"]["title_or_summary"])
    assert title and len(title) <= 40
    assert raw[:20] not in title
    assert "@alice" not in title and "example_ai" not in title
    assert FakeNotionClient.plain(row["properties"]["source_url"]) == mat["source_url"]


# --------------------------------------------------------------------- 6) repair
def _seed_round_and_other_rows(client, db_ids, ids):
    for i, tid in enumerate(ids, start=1):
        client.upsert_row(db_ids["03_选题决策"], "topic_id", tid, {
            "拟定标题": {"rich_text": [{"text": {"content": f"旧假卡{i}"}}]},
            "关联素材": {"rich_text": [{"text": {"content": f"aihot:fake-00{i}"}}]},
            "原始来源": {"url": "https://example.com/fake"},
            "用户选择结果": {"select": {"name": "待用户选择"}},
        })
        client.upsert_row(db_ids["01_原始素材"], "item_id", f"aihot:fake-00{i}", {
            "source_url": {"url": "https://example.com/fake"},
            "title_or_summary": {"rich_text": [{"text": {"content": f"假素材{i}"}}]},
        })
    client.upsert_row(db_ids["03_选题决策"], "topic_id", "TX-OTHER-1", {
        "拟定标题": {"rich_text": [{"text": {"content": "其他轮次卡，不应改动"}}]},
        "关联素材": {"rich_text": [{"text": {"content": "aihot:real-old"}}]},
        "原始来源": {"url": "https://aihot.news/items/real-old"},
        "用户选择结果": {"select": {"name": "已选定"}},
    })
    client.upsert_row(db_ids["01_原始素材"], "item_id", "aihot:real-old", {
        "source_url": {"url": "https://aihot.news/items/real-old"},
        "title_or_summary": {"rich_text": [{"text": {"content": "真实旧素材"}}]},
    })


def test_repair_round_updates_only_round_topic_ids_and_preserves_user_choice(
        tmp_path, monkeypatch):
    client = FakeNotionClient()
    db_ids = ensure_kb(client, "root")
    ensure_kb_upgrades(client, db_ids)
    ids = ["TX-20261102-V01", "TX-20261102-V02"]
    _seed_round_and_other_rows(client, db_ids, ids)
    # 用户已对本轮其中一张做出选择，repair 必须保留
    client.set_select("03_选题决策", "topic_id", ids[0], "用户选择结果", "已选定")

    state_dir = Path(tmp_path)
    round_file = state_dir / "topics_round_ROUND-REPAIR.json"
    round_file.write_text(json.dumps({
        "round_id": "ROUND-REPAIR",
        "topic_ids": ids,
        "cards": [{"topic_id": tid} for tid in ids],
        "material_ids": ["aihot:fake-001", "aihot:fake-002"],
        "materials": [
            {"item_id": "aihot:fake-001", "source_url": "https://example.com/fake"},
            {"item_id": "aihot:fake-002", "source_url": "https://example.org/fake"},
        ],
    }, ensure_ascii=False), encoding="utf-8")

    real = [
        _material("aihot:real-1", "aihot",
                  "OpenAI 发布真实新模型，零基础教程国内可用"),
        _material("x_posts:real-2", "x_posts",
                  "真实 X 帖子：AI 副业接单实测，国内可用",
                  url="https://x.com/example_real_user/status/2"),
    ]
    out = {
        "materials": real, "dropped": [], "snapshot": None, "x_posts": 1,
        "per_source": {"aihot": 1, "x": 1},
        "validation_error_counts": {}, "source_errors": {},
        "external_calls": {"aihot": 1, "x": 0}, "sources": ["aihot", "x"],
    }
    monkeypatch.setattr(run.pipeline, "step_collect", lambda *a, **kw: out)

    args = _args(tmp_path, round_id=None, repair_round="ROUND-REPAIR",
                 analyze="heuristic", dry_run=False, write_notion=True)
    assert run.cmd_topics_round_repair(args, client=client,
                                       chat=None, now=NOW) == 0

    # 只更新本轮 topic_id；其他行原样
    other = client.row("03_选题决策", "topic_id", "TX-OTHER-1")
    assert FakeNotionClient.plain(other["properties"]["拟定标题"]) == "其他轮次卡，不应改动"
    assert FakeNotionClient.plain(other["properties"]["用户选择结果"]) == "已选定"

    for tid in ids:
        row = client.row("03_选题决策", "topic_id", tid)
        title = FakeNotionClient.plain(row["properties"]["拟定标题"])
        linked = FakeNotionClient.plain(row["properties"]["关联素材"])
        assert title.startswith(MARKER)
        assert "fake" not in linked and "example.com" not in linked
        assert "real-" in linked
    # 用户选择结果保持原值
    assert FakeNotionClient.plain(
        client.row("03_选题决策", "topic_id", ids[0])["properties"]["用户选择结果"]
    ) == "已选定"
    # 本轮 fake 01 行加标记；其他轮真实行不动
    for iid in ("aihot:fake-001", "aihot:fake-002"):
        row = client.row("01_原始素材", "item_id", iid)
        assert FakeNotionClient.plain(
            row["properties"]["title_or_summary"]).startswith(MARKER)
    other_mat = client.row("01_原始素材", "item_id", "aihot:real-old")
    assert not FakeNotionClient.plain(
        other_mat["properties"]["title_or_summary"]).startswith(MARKER)
    # 不新增/删除行
    assert len(client.query_database(db_ids["03_选题决策"])) == 3
    assert len(client.query_database(db_ids["01_原始素材"])) == 3


def test_repair_ids_work_without_local_round_file(tmp_path, monkeypatch):
    client = FakeNotionClient()
    db_ids = ensure_kb(client, "root")
    ensure_kb_upgrades(client, db_ids)
    ids = ["TX-20261102-V09"]
    _seed_round_and_other_rows(client, db_ids, ids)

    real = [_material("aihot:real-9", "aihot",
                      "真实新模型发布，零基础教程国内可用，免费开源")]
    out = {
        "materials": real, "dropped": [], "snapshot": None, "x_posts": 0,
        "per_source": {"aihot": 1}, "validation_error_counts": {},
        "source_errors": {}, "external_calls": {"aihot": 1},
        "sources": ["aihot"],
    }
    monkeypatch.setattr(run.pipeline, "step_collect", lambda *a, **kw: out)
    args = _args(tmp_path, repair_round=None,
                 repair_ids=",".join(ids), analyze="heuristic",
                 dry_run=False, write_notion=True)
    assert run.cmd_topics_round_repair(args, client=client,
                                       chat=None, now=NOW) == 0
    row = client.row("03_选题决策", "topic_id", ids[0])
    assert "real-9" in FakeNotionClient.plain(row["properties"]["关联素材"])
    assert FakeNotionClient.plain(
        row["properties"]["拟定标题"]).startswith(MARKER)

def test_repair_ids_marks_example_domain_rows_even_without_fake_in_item_id(
        tmp_path, monkeypatch):
    client = FakeNotionClient()
    db_ids = ensure_kb(client, "root")
    ensure_kb_upgrades(client, db_ids)
    tid = "TX-20261102-V08"
    client.upsert_row(db_ids["03_选题决策"], "topic_id", tid, {
        "拟定标题": {"rich_text": [{"text": {"content": "旧卡"}}]},
        "关联素材": {"rich_text": [{"text": {"content": "aihot:legacy-8"}}]},
        "原始来源": {"url": "https://aihot.news/items/legacy-8"},
        "用户选择结果": {"select": {"name": "已选定"}},
    })
    client.upsert_row(db_ids["01_原始素材"], "item_id", "aihot:legacy-8", {
        "source_url": {"url": "https://example.org/legacy-8"},
        "title_or_summary": {"rich_text": [{"text": {"content": "旧素材"}}]},
    })
    real = [_material("aihot:real-8", "aihot",
                      "真实新模型发布，零基础教程国内可用，免费开源")]
    out = {
        "materials": real, "dropped": [], "snapshot": None, "x_posts": 0,
        "per_source": {"aihot": 1}, "validation_error_counts": {},
        "source_errors": {}, "external_calls": {"aihot": 1},
        "sources": ["aihot"],
    }
    monkeypatch.setattr(run.pipeline, "step_collect", lambda *a, **kw: out)
    args = _args(tmp_path, repair_ids=tid, analyze="heuristic",
                 dry_run=False, write_notion=True)
    assert run.cmd_topics_round_repair(args, client=client,
                                       chat=None, now=NOW) == 0
    assert FakeNotionClient.plain(
        client.row("03_选题决策", "topic_id", tid)["properties"]["拟定标题"]
    ).startswith(MARKER)
    assert FakeNotionClient.plain(
        client.row("01_原始素材", "item_id", "aihot:legacy-8")[
            "properties"]["title_or_summary"]).startswith(MARKER)
    assert FakeNotionClient.plain(
        client.row("03_选题决策", "topic_id", tid)[
            "properties"]["用户选择结果"]) == "已选定"
