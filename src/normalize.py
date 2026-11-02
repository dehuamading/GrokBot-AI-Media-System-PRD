"""标准化层：统一素材格式、URL 规范化、去重、可解释初筛。

对应 PRD 第 4 节（统一素材格式与证据约束）与第 5.3 节（候选选题判断维度）。
本模块不含任何网络请求，纯函数，便于单元测试与故障恢复后重放。
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# PRD 第 4 节定义的最小字段集（必填），缺一即为非法素材
REQUIRED_FIELDS = [
    "item_id",
    "source_type",
    "source_url",
    "author_or_publisher",
    "published_at",
    "collected_at",
    "title_or_summary",
    "evidence_status",
    "processing_status",
]

VALID_SOURCE_TYPES = {
    "x_following",
    "x_posts",  # 任务05：X 帖子文件（本地 JSON，由用户侧工具导出；程序不访问 X）
    "x_discovery",
    "aihot",
    "overseas_case",
    "github_domestic",
    "feishu_domestic",
    "manual",
}

VALID_EVIDENCE_STATUS = {"verified", "self_reported", "third_party", "unverified"}
VALID_PROCESSING_STATUS = {"new", "screened", "candidate", "archived"}

# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------


def iso_now() -> str:
    """当前 UTC 时间的 ISO-8601 字符串。"""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def normalize_url(url: str) -> str:
    """URL 规范化：去跟踪参数、去fragment、统一host小写、去尾斜杠。

    用于跨来源去重（PRD 3.5：同一消息多次转载/同步不算多条独立证据）。
    """
    if not url:
        return ""
    parts = urlsplit(url.strip())
    query = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if not k.lower().startswith(("utm_", "ref", "fbclid", "gclid", "spm"))
    ]
    path = parts.path.rstrip("/")
    return urlunsplit(
        (parts.scheme.lower(), parts.netloc.lower(), path, urlencode(query), "")
    )


def make_item_id(source_type: str, key: str) -> str:
    """稳定 item_id：`source:original_id_or_hash`（PRD 9.3 幂等要求）。"""
    return f"{source_type}:{key}"


# ---------------------------------------------------------------------------
# 连接器归一化：aihot / X 关注名单
# ---------------------------------------------------------------------------


def normalize_aihot_item(raw: dict) -> dict:
    """把 aihot.news /api/v1 单条记录映射为 PRD 统一素材格式。

    证据等级判定（PRD 3.4 暂行字段）：
    - 官方渠道（官网/官方博客）→ verified
    - 具名作者专栏/分析 → third_party
    - 其余 → unverified
    """
    source = (raw.get("source") or {}).get("name") or "aihot.news 聚合"
    links = raw.get("links") or {}
    url = normalize_url(links.get("original") or links.get("aihot") or "")
    lower = source.lower()
    if any(k in lower for k in ("openai", "anthropic", "google", "mistral", "腾讯", "官方")):
        evidence = "verified"
    elif any(k in lower for k in ("blog", "专栏", "understandingai", "semanalysis", "verge", "404")):
        evidence = "third_party"
    else:
        evidence = "unverified"

    return {
        "item_id": make_item_id("aihot", raw["id"]),
        "source_type": "aihot",
        "source_url": url,
        "author_or_publisher": source,
        "published_at": (raw.get("publishedAt") or "")[:19] or None,
        "collected_at": iso_now(),
        "title_or_summary": raw.get("title") or "",
        "raw_reference": f"aihot 条目页: {links.get('aihot', '')}；原始出处: {url}",
        "claims": [raw.get("summary") or raw.get("title") or ""],
        "evidence_status": evidence,
        "target_user_hypotheses": [],
        "user_problem_evidence": [],  # 本轮无评论/帖子数据 → 显式留空，不编造
        "tool_or_method": [],
        "duplicate_of": None,
        "processing_status": "new",
        "extra": {
            "category": raw.get("category"),
            "aihot_score": raw.get("score"),
            "reason": raw.get("reason") or "",
            "aihot_url": links.get("aihot", ""),
        },
    }


def normalize_x_account(raw: dict) -> dict:
    """把 X 关注名单（账号级）映射为素材池记录。

    ⚠️ 边界说明（任务02决定）：X 帖子内容当前不可得，X 仅作为账号名单使用，
    故不产生内容级素材；账号描述中的需求线索仅作为假设，不作为需求证据。
    """
    username = raw.get("username") or ""
    return {
        "username": username,
        "name": raw.get("name") or "",
        "description": raw.get("description") or "",
        "followers": raw.get("followers"),
        "item_id": make_item_id("x_following", username),
        "collected_at": iso_now(),
        "processing_status": "screened",
        "content_gap": "X 帖子内容不可得（Pi Agent 不访问 X）；账号仅作情报名单",
    }


def normalize_x_post(raw: dict, users: dict | None = None) -> dict:
    """把 X 帖子文件的单条记录映射为统一素材格式。

    兼容两种输入（任务 A2）：
    1) 扁平格式：id,url,author_username,author_name,created_at,text,lang,likes,
       reposts,replies,quotes,impressions,links；
    2) X API 原样格式：data[] 条目（author_id + public_metrics + conversation_id），
       作者由 users（includes.users 的 id→user 映射）补全。

    - source_url 原样使用文件里的 url；缺失时留空并记入 errors（不凭空拼接 URL）；
    - 证据等级 self_reported：帖子是作者自述，未交叉核验；
    - 互动数据同时保留在 extra（兼容旧消费方）与 metrics（新统一字段）。
    """
    text = (raw.get("text") or "").strip()
    username = str(raw.get("author_username") or "")
    author_name = str(raw.get("author_name") or "")
    author_id = str(raw.get("author_id") or "")
    if author_id and users:
        user = users.get(author_id)
        if isinstance(user, dict):
            username = username or str(user.get("username") or "")
            author_name = author_name or str(user.get("name") or "")
    if username or author_name:
        author = f"{author_name} (@{username})".strip()
    elif author_id:
        author = f"@{author_id}"
    else:
        author = ""

    api_metrics = raw.get("public_metrics") if isinstance(
        raw.get("public_metrics"), dict) else {}

    def metric(flat_key: str, api_key: str):
        value = raw.get(flat_key)
        if isinstance(value, (list, dict)):
            value = None  # 例如 replies 为回复数组而非计数
        if value is None:
            value = api_metrics.get(api_key)
        return 0 if value is None else value

    likes = metric("likes", "like_count")
    reposts = metric("reposts", "retweet_count")
    reply_count = metric("replies", "reply_count")
    quotes = metric("quotes", "quote_count")
    impressions = metric("impressions", "impression_count")

    replies_raw = raw.get("replies")
    comments = normalize_x_replies(replies_raw) if isinstance(replies_raw, list) else []

    links = [x for x in (raw.get("links") or []) if x]
    entities = raw.get("entities") if isinstance(raw.get("entities"), dict) else {}
    for link in entities.get("urls") or []:
        if isinstance(link, dict):
            url = link.get("expanded_url") or link.get("url")
            if url and url not in links:
                links.append(url)

    keywords = raw.get("keywords")
    if isinstance(keywords, list):
        keywords = [str(k) for k in keywords if k]
    else:
        keywords = [str(h.get("tag")) for h in (entities.get("hashtags") or [])
                    if isinstance(h, dict) and h.get("tag")]

    source_url = raw.get("url") or raw.get("source_url") or ""
    errors = list(raw.get("errors")) if isinstance(raw.get("errors"), list) else []
    if not source_url:
        errors.append("缺少 url（未拼接 x.com 链接，避免生成未验证来源）")
    if not author:
        errors.append("缺少作者信息")

    return {
        "item_id": make_item_id("x_posts", str(raw.get("id") or "")),
        "source_type": "x_posts",
        "source_url": source_url,  # 原样使用；缺失则留空（不规范化改写）
        "author_or_publisher": author,
        "published_at": raw.get("created_at") or None,
        "collected_at": iso_now(),
        "title_or_summary": text[:500],
        "raw_reference": "X 帖子（本地文件输入，任务05；程序不访问 X）",
        "claims": [text[:2000]] if text else [],
        "evidence_status": "self_reported",
        "target_user_hypotheses": [],
        "user_problem_evidence": [c["text"] for c in comments if c.get("text")][:5],
        "tool_or_method": [],
        "duplicate_of": None,
        "processing_status": "new",
        # 任务 A2 新增统一字段（只增不改旧字段）
        "platform": "x",
        "origin_agent": "grokbot",
        "metrics": {"likes": likes, "reposts": reposts, "replies": reply_count,
                    "quotes": quotes, "views": impressions},
        "comments": comments,
        "keywords": keywords,
        "license_status": "公开",
        "errors": errors,
        "extra": {
            "author_username": username,
            "author_id": author_id,
            "conversation_id": raw.get("conversation_id") or "",
            "lang": raw.get("lang"),
            "likes": likes,
            "reposts": reposts,
            "replies": reply_count,
            "quotes": quotes,
            "impressions": impressions,
            "links": links,
        },
    }


def normalize_x_reply(raw: dict) -> dict:
    """把单条回复（扁平或 X API 原样）映射为 comments 条目。

    隐私：评论作者用户名/作者 id 一律不进入 comments，只保留文本、时间、计数。
    """
    metrics = raw.get("metrics") if isinstance(raw.get("metrics"), dict) else {}
    api_metrics = raw.get("public_metrics") if isinstance(
        raw.get("public_metrics"), dict) else {}

    def value(flat_key: str, api_key: str):
        if flat_key in metrics and metrics[flat_key] is not None:
            return metrics[flat_key]
        if flat_key in raw and raw[flat_key] is not None:
            return raw[flat_key]
        if api_key in api_metrics and api_metrics[api_key] is not None:
            return api_metrics[api_key]
        return 0

    return {
        "text": (raw.get("text") or "").strip(),
        "created_at": raw.get("created_at") or "",
        "metrics": {
            "likes": value("likes", "like_count"),
            "reposts": value("reposts", "retweet_count"),
            "replies": value("replies", "reply_count"),
            "quotes": value("quotes", "quote_count"),
        },
        "parent_id": _x_reply_parent(raw),
        "lang": raw.get("lang") or "",
    }


def normalize_x_replies(replies: list) -> list[dict]:
    if not isinstance(replies, list):
        raise ValueError("replies 应为回复数组（list）")
    out = []
    for i, raw in enumerate(replies):
        if not isinstance(raw, dict):
            raise ValueError(f"第 {i} 条回复不是对象")
        out.append(normalize_x_reply(raw))
    return out


def _x_reply_parent(raw: dict) -> str:
    for key in ("parent_id", "in_reply_to", "in_reply_to_id",
                "in_reply_to_status_id", "reply_to"):
        value = raw.get(key)
        if value:
            return str(value)
    refs = raw.get("referenced_tweets") or raw.get("referenced_tones") or []
    if isinstance(refs, list):
        for ref in refs:
            if not isinstance(ref, dict):
                continue
            if ref.get("type") in (None, "replied_to", "reply") and ref.get("id"):
                return str(ref["id"])
    return ""


# ---------------------------------------------------------------------------
# 去重与校验
# ---------------------------------------------------------------------------


def dedupe(items: list[dict]) -> tuple[list[dict], list[str]]:
    """按 item_id + 规范化 URL 去重。返回 (去重后列表, 被丢弃的item_id列表)。"""
    seen_ids: set[str] = set()
    seen_urls: set[str] = set()
    out, dropped = [], []
    for it in items:
        iid = it["item_id"]
        nurl = normalize_url(it.get("source_url") or "")
        if iid in seen_ids or (nurl and nurl in seen_urls):
            dropped.append(iid)
            continue
        seen_ids.add(iid)
        if nurl:
            seen_urls.add(nurl)
        out.append(it)
    return out, dropped


def validate_item(item: dict) -> list[str]:
    """校验素材是否满足 PRD 最小字段与枚举约束，返回错误列表。"""
    errors = []
    for f in REQUIRED_FIELDS:
        if f not in item or item[f] in (None, ""):
            # X API 原样文件可能无 url；此时 normalize_x_post 留空并写入 errors，
            # 由用户/下游显式看到缺失，而不是凭空拼接来源链接。
            if (f == "source_url" and item.get("source_type") == "x_posts"
                    and item.get("platform") == "x" and item.get("errors")):
                continue
            if not (f == "published_at" and item.get("published_at") is None):
                errors.append(f"缺少必填字段: {f}")
    if item.get("source_type") not in VALID_SOURCE_TYPES:
        errors.append(f"非法 source_type: {item.get('source_type')}")
    if item.get("evidence_status") not in VALID_EVIDENCE_STATUS:
        errors.append(f"非法 evidence_status: {item.get('evidence_status')}")
    if item.get("processing_status") not in VALID_PROCESSING_STATUS:
        errors.append(f"非法 processing_status: {item.get('processing_status')}")
    return errors


# ---------------------------------------------------------------------------
# 可解释初筛（heuristic，非已验证模型 —— PRD 5.3 要求不伪装成已验证）
# ---------------------------------------------------------------------------

# 目标用户（PRD 1.1）：①职场提效者 ②非程序员想做工具的人 ③个人项目/副业关注者
# 关键词启发式：命中越多越贴近"普通人可上手"，仅用于排序辅助，不得当传播规律。
SCREEN_KEYWORDS = {
    "high": ["教程", "如何", "指南", "提示词", "prompt", "文档", "表格", "pdf",
             "ppt", "演示文稿", "本地运行", "免费", "开源", "量化", "用量",
             "上手", "步骤", "实测", "过度提示"],
    "mid": ["发布", "更新", "新增", "登陆", "上线", "支持"],
}
CATEGORY_BASE = {"tip": 3, "ai-products": 2, "ai-models": 2, "opinion": 1,
                 "industry": 1, "paper": 0}


def screen_score(item: dict) -> tuple[int, list[str]]:
    """返回 (0-10 分, 命中理由列表)。规则透明可解释，禁止用于宣称'爆款预测'。"""
    reasons: list[str] = []
    text = (item.get("title_or_summary") or "") + " " + " ".join(item.get("claims") or [])
    text_l = text.lower()
    cat = (item.get("extra") or {}).get("category") or ""
    base = CATEGORY_BASE.get(cat, 0)
    if base:
        reasons.append(f"类目 {cat} 基础分 {base}")
    hits = [k for k in SCREEN_KEYWORDS["high"] if k in text_l]
    reasons += [f"高相关词「{k}」+2" for k in hits]
    mid_hits = [k for k in SCREEN_KEYWORDS["mid"] if k in text_l][:2]
    reasons += [f"中相关词「{k}」+1" for k in mid_hits]
    score = min(10, base + 2 * len(hits) + len(mid_hits))
    return score, reasons
