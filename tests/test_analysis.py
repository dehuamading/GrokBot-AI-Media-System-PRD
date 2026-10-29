"""分析层测试：分类 / 评分 / 选题卡（全部离线，假数据）。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.analysis.classify import classify_item
from src.analysis.scoring import SCORING_VERSION, score_item
from src.analysis.topics import build_topic_cards
from src.connectors.aihot_source import load_from_file

FAKE = Path(__file__).resolve().parents[1] / "data" / "examples" / "aihot_fake_sample.json"
NOW = datetime(2026, 10, 30, tzinfo=timezone.utc)


def _materials():
    return load_from_file(FAKE)  # 8 条假数据


# ---------------------------------------------------------------- 分类
def test_classify_covers_all_types():
    mats = {m["item_id"]: m for m in _materials()}
    got = set()
    for m in mats.values():
        ctype, reason = classify_item(m)
        assert reason  # 每次分类必须给理由
        got.add(ctype)
    assert {"工具教程", "安全事件"} <= got  # 假数据设计保证这两类必命中
    assert got <= {"工具教程", "产品发布", "行业动态", "研究解读", "观点评论", "安全事件"}


def test_classify_fallback():
    item = {"title_or_summary": "某公司发了个东西", "claims": [], "extra": {}}
    ctype, reason = classify_item(item)
    assert ctype == "行业动态" and "回退" in reason


# ---------------------------------------------------------------- 评分
def test_score_dimensions_and_range():
    for m in _materials():
        sc = score_item(m, now=NOW)
        assert len(sc["dimensions"]) == 7
        assert 0 <= sc["total"] <= 14
        assert sc["level"] in ("推荐", "备选", "观察")
        assert sc["version"] == SCORING_VERSION
        assert all(d["grade"] in ("高", "中", "低") for d in sc["dimensions"])
        # 传播潜力维度必须显式标注为假设
        spread = next(d for d in sc["dimensions"] if d["name"] == "传播潜力假设")
        assert "假设" in spread["reason"]


def test_score_evidence_mapping():
    m = _materials()[0]
    m["evidence_status"] = "verified"
    assert score_item(m, now=NOW)["dimensions"][1]["points"] == 2
    m["evidence_status"] = "unverified"
    assert score_item(m, now=NOW)["dimensions"][1]["points"] == 0


def test_score_freshness():
    m = _materials()[1]
    m["published_at"] = (NOW - timedelta(days=30)).isoformat()
    fresh = score_item(m, now=NOW)["dimensions"][3]
    assert fresh["points"] == 0 and "7 天" in fresh["reason"]


def test_score_deterministic():
    m = _materials()[0]
    assert score_item(m, now=NOW) == score_item(m, now=NOW)


# ---------------------------------------------------------------- 选题卡
def test_build_topic_cards_full_fields_and_order():
    cards, details = build_topic_cards(_materials(), top_k=8, now=NOW)
    assert 5 <= len(cards) <= 10
    totals = [d["score"]["total"] for d in details[:len(cards)]]
    assert totals == sorted(totals, reverse=True)  # 按总分降序
    ids = {m["item_id"] for m in _materials()}
    for c in cards:
        for f in ("topic_id", "拟定标题", "建议平台", "面向谁", "核心价值", "为什么现在",
                  "关联素材", "需求证据", "可信度与风险", "最强反证", "差异与难度",
                  "推荐级别", "推荐理由"):
            assert c.get(f), f"{c['topic_id']} 缺 {f}"
        assert set(c["关联素材"]) <= ids  # 引用的素材必须真实存在
        assert c["需求证据"].startswith("待验证")  # 无直接证据时禁止编造
        assert c["用户选择结果"] == "待用户选择"
    assert len({c["topic_id"] for c in cards}) == len(cards)  # 编号唯一


def test_build_topic_cards_topk_cap_10():
    materials = _materials() * 3  # 24 条（含重复，会被选题引擎照常处理）
    cards, _ = build_topic_cards(materials, top_k=99, now=NOW)
    assert len(cards) <= 10


def test_build_topic_cards_small_pool_marks_insufficient():
    cards, _ = build_topic_cards(_materials()[:2], now=NOW)
    assert len(cards) == 2  # 不足 5 张时原样返回，由调用方标记 insufficient
