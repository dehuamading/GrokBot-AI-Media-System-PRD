"""评分 v3-dual 测试（mock 数据，无网络/LLM 调用）。

覆盖：9 维满分 18、传播潜力不计分、副业证据封顶、未经验证宣传=0
且输出无收益承诺字样、无评论机会型可被推荐、阈值、双轨合并两类都有。
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.analysis.audience import load_profile
from src.analysis.scoring import (SCORING_VERSION_V3, V3_ALTERNATIVE_TOTAL,
                                  V3_DIMS, V3_RECOMMEND_TOTAL,
                                  V3_TRACK_RECOMMEND_MIN, level_v3,
                                  merge_dual_tracks, score_item_v3)

REPO = Path(__file__).resolve().parents[1]
PROFILE = load_profile(REPO / "config" / "audience_profile.yaml")
NOW = datetime(2026, 11, 2, 12, tzinfo=timezone.utc)


def _item(text: str, **kw) -> dict:
    base = {
        "item_id": "mock:1",
        "source_type": "aihot",
        "source_url": "https://example.com/mock-source",
        "author_or_publisher": "mock",
        "published_at": NOW.isoformat(),
        "collected_at": NOW.isoformat(),
        "title_or_summary": text,
        "claims": [],
        "evidence_status": "verified",
        "processing_status": "new",
        "comments": [],
        "extra": {},
    }
    base.update(kw)
    return base


def _dims(sc: dict) -> dict[str, dict]:
    return {d["name"]: d for d in sc["dimensions"]}


def test_v3_has_nine_dimensions_and_max_18():
    sc = score_item_v3(_item("OpenAI 发布新模型，零基础教程，国内可用，免费开源，"
                             "手把手步骤，实测，新能力，降低门槛"),
                       now=NOW, profile=PROFILE)
    assert sc["version"] == SCORING_VERSION_V3 == "v3-dual"
    assert len(sc["dimensions"]) == 9
    assert [d["name"] for d in sc["dimensions"]] == V3_DIMS
    assert 0 <= sc["total"] <= 18
    assert sc["max_total"] == 18
    assert all(d["points"] in (0, 1, 2) for d in sc["dimensions"])
    assert sc["type"] in ("需求型", "机会型")


def test_propagation_hypothesis_is_not_scored():
    sc = score_item_v3(_item("首个免费开源工具，突破性新能力，国内可用"), now=NOW,
                       profile=PROFILE)
    assert all("传播" not in d["name"] for d in sc["dimensions"])
    assert sc["propagation_hypothesis"]["not_scored"] is True
    assert "假设" in sc["propagation_hypothesis"]["text"]
    blob = json.dumps(sc, ensure_ascii=False)
    assert "爆款预测" not in blob and "保证" not in blob


def test_side_hustle_unverified_promotion_scores_zero_without_income_promise():
    it = _item("AI 副业月入过万，稳赚不赔，保证收益，轻松躺赚",
               side_hustle_evidence="未经验证宣传")
    sc = score_item_v3(it, now=NOW, profile=PROFILE)
    dim = _dims(sc)["副业潜力"]
    assert dim["points"] == 0
    assert sc["side_hustle_evidence_level"] == "未经验证宣传"
    blob = json.dumps(sc, ensure_ascii=False)
    for bad in ("保证收益", "稳赚", "月入过万", "收益承诺", "爆款预测"):
        assert bad not in blob


def test_side_hustle_real_case_is_capped_at_two():
    it = _item("AI 副业真实案例：学员接单（个人经验分享）",
               side_hustle_evidence="真实案例")
    sc = score_item_v3(it, now=NOW, profile=PROFILE)
    assert _dims(sc)["副业潜力"]["points"] == 2


def test_opportunity_without_comments_can_be_recommended():
    it = _item("OpenAI 发布新模型，零基础教程，国内可用，免费开源，手把手步骤，"
               "实测，新能力，降低门槛")
    sc = score_item_v3(it, now=NOW, profile=PROFILE)
    assert sc["type"] == "机会型"
    assert sc["track"] == "机会型"
    assert sc["track_score"] >= V3_TRACK_RECOMMEND_MIN
    assert sc["level"] == "推荐"
    assert sc["total"] >= V3_RECOMMEND_TOTAL


def test_demand_track_score_uses_three_demand_dimensions():
    comments = [
        {"text": "国内能用吗？安装报错", "demand_tags": ["国内使用", "安装部署"],
         "source_url": "https://example.com/1"},
        {"text": "安装要多久", "demand_tags": ["安装部署"],
         "source_url": "https://example.com/2"},
        {"text": "怎么安装", "demand_tags": ["安装部署"],
         "source_url": "https://example.com/3"},
    ]
    it = _item("安装教程：国内可用，手把手步骤", comments=comments)
    sc = score_item_v3(it, now=NOW, profile=PROFILE)
    dims = _dims(sc)
    assert sc["type"] == "需求型"
    assert sc["track_score"] == (dims["真实用户价值"]["points"]
                                 + dims["可讲清与可验证"]["points"]
                                 + dims["国内适用性"]["points"])
    # ≥3 条同类评论原文 → 真实用户价值升档
    assert dims["真实用户价值"]["points"] >= 1


def test_thresholds_and_level_calibration():
    assert V3_RECOMMEND_TOTAL == 12
    assert V3_ALTERNATIVE_TOTAL == 9
    assert V3_TRACK_RECOMMEND_MIN == 3
    assert level_v3(12, 3) == "推荐"
    assert level_v3(12, 2) == "备选"  # 总分够但轨道分不足，不能推荐
    assert level_v3(9, 5) == "备选"
    assert level_v3(8, 5) == "观察"


def test_dual_track_merge_keeps_both_types():
    scored = [
        {"id": "d1", "type": "需求型", "track_score": 5, "total": 12},
        {"id": "d2", "type": "需求型", "track_score": 4, "total": 11},
        {"id": "o1", "type": "机会型", "track_score": 6, "total": 15},
        {"id": "o2", "type": "机会型", "track_score": 3, "total": 9},
    ]
    merged = merge_dual_tracks(scored, top_n=4)
    assert len(merged) == 4
    assert {x["type"] for x in merged} == {"需求型", "机会型"}
    assert [x["type"] for x in merged[:2]] == ["需求型", "机会型"]


def test_dual_track_merge_handles_empty_track():
    only_opp = [{"id": "o1", "type": "机会型", "track_score": 6, "total": 15}]
    assert [x["id"] for x in merge_dual_tracks(only_opp, top_n=5)] == ["o1"]


def test_only_active_rules_adjust_heuristic_ranking():
    it = _item("国内可用的 AI 副业教程，手把手步骤")
    base = score_item_v3(it, now=NOW, profile=PROFILE)
    positive = score_item_v3(
        it, now=NOW, profile=PROFILE,
        active_rules=["优先选择「国内可用」类选题"])
    negative = score_item_v3(
        it, now=NOW, profile=PROFILE,
        active_rules=["避免「国内可用」类选题"])
    assert positive["rule_adjustment"] >= 1
    assert negative["rule_adjustment"] <= -1
    assert positive["adjusted_total"] == base["total"] + positive["rule_adjustment"]
    assert base["rule_adjustment"] == 0
    # 认可/否决示例不传 active_rules 时不产生任何加减分
    assert score_item_v3(it, now=NOW, profile=PROFILE,
                         active_rules=[])["rule_adjustment"] == 0


# ---------------------------------------------------------------- 阶段3 审查修复
from src.analysis.scoring import _source_of  # noqa: E402


def _card(ident, track, *, source=None, level="观察", total=5, track_score=2):
    card = {"id": ident, "type": track, "track_score": track_score,
            "total": total, "推荐级别": level}
    if source:
        card["_source_group"] = source
    return card


def test_source_of_normalizes_x_from_group_and_source_type():
    assert _source_of({"_source_group": "x"}) == "x_posts"
    assert _source_of({"source_type": "x"}) == "x_posts"
    assert _source_of({"platform": "x"}) == "x_posts"
    assert _source_of({"_source_group": "aihot"}) == "aihot"


def test_merge_dual_tracks_top_n_2_keeps_at_least_one_demand_and_marks_overflow():
    cards = [
        _card("d1", "需求型", level="推荐", total=15, track_score=6),
        _card("d2", "需求型", level="推荐", total=14, track_score=5),
        _card("x1", "机会型", source="x_posts", level="推荐", total=15,
              track_score=2),
        _card("a1", "机会型", source="aihot", level="备选", total=11,
              track_score=5),
        _card("g1", "机会型", source="github", level="备选", total=10,
              track_score=4),
        _card("o1", "机会型", source="official", level="备选", total=9,
              track_score=3),
    ]
    merged = merge_dual_tracks(
        cards, top_n=2,
        opportunity_sources=("aihot", "github", "official"))

    assert len(merged) == 2
    assert any(c["type"] == "需求型" for c in merged)
    placed_sources = {c.get("_source_group") for c in merged}
    assert "aihot" in placed_sources
    overflow = merged[0].get("_merge_unplaced_mandatory")
    assert overflow
    assert set(overflow) == {"github", "official"}


def test_merge_dual_tracks_top_n_3_keeps_demand_when_mandatory_overflow():
    cards = [
        _card("d1", "需求型", level="推荐", total=15, track_score=6),
        _card("d2", "需求型", level="推荐", total=14, track_score=5),
        _card("a1", "机会型", source="aihot", level="备选", total=11,
              track_score=5),
        _card("g1", "机会型", source="github", level="备选", total=10,
              track_score=4),
        _card("o1", "机会型", source="official", level="备选", total=9,
              track_score=3),
    ]
    merged = merge_dual_tracks(
        cards, top_n=3,
        opportunity_sources=("aihot", "github", "official"))

    assert len(merged) == 3
    assert any(c["type"] == "需求型" for c in merged)
    assert {"aihot", "github"} <= {c.get("_source_group") for c in merged}
    assert merged[0].get("_merge_unplaced_mandatory") == ["official"]


def test_three_same_category_comments_strictly_raise_value_dimension():
    comments = [
        {"text": f"安装报错求助 {i}", "demand_tags": ["安装部署"],
         "source_url": f"https://example.com/{i}"}
        for i in range(3)
    ]
    baseline = score_item_v3(_item("安装教程"), now=NOW, profile=PROFILE)
    boosted = score_item_v3(_item("安装教程", comments=comments),
                            now=NOW, profile=PROFILE)
    base_dim = _dims(baseline)["真实用户价值"]
    boosted_dim = _dims(boosted)["真实用户价值"]
    assert base_dim["points"] < 2
    assert boosted_dim["points"] == base_dim["points"] + 1
    assert "升档" in boosted_dim["reason"]


def test_side_hustle_potential_and_personal_experience_capped_at_one():
    potential = score_item_v3(_item("AI 副业变现接单机会"),
                              now=NOW, profile=PROFILE)
    personal = score_item_v3(_item("我试过 AI 副业接单，分享个人经验"),
                             now=NOW, profile=PROFILE)
    assert _dims(potential)["副业潜力"]["points"] <= 1
    assert potential["side_hustle_evidence_level"] == "潜在机会"
    assert _dims(personal)["副业潜力"]["points"] <= 1
    assert personal["side_hustle_evidence_level"] in {"个人经验", "潜在机会"}


def test_china_usability_without_evidence_is_pending_and_scores_zero():
    sc = score_item_v3(_item("一个没有提到可用性的新工具"),
                       now=NOW, profile=PROFILE)
    assert _dims(sc)["国内适用性"]["points"] == 0
    assert sc["china_pending"] is True
    assert _dims(sc)["国内适用性"]["reason"].startswith("待验证")


def test_china_usability_question_sentence_is_pending_not_usable():
    for text in ("这个工具国内可用么？", "是不是需要梯子么？"):
        sc = score_item_v3(_item(text), now=NOW, profile=PROFILE)
        assert _dims(sc)["国内适用性"]["points"] == 0, text
        assert sc["china_pending"] is True, text


def test_opportunity_track_score_is_sum_of_three_opportunity_dimensions():
    sc = score_item_v3(
        _item("首个新模型发布，零基础教程，国内可用，免费开源，实测新能力"),
        now=NOW, profile=PROFILE)
    dims = _dims(sc)
    assert sc["type"] == "机会型"
    assert sc["track_score"] == (dims["新技术机会"]["points"]
                                 + dims["新颖与时效"]["points"]
                                 + dims["国内适用性"]["points"])
    assert sc["track_dimensions"] == ["新技术机会", "新颖与时效", "国内适用性"]
