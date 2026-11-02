"""X 帖子文件输入接口测试（任务05）：本地 JSON、不联网、假数据。"""

from __future__ import annotations

import json
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


# ---------------------------------------------------------------- X API 原样格式
def _write_json(tmp_path: Path, name: str, obj) -> Path:
    p = tmp_path / name
    p.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
    return p


def test_load_x_posts_x_api_raw_maps_author_and_metrics(tmp_path):
    """X API 原样格式：data[] + includes.users、author_id、public_metrics。"""
    payload = {
        "data": [{
            "id": "api-1001",
            "text": "手把手教程：零基础用 AI 做短视频（假数据）",
            "created_at": "2026-10-30T02:00:00.000Z",
            "author_id": "u-1",
            "lang": "zh",
            "conversation_id": "api-1001",
            "public_metrics": {
                "like_count": 11, "retweet_count": 2, "reply_count": 3,
                "quote_count": 4, "impression_count": 500,
            },
            "entities": {
                "hashtags": [{"tag": "AI"}],
                "urls": [{"expanded_url": "https://example.com/a"}],
            },
        }],
        "includes": {"users": [
            {"id": "u-1", "username": "example_api_user", "name": "API示例"},
        ]},
    }
    p = load_x_posts(_write_json(tmp_path, "api.json", payload))[0]
    assert "(@example_api_user)" in p["author_or_publisher"]
    assert "API示例" in p["author_or_publisher"]
    assert p["metrics"] == {"likes": 11, "reposts": 2, "replies": 3,
                            "quotes": 4, "views": 500}
    assert p["extra"]["likes"] == 11
    assert p["extra"]["impressions"] == 500
    assert p["platform"] == "x"
    assert p["origin_agent"] == "grokbot"
    assert p["license_status"] == "公开"
    assert p["keywords"] == ["AI"]
    # X API 原样格式：username 与 id 齐备时构造可追溯 URL，并显式标注
    assert p["source_url"] == "https://x.com/example_api_user/status/api-1001"
    assert p["extra"]["url_constructed"] is True
    assert p["errors"] == []
    assert validate_item(p) == []


def test_load_x_posts_with_replies_keeps_text_not_author(tmp_path):
    """每帖 replies：保留回复原文/计数/父 id，不保留回复作者用户名。"""
    payload = {"posts": [{
        "id": "p-1", "url": "https://x.com/example/status/p-1",
        "author_username": "example", "author_name": "示例",
        "created_at": "2026-10-30T02:00:00.000Z", "text": "主帖（假数据）",
        "replies": [
            {"id": "r-1", "text": "求教程，我也不会（假数据）",
             "created_at": "2026-10-30T03:00:00.000Z",
             "author_username": "private_replier", "lang": "zh",
             "metrics": {"likes": 5, "reposts": 1, "replies": 0, "quotes": 0},
             "in_reply_to": "p-1"},
            {"id": "r-2", "text": "国内能用吗？（假数据）",
             "created_at": "2026-10-30T03:10:00.000Z",
             "author_username": "private_replier2", "lang": "zh",
             "public_metrics": {"like_count": 2, "retweet_count": 0,
                                "reply_count": 0, "quote_count": 0},
             "referenced_tweets": [{"type": "replied_to", "id": "p-1"}]},
        ],
    }]}
    p = load_x_posts(_write_json(tmp_path, "replies.json", payload))[0]
    assert len(p["comments"]) == 2
    assert p["comments"][0]["text"] == "求教程，我也不会（假数据）"
    assert p["comments"][0]["parent_id"] == "p-1"
    assert p["comments"][0]["metrics"]["likes"] == 5
    assert p["comments"][1]["metrics"]["likes"] == 2
    assert "author_username" not in p["comments"][0]
    assert "private_replier" not in json.dumps(p["comments"], ensure_ascii=False)
    assert p["user_problem_evidence"] == ["求教程，我也不会（假数据）",
                                           "国内能用吗？（假数据）"]


def test_top_level_replies_attach_by_conversation_or_parent(tmp_path):
    payload = {
        "posts": [
            {"id": "p-1", "url": "https://x.com/example/status/p-1",
             "author_username": "a", "created_at": "2026-10-30T02:00:00Z",
             "text": "帖子一（假数据）", "conversation_id": "conv-1"},
            {"id": "p-2", "url": "https://x.com/example/status/p-2",
             "author_username": "b", "created_at": "2026-10-30T02:00:00Z",
             "text": "帖子二（假数据）"},
        ],
        "replies": [
            {"id": "tr-1", "text": "挂在 p-1 的回复（假数据）",
             "created_at": "2026-10-30T03:00:00Z", "conversation_id": "conv-1",
             "public_metrics": {"like_count": 1, "retweet_count": 0,
                                "reply_count": 0, "quote_count": 0}},
            {"id": "tr-2", "text": "挂在 p-2 的回复（假数据）",
             "created_at": "2026-10-30T03:00:00Z", "in_reply_to": "p-2"},
        ],
    }
    posts = load_x_posts(_write_json(tmp_path, "top_replies.json", payload))
    by_id = {p["item_id"]: p for p in posts}
    assert by_id["x_posts:p-1"]["comments"][0]["text"].startswith("挂在 p-1")
    assert by_id["x_posts:p-2"]["comments"][0]["text"].startswith("挂在 p-2")


def test_reply_entry_in_posts_does_not_overwrite_root_conversation(tmp_path):
    payload = {
        "posts": [
            {"id": "p-1", "url": "https://x.com/example/status/p-1",
             "author_username": "root_user",
             "created_at": "2026-10-30T02:00:00Z",
             "text": "根帖（假数据）", "conversation_id": "conv-1"},
            {"id": "p-2", "url": "https://x.com/example/status/p-2",
             "author_username": "reply_user",
             "created_at": "2026-10-30T03:00:00Z",
             "text": "帖内回复条目（假数据）", "conversation_id": "conv-1",
             "in_reply_to": "p-1"},
        ],
        "replies": [
            {"id": "tr-1", "text": "顶层回复（假数据）",
             "created_at": "2026-10-30T04:00:00Z", "conversation_id": "conv-1",
             "author_username": "secret_replier"},
        ],
    }
    posts = load_x_posts(_write_json(tmp_path, "root_guard.json", payload))
    by_id = {p["item_id"]: p for p in posts}
    assert by_id["x_posts:p-1"]["comments"][0]["text"] == "顶层回复（假数据）"
    assert by_id["x_posts:p-2"]["comments"] == []
    blob = json.dumps(posts, ensure_ascii=False)
    assert "secret_replier" not in blob


def test_orphan_top_level_reply_is_counted_without_usernames(tmp_path):
    payload = {
        "posts": [{"id": "p-1", "url": "https://x.com/example/status/p-1",
                   "author_username": "root_user",
                   "created_at": "2026-10-30T02:00:00Z",
                   "text": "根帖（假数据）"}],
        "replies": [
            {"id": "tr-1", "text": "孤儿回复一（假数据）",
             "created_at": "2026-10-30T03:00:00Z",
             "conversation_id": "orphan-conv",
             "author_username": "orphan_user_one"},
            {"id": "tr-2", "text": "孤儿回复二（假数据）",
             "created_at": "2026-10-30T03:10:00Z",
             "in_reply_to": "missing-post",
             "author_username": "orphan_user_two"},
        ],
    }
    posts = load_x_posts(_write_json(tmp_path, "orphans.json", payload))
    assert posts[0]["comments"] == []
    assert posts.errors and "2" in posts.errors[0]
    blob = json.dumps(posts.errors, ensure_ascii=False)
    assert "orphan_user" not in blob


def test_bad_x_api_and_reply_data_raise(tmp_path):
    with pytest.raises(ValueError):
        load_x_posts(_write_json(tmp_path, "bad_api.json", {
            "data": [{"text": "缺 id 和 created_at（假数据）"}],
            "includes": {"users": []}}))
    with pytest.raises(ValueError):
        load_x_posts(_write_json(tmp_path, "bad_replies.json", {
            "posts": [{"id": "p-1", "url": "https://x.com/example/status/p-1",
                       "text": "x（假数据）", "created_at": "2026-10-30T02:00:00Z"}],
            "replies": "not-a-list"}))
    with pytest.raises(ValueError):
        load_x_posts(_write_json(tmp_path, "bad_reply_item.json", {
            "data": [{"id": "p-1", "text": "x（假数据）",
                      "created_at": "2026-10-30T02:00:00Z", "author_id": "u-1",
                      "replies": ["not-an-object"]}],
            "includes": {"users": [{"id": "u-1", "username": "u"}]}}))
