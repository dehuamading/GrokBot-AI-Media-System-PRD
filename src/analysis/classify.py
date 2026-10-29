"""分类器：把统一素材映射到内容类型（选题卡与文案模板的分派依据）。

内容类型（本系统自定义运营分类，非平台官方分类）：
  工具教程 / 产品发布 / 行业动态 / 研究解读 / 观点评论 / 安全事件
规则可解释：类目 + 标题/摘要关键词 + 来源名，命中即给出理由。
"""

from __future__ import annotations

CONTENT_TYPES = ["工具教程", "产品发布", "行业动态", "研究解读", "观点评论", "安全事件"]

# aihot category → 默认内容类型（可被关键词规则覆盖）
CATEGORY_MAP = {
    "tip": "工具教程",
    "ai-products": "产品发布",
    "ai-models": "产品发布",
    "industry": "行业动态",
    "paper": "研究解读",
    "opinion": "观点评论",
}

KEYWORD_RULES: list[tuple[str, list[str]]] = [
    ("工具教程", ["教程", "如何", "指南", "上手", "步骤", "实测", "怎么", "walkthrough", "how to"]),
    ("安全事件", ["攻击", "漏洞", "泄露", "间谍", "入侵", "安全", "attack", "breach", "vulnerability"]),
    ("研究解读", ["论文", "技术报告", "arxiv", "研究", "benchmark", "评测报告"]),
    ("观点评论", ["解读", "观点", "争议", "分析认为", "评论"]),
]


def classify_item(item: dict) -> tuple[str, str]:
    """返回 (内容类型, 可解释理由)。无法判定时回退到类目映射或『行业动态』。"""
    text = ((item.get("title_or_summary") or "") + " " +
            " ".join(item.get("claims") or [])).lower()
    publisher = (item.get("author_or_publisher") or "")
    category = (item.get("extra") or {}).get("category") or ""

    for ctype, kws in KEYWORD_RULES:
        hits = [k for k in kws if k in text]
        if hits:
            return ctype, f"标题/摘要命中关键词「{hits[0]}」→ {ctype}"

    if category in CATEGORY_MAP:
        return CATEGORY_MAP[category], f"aihot 类目 {category} → {CATEGORY_MAP[category]}"

    if any(k in publisher for k in ("安全", "Wiz", "Research")):
        return "安全事件", f"来源「{publisher}」为安全研究方"

    return "行业动态", "未命中显式规则，回退默认『行业动态』（保守分类）"
