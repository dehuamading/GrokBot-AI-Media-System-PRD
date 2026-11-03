"""规则2安全过滤：国外新工具“只讲能力，不讲开通步骤”。

纯文本函数，无网络/无 Notion 写入。供选题卡与文案生成共用。

红线：
- forbidden_steps_check(text) 返回命中的敏感步骤词，不返回空字符串占位；
- scrub_forbidden_steps / scrub_income_promises 只删除命中句，不新增事实；
- 收益承诺（月入/躺赚/保证/稳赚/一定能等）不得出现在最终文案中。
"""

from __future__ import annotations

import re

# (展示名, 正则)；英文大小写不敏感。
_FORBIDDEN_STEP_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("注册", re.compile(r"注册(?:账号|帐号|账户)?", re.I)),
    ("翻墙", re.compile(r"翻墙", re.I)),
    ("VPN", re.compile(r"\bVPN\b|vpn", re.I)),
    ("梯子", re.compile(r"梯子|科学上网", re.I)),
    ("代充", re.compile(r"代充", re.I)),
    ("虚拟卡", re.compile(r"虚拟卡", re.I)),
    ("外币卡", re.compile(r"外币卡|国外信用卡|境外卡", re.I)),
    ("绕过地区限制", re.compile(
        r"绕过(?:地区|区域|地理)?限制|绕开(?:地区|区域|地理)?限制|"
        r"解除地区限制|突破地区限制", re.I)),
]

_INCOME_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("月入", re.compile(r"月入", re.I)),
    ("日入", re.compile(r"日入", re.I)),
    ("躺赚", re.compile(r"躺赚", re.I)),
    ("稳赚", re.compile(r"稳赚", re.I)),
    ("保证", re.compile(r"保证(?:收益|赚钱|赚|收入)?", re.I)),
    ("一定能", re.compile(r"一定能", re.I)),
    ("包赚", re.compile(r"包赚", re.I)),
    ("暴富", re.compile(r"暴富", re.I)),
    ("收益翻倍", re.compile(r"收益翻倍", re.I)),
]

_SEGMENT_SPLIT = re.compile(r"(?<=[。！？!?；;\n])")


def _hits(text: str, patterns: list[tuple[str, re.Pattern]]) -> list[str]:
    blob = str(text or "")
    found: list[str] = []
    for label, pattern in patterns:
        if pattern.search(blob) and label not in found:
            found.append(label)
    return found


def forbidden_steps_check(text: str) -> list[str]:
    """返回文本中命中的敏感开通步骤（注册/翻墙/VPN/梯子/代充/虚拟卡/外币卡/绕过地区限制）。"""
    return _hits(text, _FORBIDDEN_STEP_PATTERNS)


def income_promise_check(text: str) -> list[str]:
    """返回文本中命中的收益承诺词（月入/躺赚/保证/稳赚/一定能等）。"""
    return _hits(text, _INCOME_PATTERNS)


def _scrub(text: str, patterns: list[tuple[str, re.Pattern]]) -> tuple[str, list[str]]:
    hits = _hits(text, patterns)
    if not hits:
        return str(text or ""), []
    segments = _SEGMENT_SPLIT.split(str(text or ""))
    kept = [seg for seg in segments if not _hits(seg, patterns)]
    cleaned = "".join(kept).strip()
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    return cleaned, hits


def scrub_forbidden_steps(text: str) -> tuple[str, list[str]]:
    """删除含敏感步骤的句子；返回 (清理后文本, 命中列表)。"""
    return _scrub(text, _FORBIDDEN_STEP_PATTERNS)


_CARD_STRUCTURAL_FIELDS = {
    "引用帖子", "原始来源", "_source_urls", "关联素材", "topic_id",
    "用户选择结果", "评分版本", "row_id",
}


def scrub_card_forbidden_steps(card: dict) -> dict:
    """就地把选题卡中的敏感开通步骤删掉（URL 等结构字段不动）。"""
    for key, value in list((card or {}).items()):
        if not isinstance(value, str) or key in _CARD_STRUCTURAL_FIELDS:
            continue
        cleaned, hits = scrub_forbidden_steps(value)
        if hits:
            card[key] = cleaned or "只讲能力，不讲开通步骤"
    return card


def scrub_income_promises(text: str) -> tuple[str, list[str]]:
    """删除含收益承诺的句子；返回 (清理后文本, 命中列表)。"""
    return _scrub(text, _INCOME_PATTERNS)


def scrub_texts(texts: list[str], *,
                forbidden: bool = True,
                income: bool = True) -> tuple[list[str], list[str], list[str]]:
    """批量清理字符串：返回 (清理结果, 敏感步骤命中, 收益承诺命中)。"""
    out: list[str] = []
    step_hits: list[str] = []
    income_hits: list[str] = []
    for text in texts:
        value = str(text or "")
        if forbidden:
            value, hits = scrub_forbidden_steps(value)
            step_hits.extend(h for h in hits if h not in step_hits)
        if income:
            value, hits = scrub_income_promises(value)
            income_hits.extend(h for h in hits if h not in income_hits)
        out.append(value)
    return out, step_hits, income_hits
