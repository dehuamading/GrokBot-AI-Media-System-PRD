"""来源校验与跨来源去重（阶段2-B4）。

- validate_item：source_url 必填、published_at 可解析、platform 合法、
  license_status 存在；错误写入 item.errors，并标记 eligible_for_topic=False。
- dedupe：URL 规范化（去跟踪参数/尾斜杠、忽略 http/https）跨来源去重；
  同一 GitHub 仓库被 GitHub 连接器与官方 Releases 同时命中时合并 sources，
  不丢弃数据。
"""

from __future__ import annotations

import hashlib
import re
from datetime import date, datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit, urlunsplit

from src.normalize import normalize_url

VALID_PLATFORMS = {
    "x", "aihot", "github", "official",
    "douyin", "bilibili", "xiaohongshu", "other",
}
VALID_LICENSE_STATUS = {"公开", "授权", "待确认"}
DOMESTIC_PLATFORMS = {"douyin", "bilibili", "xiaohongshu"}


def parse_datetime(value) -> datetime | None:
    """解析 ISO-8601 / RFC-822 / 日期字符串；无法解析返回 None。"""
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=timezone.utc)
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        pass
    try:
        return parsedate_to_datetime(text)
    except (TypeError, ValueError, OverflowError):
        pass
    try:
        return datetime.fromisoformat(text[:10])
    except ValueError:
        return None


def canonical_url(url: str) -> str:
    """规范化 URL 并忽略 http/https 差异（用于跨来源去重）。"""
    normalized = normalize_url(url)
    if not normalized:
        return ""
    parts = urlsplit(normalized)
    return urlunsplit(("", parts.netloc.lower(), parts.path, parts.query, ""))


def _github_repo_info(item: dict) -> tuple[str, str]:
    """返回 (owner/repo, release_key)。非 GitHub / 非 release 的 release_key 为空。"""
    github = ((item.get("extra") or {}).get("github") or {})
    full_name = github.get("full_name")
    parts = urlsplit(item.get("source_url") or "")
    segments = [s for s in parts.path.split("/") if s]
    if not full_name:
        if parts.netloc.lower() not in {"github.com", "www.github.com"}:
            return "", ""
        if len(segments) < 2:
            return "", ""
        repo = segments[1][:-4] if segments[1].endswith(".git") else segments[1]
        full_name = f"{segments[0]}/{repo}"
    repo_key = str(full_name).lower()
    release_key = ""
    if len(segments) >= 3 and segments[2] == "releases":
        release_key = "/".join(segments[2:])
    return repo_key, release_key


def _title_key(item: dict) -> tuple[str, str] | None:
    platform = str(item.get("platform") or "")
    if platform not in DOMESTIC_PLATFORMS and item.get("source_type") != "import":
        return None
    title = re.sub(r"\s+", "", str(item.get("title_or_summary") or "").lower())
    if not title:
        return None
    return platform, hashlib.sha1(title.encode("utf-8")).hexdigest()


def validate_item(item: dict) -> list[str]:
    """校验单条素材并就地标记；返回错误列表（空列表=可进入选题）。"""
    errors: list[str] = []
    source_url = str(item.get("source_url") or "").strip()
    if not source_url:
        errors.append("缺少 source_url（来源不可追溯，不入选题）")

    published = item.get("published_at")
    if published in (None, ""):
        errors.append("缺少 published_at（无法判断时效）")
    elif parse_datetime(published) is None:
        errors.append(f"published_at 无法解析: {published}")

    platform = item.get("platform")
    if platform not in VALID_PLATFORMS:
        errors.append(f"非法 platform: {platform}（允许 {sorted(VALID_PLATFORMS)}）")

    license_status = item.get("license_status")
    if not license_status:
        errors.append("缺少 license_status")
    elif license_status not in VALID_LICENSE_STATUS:
        errors.append(f"非法 license_status: {license_status}")

    item["validation_errors"] = list(errors)
    item["eligible_for_topic"] = not errors
    merged_errors = list(item.get("errors") or [])
    for err in errors:
        if err not in merged_errors:
            merged_errors.append(err)
    item["errors"] = merged_errors
    return errors


def _source_entry(item: dict) -> dict:
    return {
        "item_id": item.get("item_id"),
        "source_type": item.get("source_type"),
        "source_url": item.get("source_url"),
        "platform": item.get("platform"),
    }


def _merge_missing(target: dict, source: dict) -> None:
    for key, value in (source or {}).items():
        if key not in target or target[key] in (None, "", [], {}):
            target[key] = value
        elif isinstance(target[key], dict) and isinstance(value, dict):
            _merge_missing(target[key], value)


def _merge_into(existing: dict, incoming: dict) -> None:
    if "sources" not in existing:
        existing["sources"] = [_source_entry(existing)]
    entry = _source_entry(incoming)
    if entry not in existing["sources"]:
        existing["sources"].append(entry)

    comments = existing.setdefault("comments", [])
    seen = {(c.get("text"), c.get("created_at")) for c in comments
            if isinstance(c, dict)}
    for comment in incoming.get("comments") or []:
        key = (comment.get("text"), comment.get("created_at"))
        if key not in seen:
            comments.append(comment)
            seen.add(key)

    for kw in incoming.get("keywords") or []:
        if kw not in existing.setdefault("keywords", []):
            existing["keywords"].append(kw)

    for metric, value in (incoming.get("metrics") or {}).items():
        old = existing.setdefault("metrics", {}).get(metric)
        if isinstance(value, (int, float)) and (
                not isinstance(old, (int, float)) or value > old):
            existing["metrics"][metric] = value
        elif metric not in existing["metrics"]:
            existing["metrics"][metric] = value

    _merge_missing(existing.setdefault("extra", {}), incoming.get("extra") or {})
    existing["merged_duplicate_items"] = existing.get(
        "merged_duplicate_items", []) + [incoming.get("item_id")]


def dedupe(items: list[dict]) -> tuple[list[dict], list[dict]]:
    """跨来源去重；返回 (kept, dropped_report)。

    dropped_report 每条含 item_id / duplicate_of / reason / merged。
    """
    kept: list[dict] = []
    by_id: dict[str, dict] = {}
    by_url: dict[str, dict] = {}
    by_repo_root: dict[str, dict] = {}
    by_repo_any: dict[str, dict] = {}
    by_release: dict[tuple[str, str], dict] = {}
    by_title: dict[tuple[str, str], dict] = {}
    dropped: list[dict] = []

    def has_official_source(item: dict) -> bool:
        sources = item.get("sources") or [_source_entry(item)]
        return any(s.get("source_type") == "official" for s in sources)

    def find(item: dict) -> tuple[dict | None, str]:
        iid = str(item.get("item_id") or "")
        if iid and iid in by_id:
            return by_id[iid], "item_id 重复"
        repo, release_key = _github_repo_info(item)
        if repo and release_key and (repo, release_key) in by_release:
            return by_release[(repo, release_key)], "同一 GitHub Release 重复"
        if repo:
            root = by_repo_root.get(repo)
            if root is not None:
                # GitHub 仓库根条目与官方 Releases 跨来源合并；同源多条
                # release 不彼此吞并（保留每个版本），只合并第一条跨来源记录。
                if not release_key or not has_official_source(root):
                    return root, "同一 GitHub 仓库（跨来源合并 sources）"
            if not release_key and repo in by_repo_any:
                return by_repo_any[repo], "同一 GitHub 仓库（跨来源合并 sources）"
        url = canonical_url(item.get("source_url") or "")
        if url and url in by_url:
            return by_url[url], "URL 规范化后重复（去跟踪参数/尾斜杠/http 差异）"
        title_key = _title_key(item)
        if title_key and title_key in by_title:
            return by_title[title_key], "标题哈希+平台重复"
        return None, ""

    def register(item: dict, value: dict) -> None:
        iid = str(item.get("item_id") or "")
        if iid:
            by_id[iid] = value
        repo, release_key = _github_repo_info(item)
        if repo:
            by_repo_any.setdefault(repo, value)
            if release_key:
                by_release[(repo, release_key)] = value
            else:
                by_repo_root[repo] = value
        url = canonical_url(item.get("source_url") or "")
        if url:
            by_url[url] = value
        title_key = _title_key(item)
        if title_key:
            by_title[title_key] = value

    for item in items:
        existing, reason = find(item)
        if existing is None:
            kept.append(item)
            register(item, item)
            continue
        _merge_into(existing, item)
        register(item, existing)
        dropped.append({
            "item_id": item.get("item_id"),
            "duplicate_of": existing.get("item_id"),
            "reason": reason,
            "merged": True,
        })
    return kept, dropped
