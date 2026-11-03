"""选题卡字段回退与口径归一（阶段3 修复轮）。

目的：新字段（面向用户/痛点或机会/为何现在做/切入角度/国内可用/是否付费/
能否实际验证）不得在旧字段已有内容时偷懒写「待验证」。
本模块只做纯文本处理，不发网络请求、不读写 Notion。
"""

from __future__ import annotations

from urllib.parse import urlparse

AUDIENCE_CATEGORIES = (
    "AI普通使用者",
    "AI内容创作者",
    "AI编程与应用开发者",
    "AI副业与创业探索者",
    "国外AI工具的国内使用者",
)

_UNKNOWN = {"", "待验证", "未知", "待核实", "待确认", "none", "n/a", "null"}

_AUDIENCE_KEYWORDS: dict[str, tuple[str, ...]] = {
    "AI普通使用者": (
        "普通使用者", "普通用户", "职场", "提效", "办公", "小白", "零基础",
        "非技术", "非程序员", "普通人", "生活", "学习",
    ),
    "AI内容创作者": (
        "内容创作者", "创作者", "自媒体", "短视频", "抖音", "小红书",
        "剪辑", "文案", "视频", "博主", "选题", "账号",
    ),
    "AI编程与应用开发者": (
        "开发者", "程序员", "编程", "代码", "开发", "部署", "api",
        "agent", "智能体", "vibe coding", "应用开发", "github",
    ),
    "AI副业与创业探索者": (
        "副业", "创业", "接单", "变现", "赚钱", "商业化", "个人项目",
        "小老板", "小企业", "自由职业", "side hustle",
    ),
    "国外AI工具的国内使用者": (
        "国外", "海外", "国内使用", "国内可用", "国内能用", "梯子",
        "翻墙", "外币卡", "openai", "claude", "gemini", "chatgpt",
        "平替", "替代",
    ),
}

_CHINA_LABELS = ("不可用", "需外币卡", "需梯子", "可用")


def filled(value) -> str:
    """返回可用的非占位文本；未知/空统一视为空串。"""
    text = str(value or "").strip()
    return "" if text.lower() in _UNKNOWN else text


def _dedupe_prefix(text: str, label: str) -> str:
    """去掉重复的标签前缀，例如“待验证：待验证” → “待验证”。"""
    value = str(text or "").strip()
    if not value:
        return label
    if value == label:
        return label
    prefix = f"{label}："
    while value.startswith(prefix):
        value = value[len(prefix):].strip()
    # 兼容半角冒号与重复标签本身。
    prefix_ascii = f"{label}:"
    while value.startswith(prefix_ascii):
        value = value[len(prefix_ascii):].strip()
    if not value or value == label:
        return label
    return f"{label}：{value}"


def audience_category(*texts) -> str:
    """从 5 类目标受众中选匹配项；无信号时落到「AI普通使用者」。"""
    categories = infer_audience_categories(*texts)
    return categories[0] if categories else "AI普通使用者"


def parse_audience_categories(value) -> list[str]:
    """解析文本中出现的白名单受众；可多个，按白名单顺序返回。"""
    text = str(value or "")
    return [name for name in AUDIENCE_CATEGORIES if name in text]


def infer_audience_categories(*texts) -> list[str]:
    """规则推断受众类别（可多个）；无信号时回退 AI普通使用者。"""
    haystack = " ".join(str(t or "") for t in texts).lower()
    if not haystack.strip():
        return ["AI普通使用者"]
    scores = {
        name: sum(1 for word in words if word.lower() in haystack)
        for name, words in _AUDIENCE_KEYWORDS.items()
    }
    matched = [name for name in AUDIENCE_CATEGORIES if scores[name] > 0]
    if not matched:
        return ["AI普通使用者"]
    # 多个命中时最多保留 3 类，避免把弱相关的类别全部贴上。
    matched.sort(key=lambda name: (-scores[name], AUDIENCE_CATEGORIES.index(name)))
    return matched[:3]


def audience_user(old_value, *extra_texts) -> str:
    """面向用户：映射到 5 类白名单受众（可多个，用“、”连接）。

    旧字段已有描述时保留描述，并附上规则/LLM 给出的类别；若旧值本身已是
    白名单类别（或 LLM 只返回类别），则直接输出类别列表，避免重复。
    """
    base = filled(old_value)
    explicit = parse_audience_categories(base)
    if explicit:
        return "、".join(explicit)
    categories = infer_audience_categories(base, *extra_texts)
    if not base:
        return "、".join(categories)
    return f"{base}（受众匹配：{'、'.join(categories)}）"


def normalize_china_usability(value) -> str:
    """国内可用口径：可用 / 需梯子 / 需外币卡 / 不可用 / 待验证 + 说明。"""
    text = filled(value)
    if not text:
        return "待验证"
    if text.startswith("国外新机会"):
        return text
    if any(k in text for k in ("不可用", "无法访问", "被墙", "封禁", "不支持国内")):
        label = "不可用"
    elif any(k in text for k in ("外币卡", "国外信用卡", "境外卡",
                                 "需要订阅", "需付费", "付费订阅")):
        label = "需外币卡"
    elif any(k in text for k in ("梯子", "翻墙", "科学上网", "vpn", "受限")):
        label = "需梯子"
    elif any(k in text for k in ("可用", "能用", "直接使用", "免翻墙",
                                 "无需翻墙", "支持国内")):
        label = "可用"
    else:
        return _dedupe_prefix(text, "待验证")
    return _dedupe_prefix(text, label)


def paid_label(value) -> str:
    """是否付费：有素材文本时给规则判断，确实无线索才「待验证」。"""
    text = filled(value)
    if not text:
        return "待验证"
    lowered = text.lower()
    if any(k in lowered for k in ("免费", "开源", "试用", "白嫖")):
        return "免费/开源（额度待验证）"
    if any(k in lowered for k in ("订阅", "付费", "收费", "价格", "会员")):
        return "可能需付费（待验证）"
    return "待验证"


def is_simulated_source(item_id, source_url) -> bool:
    """fake/example 模拟来源判定（用于拒绝真实写入与修复标记）。"""
    if "fake" in str(item_id or "").lower():
        return True
    url = str(source_url or "").strip()
    if not url:
        return False
    host = (urlparse(url).hostname or "").lower().rstrip(".")
    return (host == "example.com" or host.endswith(".example.com")
            or host == "example.org" or host.endswith(".example.org"))
