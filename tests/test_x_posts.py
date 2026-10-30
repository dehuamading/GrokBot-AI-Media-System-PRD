"""X 帖子文件输入接口测试（任务05）：本地 JSON、不联网、假数据。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.analysis.audience import load_profile
from src.analysis.llm_topics import shortlist
from src.connectors.x_posts_source import load_x_posts
from src.normalize import dedupe, validate_item
from src.workflows import pipeline
from src.workflows.state import WorkflowState

REPO = Path(__file__).resolve().parents[1]
XPOSTS = REPO / "data" / "examples" / "x_posts_fake_sample.json"
AIHOT = REPO / "data" / "examples" / "aihot_fake_sample.json"
XF = REPO / "data" / "examples" / "x_following.example.json"


# ---------------------------------------------------------------- 接口本身
def test_x_posts_module_has_no_network_code():
    """红线：X 帖子连接器必须纯本地（不得 import requests/urllib）。"""
    import src.connectors.x_posts_source as m
    src_text = Path(m.__file__).read_text(encoding="utf-8")
    assert "requests" not in src_text and "urllib" not in src_text
    assert not hasattr(m, "requests")


def test_load_x_posts_normalizes_all_fields():
    posts = load_x_posts(XPOSTS)
    assert len(posts) == 7
    p = posts[0]
    assert p["item_id"] == "x_posts:fake-9001"
    assert p["source_type"] == "x_posts"
    assert p["source_url"] == "https://x.com/example_ai_teacher/status/9001"  # url 原样
    assert "(@example_ai_teacher)" in p["author_or_publisher"]
    assert p["evidence_status"] == "self_reported"  # 帖子=作者自述，未交叉核验
    assert p["extra"]["likes"] == 320 and p["extra"]["lang"] == "zh"
    assert validate_item(p) == []


def test_load_x_posts_errors(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_x_posts(tmp_path / "missing.json")
    bad_type = tmp_path / "bad1.json"
    bad_type.write_text('{"data": []}', encoding="utf-8")  # 无 posts 键的 dict
    with pytest.raises(ValueError):
        load_x_posts(bad_type)
    bad_field = tmp_path / "bad2.json"
    bad_field.write_text('[{"id": "1", "url": "", "text": "x", "created_at": "t"}]',
                         encoding="utf-8")  # 缺 url
    with pytest.raises(ValueError):
        load_x_posts(bad_field)


def test_dedupe_x_posts_by_id_and_url(tmp_path):
    f = tmp_path / "dup.json"
    f.write_text(
        '[{"id":"1","url":"https://x.com/a/status/1","text":"aa bb cc dd",'
        '"created_at":"2026-10-30T00:00:00Z","author_username":"a"},'
        '{"id":"1","url":"https://x.com/a/status/1","text":"aa bb cc dd",'
        '"created_at":"2026-10-30T00:00:00Z","author_username":"a"}]',
        encoding="utf-8")
    out, dropped = dedupe(load_x_posts(f))
    assert len(out) == 1 and len(dropped) == 1


# ---------------------------------------------------------------- 预筛
def test_shortlist_filters_irrelevant_and_short():
    prof = load_profile(REPO / "config" / "audience_profile.yaml")
    posts = load_x_posts(XPOSTS)
    short = shortlist(posts, k=5, profile=prof)
    ids = [p["item_id"] for p in short]
    assert "x_posts:fake-9003" not in ids      # 空投撸毛 → 负面出局
    assert "x_posts:fake-9004" not in ids      # 外国选举 → 负面出局
    assert "x_posts:fake-9006" not in ids      # 过短帖 → 跳过
    assert "x_posts:fake-9001" in ids          # AI 短视频教程 → 入选
    assert len(short) <= 5


def test_shortlist_respects_language_of_profile():
    prof = load_profile(REPO / "config" / "audience_profile.yaml")
    posts = load_x_posts(XPOSTS)
    for p in shortlist(posts, k=99, profile=prof):
        lang = (p.get("extra") or {}).get("lang")
        assert lang in (None, "zh", "en")  # 画像语言外的（ja 等）被过滤


# ---------------------------------------------------------------- 工作流接入
def test_step_collect_merges_x_posts_with_aihot(tmp_path):
    state = WorkflowState(tmp_path / "s.json")
    out = pipeline.step_collect(state, aihot_file=AIHOT, x_following_file=XF,
                                x_posts_file=XPOSTS, snapshot_dir=tmp_path / "raw")
    assert len(out["materials"]) == 8 + 7  # aihot 假样例 8 条 + X 帖子 7 条
    assert out["x_posts"] == 7
    assert {m["source_type"] for m in out["materials"]} >= {"aihot", "x_posts"}
    assert state.get("last_collect")["x_posts"] == 7
    assert state.stage == "collected"


def test_step_collect_without_x_posts_backward_compatible(tmp_path):
    """旧调用（无 --x-posts）行为不变。"""
    state = WorkflowState(tmp_path / "s.json")
    out = pipeline.step_collect(state, aihot_file=AIHOT, x_following_file=XF,
                                snapshot_dir=tmp_path / "raw")
    assert len(out["materials"]) == 8 and out["x_posts"] == 0
