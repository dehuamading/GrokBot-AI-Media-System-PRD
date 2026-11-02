"""来源校验与跨来源去重（阶段2-B4）：URL 规范化、来源合并、非法素材标记。"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.validate import dedupe, parse_datetime, validate_item


def _item(**kw):
    base = {
        "item_id": kw.pop("item_id", "github:o/r"),
        "source_type": kw.pop("source_type", "github"),
        "source_url": "https://github.com/o/r",
        "platform": "github",
        "published_at": "2026-10-30T10:00:00Z",
        "license_status": "公开",
        "title_or_summary": "示例仓库（假数据）",
    }
    base.update(kw)
    return base


def test_validate_valid_item_marks_selectable():
    it = _item()
    assert validate_item(it) == []
    assert it["eligible_for_topic"] is True
    assert it["validation_errors"] == []


def test_missing_source_url_is_error_and_not_selectable():
    it = _item(source_url="")
    errs = validate_item(it)
    assert any("source_url" in e for e in errs)
    assert it["eligible_for_topic"] is False
    assert any("source_url" in e for e in it["errors"])


def test_published_at_must_be_parseable():
    assert parse_datetime("2026-10-30T10:00:00Z") is not None
    assert parse_datetime("2026-10-30") is not None
    bad = _item(published_at="不是日期")
    assert any("published_at" in e for e in validate_item(bad))
    missing = _item(published_at="")
    assert any("published_at" in e for e in validate_item(missing))


def test_platform_and_license_are_required():
    assert any("platform" in e for e in validate_item(_item(platform="myspace")))
    assert any("license_status" in e for e in validate_item(_item(license_status="")))
    assert validate_item(_item(platform="xiaohongshu", source_type="import")) == []


def test_license_status_starting_with_public_is_allowed():
    it = _item(license_status="公开+许可证:MIT")
    assert validate_item(it) == []
    assert it["eligible_for_topic"] is True


def test_dedupe_normalizes_scheme_trailing_slash_and_tracking():
    a = _item(item_id="x:1", source_type="aihot", platform="aihot",
              source_url="https://Example.com/post/?utm_source=rss")
    b = _item(item_id="x:2", source_type="official", platform="official",
              source_url="http://example.com/post")
    kept, dropped = dedupe([a, b])
    assert len(kept) == 1
    assert len(dropped) == 1
    assert dropped[0]["duplicate_of"] == "x:1"


def test_dedupe_same_github_repo_merges_sources_instead_of_dropping_data():
    gh = _item(item_id="github:o/r", source_type="github",
               source_url="https://github.com/o/r", platform="github")
    official = _item(item_id="official:rel-1", source_type="official",
                     platform="official",
                     source_url="https://github.com/o/r/releases/tag/v1.0")
    official["extra"] = {"official": {"source_name": "示例发布源"}}
    kept, dropped = dedupe([gh, official])
    assert len(kept) == 1
    assert len(dropped) == 1
    assert dropped[0]["merged"] is True
    merged_sources = kept[0]["sources"]
    assert {s["source_type"] for s in merged_sources} == {"github", "official"}
    assert kept[0]["extra"]["official"]["source_name"] == "示例发布源"


def test_dedupe_keeps_distinct_items():
    a = _item(item_id="a", source_url="https://example.com/a")
    b = _item(item_id="b", source_url="https://example.com/b")
    kept, dropped = dedupe([a, b])
    assert len(kept) == 2 and dropped == []


def test_multiple_official_releases_same_repo_are_not_collapsed():
    rels = []
    for tag in ("v1.0.0", "v1.1.0", "v1.2.0"):
        rels.append(_item(
            item_id=f"official:{tag}", source_type="official", platform="official",
            source_url=f"https://github.com/o/r/releases/tag/{tag}",
            title_or_summary=f"r {tag}"))
    kept, dropped = dedupe(rels)
    assert len(kept) == 3 and dropped == []
