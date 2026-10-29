"""单元测试（无网络、无密钥）：标准化/去重/初筛/选题卡校验。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.normalize import (dedupe, make_item_id, normalize_aihot_item,
                           normalize_url, normalize_x_account, screen_score,
                           validate_item)


def _sample_aihot_raw():
    return {
        "id": "abc123",
        "title": "OpenRouter 教程：用 LLM 从 PDF 中抽取表格",
        "summary": "把 PDF 连同 JSON schema 发给模型",
        "source": {"name": "OpenRouter：Announcements（RSS）"},
        "links": {"aihot": "https://aihot.news/items/abc123",
                  "original": "https://openrouter.ai/blog/tutorials/x/?utm_source=rss"},
        "publishedAt": "2026-10-29T10:00:00.000Z",
        "category": "tip",
        "score": 76,
    }


# ---------------------------------------------------------------- URL 规范化
def test_normalize_url_strips_tracking_and_slash():
    assert normalize_url("https://OpenRouter.ai/blog/x/?utm_source=rss&ref=x") == \
        "https://openrouter.ai/blog/x"
    assert normalize_url("  https://a.com/p/1/#frag  ") == "https://a.com/p/1"
    assert normalize_url("") == ""


def test_make_item_id():
    assert make_item_id("aihot", "abc") == "aihot:abc"


# ---------------------------------------------------------------- aihot 归一化
def test_normalize_aihot_item_fields():
    it = normalize_aihot_item(_sample_aihot_raw())
    assert it["item_id"] == "aihot:abc123"
    assert it["source_type"] == "aihot"
    assert it["source_url"] == "https://openrouter.ai/blog/tutorials/x"  # 已去utm参数与尾斜杠
    assert it["evidence_status"] in {"verified", "self_reported", "third_party", "unverified"}
    assert it["processing_status"] == "new"
    assert it["user_problem_evidence"] == []  # 无编造需求证据
    assert validate_item(it) == []


def test_normalize_aihot_item_official_source_is_verified():
    raw = _sample_aihot_raw()
    raw["source"]["name"] = "OpenAI：官网动态（RSS）"
    assert normalize_aihot_item(raw)["evidence_status"] == "verified"


# ---------------------------------------------------------------- X 账号
def test_normalize_x_account_marks_content_gap():
    acc = normalize_x_account({"username": "a", "name": "A", "description": "d",
                               "followers": 10})
    assert acc["item_id"] == "x_following:a"
    assert "不可得" in acc["content_gap"]  # 显式缺口标注


# ---------------------------------------------------------------- 去重
def test_dedupe_by_id_and_url():
    a = normalize_aihot_item(_sample_aihot_raw())
    b = normalize_aihot_item(_sample_aihot_raw())          # 同 id 同 url
    c = normalize_aihot_item(_sample_aihot_raw())
    c["id"] = "xyz789"                                     # 不同 id，相同 url → 仍应去重
    out, dropped = dedupe([a, b, c])
    assert len(out) == 1 and len(dropped) == 2


# ---------------------------------------------------------------- 初筛（可解释）
def test_screen_score_explainable():
    it = normalize_aihot_item(_sample_aihot_raw())
    score, reasons = screen_score(it)
    assert 0 <= score <= 10
    assert any("教程" in r or "类目" in r for r in reasons)
    assert screen_score({"title_or_summary": "xx", "claims": [], "extra": {}}) == (0, [])


# ---------------------------------------------------------------- 选题卡校验（PRD 5.4）
def _valid_card(item_id):
    return {"topic_id": "T01", "拟定标题": "t", "建议平台": "抖音", "面向谁": "a",
            "核心价值": "v", "为什么现在": "n", "关联素材": [item_id],
            "需求证据": "待验证：无直接证据", "可信度与风险": "r", "最强反证": "c",
            "差异与难度": "d", "推荐级别": "推荐", "推荐理由": "y"}


def test_topic_card_rejects_unknown_material_and_bad_count():
    from src.analysis.topics import validate_topic_cards
    with pytest.raises(ValueError):
        validate_topic_cards([_valid_card("aihot:not-exist")], {"aihot:x"})
    cards = [_valid_card("aihot:x") for _ in range(4)]
    with pytest.raises(ValueError):
        validate_topic_cards(cards, {"aihot:x"})
    # 需求证据不诚实（编造很多人都在问）应拒
    bad = _valid_card("aihot:x")
    bad["需求证据"] = "很多人都在问这个"
    with pytest.raises(ValueError):
        validate_topic_cards([bad] * 5, {"aihot:x"})
