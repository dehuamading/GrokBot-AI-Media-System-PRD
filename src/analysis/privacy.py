"""选题卡隐私脱敏：X 帖子正文/用户名不得进入 03/02。

红线（阶段3 修复轮）：
- 任何写入 03/02 的字段不得包含 X 帖子正文的 >20 字连续原文；
- 不得包含 X 作者显示名、username 或 @handle（引用链接字段只保留 url）；
- 启发式回退标题用「<类型>｜<关键词概括>」，绝不截断帖子原文。

本模块只做纯文本处理，不发网络请求，不读写 Notion。
"""

from __future__ import annotations

import re

X_SOURCES = {"x", "x_posts"}
VERBATIM_MIN = 21  # >20 字连续原文

# 关键词概括只从固定词表/结构字段生成，不复制帖子原文。
_KEYWORD_BUCKETS: list[tuple[str, tuple[str, ...]]] = [
    ("国内可用", ("国内可用", "国内能用", "不用翻墙", "免翻墙", "国内直接")),
    ("免费/开源", ("免费", "开源", "试用", "白嫖")),
    ("教程/实测", ("教程", "指南", "步骤", "实测", "零基础", "手把手", "上手")),
    ("新模型/新功能", ("新模型", "新功能", "新能力", "首个", "首次", "发布", "更新")),
    ("副业/接单", ("副业", "接单", "变现", "赚钱", "创业")),
    ("工具/效率", ("工具", "办公", "效率", "自动化", "工作流", "agent", "智能体")),
    ("安全/风险", ("安全", "漏洞", "泄露", "风险", "故障", "报错")),
    ("用户需求", ("安装", "部署", "注册", "付款", "替代", "比较", "故障排查")),
]

# 这些字段允许保留 url / item_id，不做正文替换；但仍会清洗 @handle。
_URL_FIELDS = {"引用帖子", "原始来源", "_source_urls", "关联素材", "topic_id",
               "用户选择结果", "评分版本"}
# 评论证据是脱敏短摘录（非帖子正文），只清 @handle，不因巧合做整段替换。
_NO_VERBATIM_REPLACE = {"需求证据"}


def is_x_material(material: dict) -> bool:
    source = str(material.get("source_type") or material.get("platform") or "")
    return source in X_SOURCES


def _compact(text: str) -> str:
    return re.sub(r"\s+", "", str(text or ""))


def x_raw_texts(materials: list[dict]) -> list[str]:
    texts: list[str] = []
    for m in materials:
        if not is_x_material(m):
            continue
        candidates = [m.get("title_or_summary") or ""]
        candidates.extend(str(c) for c in (m.get("claims") or []))
        for text in candidates:
            compact = _compact(text)
            if len(compact) >= VERBATIM_MIN:
                texts.append(compact)
    return list(dict.fromkeys(texts))


def x_identifiers(materials: list[dict]) -> list[str]:
    """提取显示名、username、@handle；按长度降序，避免短串先替换破坏长串。"""
    names: set[str] = set()
    for m in materials:
        if not is_x_material(m):
            continue
        author = str(m.get("author_or_publisher") or "").strip()
        if author:
            names.add(author)
            names.add(author.split(" (")[0].strip())
            names.update(re.findall(r"@([A-Za-z0-9_]+)", author))
        username = str((m.get("extra") or {}).get("author_username") or "").strip()
        if username:
            names.add(username)
            names.add(f"@{username}")
    return sorted((n for n in names if n), key=len, reverse=True)


def keyword_summary(material: dict, content_type: str | None = None) -> str:
    text = ((material.get("title_or_summary") or "") + " " +
            " ".join(str(c) for c in (material.get("claims") or []))).lower()
    labels: list[str] = []
    for label, words in _KEYWORD_BUCKETS:
        if any(word.lower() in text for word in words):
            labels.append(label)
    if not labels:
        category = str((material.get("extra") or {}).get("category") or "").strip()
        if category in ("tip", "ai-products", "ai-models"):
            labels.append("AI 新工具")
        elif category in ("industry", "paper", "opinion"):
            labels.append("行业动态")
        else:
            source = str(material.get("source_type") or "")
            labels.append("开源项目" if source == "github" else "AI 行业动态")
    return "、".join(labels[:3])


def contains_verbatim(text: str, raw_texts: list[str],
                      min_len: int = VERBATIM_MIN) -> bool:
    compact = _compact(text)
    if not compact:
        return False
    for raw in raw_texts:
        if len(raw) < min_len:
            continue
        for i in range(0, len(raw) - min_len + 1):
            if raw[i:i + min_len] in compact:
                return True
    return False


def scrub_identifiers(text: str, identifiers: list[str]) -> str:
    cleaned = str(text or "")
    for ident in identifiers:
        if ident:
            cleaned = cleaned.replace(ident, "")
    cleaned = re.sub(r"@[A-Za-z0-9_]+", "", cleaned)
    return re.sub(r"\s+", " ", cleaned).strip()


def sanitize_card(card: dict, materials: list[dict]) -> dict:
    """就地清洗一张选题卡；只对引用到 X 素材的卡执行。"""
    x_mats = [m for m in materials if is_x_material(m)]
    if not x_mats:
        return card
    raw_texts = x_raw_texts(x_mats)
    identifiers = x_identifiers(x_mats)
    ctype = str(card.get("选题类型") or card.get("类型") or "选题")
    summary_title = f"{ctype}｜{keyword_summary(x_mats[0], ctype)}"

    for key, value in list(card.items()):
        if not isinstance(value, str):
            continue
        if key not in _URL_FIELDS:
            value = scrub_identifiers(value, identifiers)
        if key in _NO_VERBATIM_REPLACE or key in _URL_FIELDS:
            card[key] = value
            continue
        if contains_verbatim(value, raw_texts):
            card[key] = summary_title if key == "拟定标题" else (
                "基于引用素材的概括（帖子正文未入库）")
        else:
            card[key] = value

    # 标题即使恰好短于 21 字，也不得与原文相等/包含原文。
    title = str(card.get("拟定标题") or "")
    compact_title = _compact(title)
    if any(compact_title and (compact_title in raw or raw in compact_title)
           for raw in raw_texts):
        card["拟定标题"] = summary_title
    return card
