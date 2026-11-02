"""国内统一导入接口测试：JSON/CSV、错误日志、去重、追溯、需求分类钩子。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.connectors.import_source import (FeishuAdapter, classify_comment_demand,
                                          load_import_file)

REPO = Path(__file__).resolve().parents[1]
FIXTURES = REPO / "tests" / "fixtures"
SAMPLE_JSON = FIXTURES / "import_sample_masked.json"
SAMPLE_CSV = FIXTURES / "import_sample_masked.csv"


def test_import_json_fixture_fields_demand_tags_and_traceability(tmp_path):
    out = load_import_file(SAMPLE_JSON, errors_path=tmp_path / "err.jsonl")
    assert out["errors"] == []
    assert len(out["items"]) >= 2
    it = out["items"][0]
    assert it["platform"] == "douyin"
    assert it["source_type"] == "import"
    assert it["origin_agent"] == "import"
    assert it["license_status"] in {"公开", "授权", "待确认"}
    assert it["comments"] and all("demand_tags" in c for c in it["comments"])
    assert it["extra"]["import"]["batch_id"] == out["batch_id"]
    assert it["extra"]["import"]["source_file"].endswith(".json")
    assert it["extra"]["import"]["row"] == 1


def test_missing_required_fields_are_logged_and_other_rows_continue(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"items": [
        {"platform": "douyin", "original_url": "https://example.com/ok",
         "title": "正常行（假数据）", "published_at": "2026-10-20T10:00:00Z",
         "authorization_status": "公开"},
        {"platform": "douyin", "original_url": "", "title": "",
         "published_at": "2026-10-20T10:00:00Z", "authorization_status": "公开"},
    ]}, ensure_ascii=False), encoding="utf-8")
    errlog = tmp_path / "import_errors.jsonl"
    out = load_import_file(bad, errors_path=errlog)
    assert len(out["items"]) == 1
    assert len(out["errors"]) == 1
    assert out["errors"][0]["row"] == 2
    lines = errlog.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["import_batch"] == out["batch_id"]
    assert "title" in rec["error"] or "original_url" in rec["error"]


def test_import_dedupes_by_url_and_title_hash_merging_comments(tmp_path):
    dup = tmp_path / "dup.json"
    dup.write_text(json.dumps({"items": [
        {"platform": "bilibili", "original_url": "https://example.com/v/1/",
         "title": "同一视频（假数据）", "comments": ["怎么安装"],
         "published_at": "2026-10-20T10:00:00Z", "authorization_status": "公开"},
        {"platform": "bilibili", "original_url": "https://example.com/v/1?utm_source=x",
         "title": "同一视频（假数据）", "comments": ["国内能用吗"],
         "published_at": "2026-10-20T10:00:00Z", "authorization_status": "公开"},
    ]}, ensure_ascii=False), encoding="utf-8")
    out = load_import_file(dup, errors_path=tmp_path / "err.jsonl")
    assert len(out["items"]) == 1
    assert out["dropped_duplicates"] == 1
    texts = [c["text"] for c in out["items"][0]["comments"]]
    assert texts == ["怎么安装", "国内能用吗"]


def test_csv_comments_pipe_and_multirow_same_url_merge(tmp_path):
    out = load_import_file(SAMPLE_CSV, errors_path=tmp_path / "err.jsonl")
    assert out["errors"] == []
    assert len(out["items"]) == 2
    first = out["items"][0]
    assert first["platform"] == "xiaohongshu"
    texts = [c["text"] for c in first["comments"]]
    assert "怎么部署" in texts and "有没有免费替代" in texts and "会报错怎么办" in texts
    # 第二行同 URL 只补充评论，不产生重复素材
    assert out["dropped_duplicates"] == 1


def test_classify_comment_demand_stub_categories():
    tags = classify_comment_demand("国内能用吗？有没有免费替代，安装报错怎么办")
    assert "国内使用" in tags
    assert "免费替代" in tags
    assert "安装部署" in tags or "故障排查" in tags
    assert "AI副业赚钱" in classify_comment_demand("想靠 AI 副业赚钱，怎么接单")


def test_feishu_adapter_is_interface_only():
    adapter = FeishuAdapter()
    with pytest.raises(NotImplementedError):
        adapter.fetch_records()
    with pytest.raises(NotImplementedError):
        adapter.map_record({})
