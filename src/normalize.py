"""标准化层：统一素材格式、URL 规范化、去重、可解释初筛。

对应 PRD 第 4 节（统一素材格式与证据约束）与第 5.3 节（候选选题判断维度）。
本模块不含任何网络请求，纯函数，便于单元测试与故障恢复后重放。
"""

from __future__ import annotations

import hashlib
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
    # 阶段2-B 新增（只增不改）
    "github",
    "official",
    "import",
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
        # 阶段2-B 统一字段（只增不改旧字段）
        "platform": "aihot",
        "origin_agent": "pi",
        "keywords": [],
        "comments": [],
        "metrics": {},
        "license_status": "公开",
        "errors": [],
        "extra": {
            "category": raw.get("category"),
            "aihot_score": raw.get("score"),
            "reason": raw.get("reason") or "",
            "aihot_url": links.get("aihot", ""),
        },
    }


def normalize_github_item(raw: dict, github_extra: dict | None = None) -> dict:
    """把 GitHub REST search/repositories 条目映射为统一素材（只读取公开元数据）。"""
    full_name = raw.get("full_name") or raw.get("name") or str(raw.get("id") or "")
    owner = (raw.get("owner") or {}).get("login") or full_name.split("/")[0]
    description = (raw.get("description") or "").strip()
    stars = raw.get("stargazers_count")
    if not isinstance(stars, (int, float)):
        stars = 0
    topics = [str(t) for t in (raw.get("topics") or []) if t]
    license_raw = raw.get("license") or {}
    license_name = (license_raw.get("spdx_id") or license_raw.get("name")
                    or "未标注")
    spdx = str(license_raw.get("spdx_id") or "").strip()
    license_label = spdx if spdx and spdx.upper() not in {
        "NOASSERTION", "NONE"} else "未知"
    html_url = raw.get("html_url") or ""
    pushed = raw.get("pushed_at") or raw.get("updated_at") or None

    github = {
        "name": raw.get("name") or full_name,
        "full_name": full_name,
        "url": html_url,
        "purpose": description or "待验证",
        "stars": stars,
        "last_update": pushed,
        "latest_release": "无",
        "license": license_name,
        "install_difficulty": "待验证（推测）",
        "needs_paid_api": "待验证",
        "windows_support": "待验证",
        "china_usability": "待验证",
        "target_users": [],
        "worth_video": "待验证（推测）",
        "language": raw.get("language"),
        # 仓库元数据来自官方 REST，可信；description 仍是维护者自述。
        "metadata_verified": True,
    }
    if github_extra:
        github.update(github_extra)

    return {
        "item_id": make_item_id("github", full_name or str(raw.get("id") or "")),
        "source_type": "github",
        "source_url": html_url,
        "author_or_publisher": str(owner),
        "published_at": pushed,
        "collected_at": iso_now(),
        "title_or_summary": f"{full_name}：{description}" if description else full_name,
        "raw_reference": f"GitHub 官方 REST：{full_name}",
        "claims": [description] if description else [],
        "evidence_status": "self_reported",
        "target_user_hypotheses": list(github.get("target_users") or []),
        "user_problem_evidence": [],
        "tool_or_method": topics,
        "duplicate_of": None,
        "processing_status": "new",
        "platform": "github",
        "origin_agent": "pi",
        "keywords": topics,
        "comments": [],
        "metrics": {"stars": stars},
        "license_status": f"公开+许可证:{license_label}",
        "errors": [],
        "extra": {"github": github},
    }


def normalize_official_item(raw: dict, source: dict) -> dict:
    """把官方 RSS/Atom/Releases 条目映射为统一素材。

    raw 由 official_source 解析成统一中间结构：id/url/title/summary/published_at。
    """
    url = (raw.get("url") or "").strip()
    title = (raw.get("title") or "").strip()
    summary = (raw.get("summary") or "").strip()
    key = raw.get("id") or hashlib.sha1(
        f"{source.get('id', '')}|{url}|{title}".encode("utf-8")).hexdigest()[:16]
    source_name = source.get("name") or source.get("id") or "官方源"
    extra_official = {
        "source_id": source.get("id"),
        "source_name": source_name,
        "source_type": source.get("type"),
    }
    extra_official.update(raw.get("extra") or {})
    return {
        "item_id": make_item_id("official", str(key)),
        "source_type": "official",
        "source_url": url,
        "author_or_publisher": source_name,
        "published_at": raw.get("published_at") or None,
        "collected_at": iso_now(),
        "title_or_summary": f"{title} {summary}".strip()[:800],
        "raw_reference": f"官方源：{source_name}",
        "claims": [summary] if summary else ([title] if title else []),
        "evidence_status": "verified",
        "target_user_hypotheses": [],
        "user_problem_evidence": [],
        "tool_or_method": [],
        "duplicate_of": None,
        "processing_status": "new",
        "platform": "official",
        "origin_agent": "pi",
        "keywords": [],
        "comments": [],
        "metrics": {},
        "license_status": "公开",
        "errors": [],
        "extra": {"official": extra_official},
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

    - source_url 原样使用文件里的 url；缺失时若 username 与 id 齐备，则构造
      x.com 可追溯链接并在 extra 标注 url_constructed=true，否则留空记入 errors；
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
    url_constructed = False
    if not source_url and username and raw.get("id"):
        # X API 原样格式无 url，但 username 与 id 齐备时可稳定构造可追溯链接。
        # x.com 仅用于拼接本地素材的可追溯链接，不是网络访问端点。
        source_url = urlunsplit(("https", "x.com",
                                 f"/{username}/status/{raw['id']}", "", ""))
        url_constructed = True
    errors = list(raw.get("errors")) if isinstance(raw.get("errors"), list) else []
    if not source_url:
        errors.append("缺少 url（无法用 username+id 构造）")
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
            "url_constructed": url_constructed,
        },
    }


def normalize_x_reply(raw: dict) -> dict:
    """把单条回复（扁平或 X API 原样）映射为 comments 条目。

    隐私：评论作者用户名/作者 id 一律不进入 comments，只保留文本、时间、计数。
    """
    metrics = raw.get("metrics") if isinstance(raw.get("metrics"), dict) else {}
    api_metrics = raw.get("public_metrics") if isinstance(
        raw.get("public_metrics"), dict) else {}

    def scalar(value):
        return None if isinstance(value, (list, dict)) else value

    def value(flat_key: str, api_key: str):
        for candidate in (metrics.get(flat_key), raw.get(flat_key),
                          api_metrics.get(api_key)):
            candidate = scalar(candidate)
            if candidate is not None:
                return candidate
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
    refs = raw.get("referenced_tweets") or []
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


def _has_missing_url_error(errors) -> bool:
    for err in errors or []:
        text = str(err)
        if "缺少 url" in text or "缺少 source_url" in text:
            return True
    return False


def validate_item(item: dict) -> list[str]:
    """校验素材是否满足 PRD 最小字段与枚举约束，返回错误列表。"""
    errors = []
    for f in REQUIRED_FIELDS:
        if f not in item or item[f] in (None, ""):
            # X API 原样文件可能无 url；仅当 errors 明确说明缺少 url/source_url
            # 时豁免，避免任意错误（如缺少作者）掩盖来源缺失。
            if (f == "source_url" and item.get("source_type") == "x_posts"
                    and item.get("platform") == "x"
                    and _has_missing_url_error(item.get("errors"))):
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
