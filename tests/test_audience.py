"""受众画像测试（任务05）：加载 / 信号匹配 / 评分接入（全部离线）。"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.analysis.audience import (AudienceProfile, load_profile,
                                   match_audience)
from src.analysis.scoring import SCORING_VERSION, score_item
from src.analysis.topics import build_topic_cards
from src.connectors.aihot_source import load_from_file

REPO = Path(__file__).resolve().parents[1]
PROFILE_PATH = REPO / "config" / "audience_profile.yaml"
FAKE = REPO / "data" / "examples" / "aihot_fake_sample.json"
NOW = datetime(2026, 10, 30, tzinfo=timezone.utc)


def _mini_profile(**kw) -> AudienceProfile:
    base = dict(profile_id="test-p1", name="测试画像", description="测试用迷你画像",
                high_signals=["教程", "短视频"], mid_signals=["ai"],
                negative_signals=["空投", "election"])
    base.update(kw)
    return AudienceProfile(**base)


# ---------------------------------------------------------------- 加载
def test_load_profile_from_repo_config():
    prof = load_profile(PROFILE_PATH)
    assert prof.profile_id == "audience-zh-ai-creator-v1"
    assert "AI 短视频" in prof.name or "AI" in prof.name
    assert prof.high_signals and prof.negative_signals  # 评分信号必须已配置
    assert "翻墙" in prof.description or "访问限制" in prof.description
    # 配置文件本身必须能被 yaml 解析且含任务书四要素
    raw = yaml.safe_load(PROFILE_PATH.read_text(encoding="utf-8"))
    for k in ("interests", "pain_points", "signals"):
        assert k in raw


def test_load_profile_errors(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_profile(tmp_path / "nope.yaml")
    bad = tmp_path / "bad.yaml"
    bad.write_text("name: x\ndescription: y\n", encoding="utf-8")  # 缺 profile_id
    with pytest.raises(ValueError):
        load_profile(bad)


# ---------------------------------------------------------------- 信号匹配
def test_match_audience_levels():
    prof = _mini_profile()
    assert match_audience("零基础短视频教程来了", prof)[0] == 2      # 高信号
    assert match_audience("某 ai 工具发布", prof)[0] == 1           # 仅中信号
    assert match_audience("今天天气不错", prof)[0] == 0             # 无命中
    pts, reasons, neg = match_audience("今晚空投撸毛冲", prof)        # 负面 → 0
    assert pts == 0 and neg and "受众无关" in reasons[0]


def test_match_audience_case_insensitive():
    prof = _mini_profile()
    assert match_audience("Big ELECTION night", prof)[2]  # 英文负面词小写匹配


# ---------------------------------------------------------------- 评分接入
def _aihot_item():
    return load_from_file(FAKE)[0]


def test_score_item_with_profile_marks_version_and_audience_dim():
    prof = _mini_profile()
    sc = score_item(_aihot_item(), now=NOW, profile=prof)
    assert sc["version"] == f"{SCORING_VERSION}+{prof.profile_id}"
    aud = next(d for d in sc["dimensions"] if d["name"] == "受众匹配")
    assert any("画像" in r for r in [aud["reason"]])


def test_score_item_negative_signal_lowers_total():
    """受众无关内容（加密货币/撸毛/政治）必须低分（任务05 硬要求）。"""
    prof = load_profile(PROFILE_PATH)
    good = {"item_id": "t:1", "source_type": "manual", "source_url": "https://example.com/a",
            "author_or_publisher": "x", "published_at": NOW.isoformat(),
            "collected_at": NOW.isoformat(), "title_or_summary": "AI 短视频零基础教程实测",
            "claims": ["手把手教程免费工具"], "evidence_status": "self_reported",
            "processing_status": "new", "extra": {}}
    bad = dict(good, item_id="t:2",
               title_or_summary="今晚空投撸毛质押送代币教程",
               claims=["空投撸毛教程"])
    sc_good = score_item(good, now=NOW, profile=prof)
    sc_bad = score_item(bad, now=NOW, profile=prof)
    assert sc_good["total"] >= 7
    assert sc_bad["total"] <= 4
    assert "受众无关" in sc_bad["notes"]


def test_score_item_without_profile_keeps_v1_behavior():
    sc = score_item(_aihot_item(), now=NOW)
    assert sc["version"] == SCORING_VERSION
    assert "受众无关" not in sc["notes"]


def test_build_topic_cards_passes_profile():
    prof = load_profile(PROFILE_PATH)
    mats = load_from_file(FAKE)
    cards, details = build_topic_cards(mats, top_k=8, now=NOW, profile=prof)
    assert 5 <= len(cards) <= 10
    assert all(d["score"]["version"].endswith(prof.profile_id) for d in details)
