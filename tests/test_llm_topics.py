"""GLM 分析路径测试（任务05）：mock 聊天客户端 + 硬校验（绝无真实网络调用）。"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.analysis.audience import load_profile
from src.analysis.llm_topics import (glm_build_cards, glm_score_posts,
                                     shortlist)
from src.analysis.topics import validate_topic_cards
from src.connectors.x_posts_source import load_x_posts
from src.llm.glm import GLMChat, GLMNotConfigured, parse_json_loose
from src.notion_store.audience_page import ensure_audience_page
from src.notion_store.bootstrap import ensure_kb, ensure_kb_upgrades
from src.workflows import pipeline
from tests.fakes import FakeNotionClient

REPO = Path(__file__).resolve().parents[1]
XPOSTS = REPO / "data" / "examples" / "x_posts_fake_sample.json"
NOW = datetime(2026, 10, 30, 12, tzinfo=timezone.utc)
PROFILE = load_profile(REPO / "config" / "audience_profile.yaml")


class FakeGLMChat:
    """与 GLMChat 同接口的假客户端：按预设脚本返回，绝不联网。"""

    model = "glm-fake"

    def __init__(self, score_response=None, card_response=None):
        self.calls = 0
        self._score = score_response or {"results": [
            {"n": 1, "score": 8, "reason": "AI教程直接命中受众"},
            {"n": 2, "score": 7, "reason": "vibe coding 教程适合零基础"},
            {"n": 3, "score": 1, "reason": "空投撸毛与受众无关"},
            {"n": 4, "score": 0, "reason": "外国选举无关"},
            {"n": 5, "score": 8, "reason": "国内平替测评正中痛点"},
        ]}
        self._cards = card_response or {"cards": [
            {"title": "零基础用AI做第一条短视频",
             "why_fit": "受众想做AI短视频，教程可照做",
             "hook": "别再收藏夹吃灰，三小时出你第一条AI视频",
             "refs": [1], "china": "工具国内可直接用，待核实：免费额度",
             "risk": "免费工具可能改版", "counter": "同类教程已很多，缺乏差异",
             "difficulty": "低，录屏即可", "platform": "抖音", "score": 8.5},
            {"title": "国内AI剪辑平替测评",
             "why_fit": "受众受访问限制困扰，正需要平替",
             "hook": "不用翻墙，三款国产AI剪辑工具实测",
             "refs": [5], "china": "全部国内可用", "risk": "测评时效性",
             "counter": "测评主观，工具更新快", "difficulty": "中，需实测",
             "platform": "两者", "score": 8.0},
        ]}

    def chat_json(self, system, user, **kw):  # noqa: ARG002
        self.calls += 1
        return self._cards if "选题卡" in system else self._score


# ---------------------------------------------------------------- 基础
def test_glm_chat_requires_env(monkeypatch):
    for k in ("GLM_API_BASE", "GLM_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    with pytest.raises(GLMNotConfigured):
        GLMChat()


def test_parse_json_loose_fences_and_noise():
    assert parse_json_loose('```json\n{"a": 1}\n```') == {"a": 1}
    assert parse_json_loose('前置说明 {"a": {"b": 2}} 后置说明') == {"a": {"b": 2}}
    with pytest.raises(Exception):
        parse_json_loose("完全没有 JSON")


# ---------------------------------------------------------------- 打分
def test_glm_score_posts_maps_and_clamps():
    posts = load_x_posts(XPOSTS)[:5]
    scores = glm_score_posts(FakeGLMChat(), posts, PROFILE)
    assert scores["x_posts:fake-9001"]["score"] == 8
    assert scores["x_posts:fake-9003"]["score"] <= 2  # 受众无关低分
    assert "理由" in scores["x_posts:fake-9001"]["reason"] or \
        scores["x_posts:fake-9001"]["reason"]


def test_glm_score_posts_skips_invalid_entries():
    chat = FakeGLMChat(score_response={"results": [
        {"n": 99, "score": 10, "reason": "越界 n"},
        {"n": "x", "score": 5, "reason": "非法 n"},
        {"n": 1, "score": 99, "reason": "超界分→截断"}]})
    scores = glm_score_posts(chat, load_x_posts(XPOSTS)[:3], PROFILE)
    assert set(scores) == {"x_posts:fake-9001"}
    assert scores["x_posts:fake-9001"]["score"] == 10  # clamp 到 [0,10]


# ---------------------------------------------------------------- 选题卡
def test_glm_build_cards_assembles_and_validates():
    posts = load_x_posts(XPOSTS)
    chat = FakeGLMChat()
    cards, meta = glm_build_cards(chat, posts, PROFILE, now=NOW)
    assert len(cards) == 2 and meta["insufficient"]  # <5 → 标记不足
    material_ids = {p["item_id"] for p in posts}
    validate_topic_cards(cards, material_ids, strict=False)  # PRD 字段全通过
    for c in cards:
        for f in ("拟定标题", "切入角度与钩子", "为什么适合", "国内可用性",
                  "引用帖子", "综合评分", "最强反证", "推荐级别"):
            assert c.get(f), f"{c['topic_id']} 缺 {f}"
        # 链接必须原样来自素材记录（文件 url 字段），不得来自 LLM
        for url in c["_source_urls"]:
            assert url.startswith("https://x.com/example_") and url in \
                {p["source_url"] for p in posts}
        assert c["topic_id"].startswith("TX-20261030-")
        assert c["用户选择结果"] == "待用户选择"


def test_glm_build_cards_drops_bad_refs_and_missing_fields():
    chat = FakeGLMChat(card_response={"cards": [
        {"title": "引用不存在帖子", "why_fit": "w", "hook": "h", "refs": [99],
         "china": "c", "risk": "r", "counter": "x", "score": 9},      # refs 全非法→丢
        {"title": "缺字段", "why_fit": "w", "refs": [1], "china": "c",
         "score": 9},                                                  # 缺 hook→丢
        {"title": "好卡", "why_fit": "w", "hook": "h", "refs": [1, 2],
         "china": "c", "risk": "r", "counter": "x", "score": 7},
        {"title": "好卡", "why_fit": "w", "hook": "h", "refs": [1],
         "china": "c", "risk": "r", "counter": "x", "score": 6},       # 标题重复→丢
    ]})
    cards, meta = glm_build_cards(chat, load_x_posts(XPOSTS), PROFILE, now=NOW)
    assert len(cards) == 1 and len(meta["dropped"]) == 3
    assert cards[0]["关联素材"] == ["x_posts:fake-9001", "x_posts:fake-9002"]


def test_glm_build_cards_caps_at_max_cards():
    raw = [{"title": f"标题{i}", "why_fit": "w", "hook": "h", "refs": [1],
            "china": "c", "risk": "r", "counter": "x", "score": 9 - i * 0.1}
           for i in range(10)]
    chat = FakeGLMChat(card_response={"cards": raw})
    cards, _ = glm_build_cards(chat, load_x_posts(XPOSTS), PROFILE, now=NOW,
                               max_cards=8)
    assert len(cards) <= 8


# ---------------------------------------------------------------- Notion 接入
def test_step_write_topics_only_referenced():
    posts = load_x_posts(XPOSTS)
    chat = FakeGLMChat()
    cards, _ = glm_build_cards(chat, posts, PROFILE, now=NOW)
    client = FakeNotionClient()
    db_ids = ensure_kb(client, "root")
    added = ensure_kb_upgrades(client, db_ids)
    # 全新建库已含新列（spec 内置），升级无需新增 → 结构两条路径一致
    assert added["03_选题决策"] == []
    props = set(client.databases["03_选题决策"]["properties"])
    assert {"为什么适合", "切入角度与钩子", "国内可用性", "引用帖子", "综合评分"} <= props
    w = pipeline.step_write_topics(client, db_ids, cards, posts,
                                   only_referenced=True)
    # 只写被引用素材（2 张卡各引 1 条，共 2 条），不是全部 7 条
    assert w["materials_written"] == 2 and w["materials_total"] == 2
    assert len(client.databases["01_原始素材"]["rows"]) == 2
    # 03 行：新列写入、状态待用户选择
    row = client.row("03_选题决策", "topic_id", cards[0]["topic_id"])
    assert FakeNotionClient.plain(row["properties"]["用户选择结果"]) == "待用户选择"
    assert FakeNotionClient.plain(row["properties"]["引用帖子"]).startswith("示例")
    assert FakeNotionClient.plain(row["properties"]["综合评分"]) >= 0


def test_step_write_topics_writes_all_when_not_only_referenced():
    posts = load_x_posts(XPOSTS)
    chat = FakeGLMChat()
    cards, _ = glm_build_cards(chat, posts, PROFILE, now=NOW)
    client = FakeNotionClient()
    db_ids = ensure_kb(client, "root")
    ensure_kb_upgrades(client, db_ids)
    w = pipeline.step_write_topics(client, db_ids, cards, posts,
                                   only_referenced=False)
    assert w["materials_written"] == len(posts)  # 兼容旧行为：全量写


def test_select_option_upgrade_only_adds():
    client = FakeNotionClient()
    db_ids = ensure_kb(client, "root")
    db = client.databases["01_原始素材"]
    before = [o["name"] for o in db["properties"]["source_type"]["select"]["options"]]
    assert "x_posts" in before  # spec 已内置（任务05）
    # 对既有库只增新选项（如未来扩展），保留既有全部选项，且幂等
    added = client.ensure_select_options(db["id"], "source_type", ["future_opt"])
    assert added == ["future_opt"]
    after = [o["name"] for o in db["properties"]["source_type"]["select"]["options"]]
    assert set(before) < set(after) and "future_opt" in after  # 只增不删
    assert client.ensure_select_options(db["id"], "source_type", ["future_opt"]) == []  # 幂等


def test_ensure_audience_page_idempotent():
    client = FakeNotionClient()
    p1, created1 = ensure_audience_page(client, "root", PROFILE)
    assert created1 is True
    p2, created2 = ensure_audience_page(client, "root", PROFILE)
    assert created2 is False and p1 == p2  # 二次调用不新建不改动
    page = client.pages[p1]
    body = json.dumps(page["children"], ensure_ascii=False)
    assert PROFILE.name in body and "config/audience_profile.yaml" in body
    assert "痛点" in body and "受众匹配" in body  # 含评分用法说明


def test_shortlist_feeds_glm_pipeline_end_to_end_offline():
    """预筛→打分→出卡：全链路离线（FakeGLM），受众无关帖不出现在任何卡引用里。"""
    posts = load_x_posts(XPOSTS)
    short = shortlist(posts, k=6, profile=PROFILE)
    chat = FakeGLMChat()
    scores = glm_score_posts(chat, short, PROFILE)
    cards, _ = glm_build_cards(chat, short, PROFILE, now=NOW, glm_scores=scores)
    cited = {iid for c in cards for iid in c["关联素材"]}
    assert "x_posts:fake-9003" not in cited and "x_posts:fake-9004" not in cited
