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


def audience_category(*texts) -> str:
    """从 5 类目标受众中选匹配项；无信号时落到「AI普通使用者」。"""
    haystack = " ".join(str(t or "") for t in texts).lower()
    if not haystack.strip():
        return "AI普通使用者"
    scores = {
        name: sum(1 for word in words if word.lower() in haystack)
        for name, words in _AUDIENCE_KEYWORDS.items()
    }
    best = max(AUDIENCE_CATEGORIES, key=lambda name: (scores[name], -AUDIENCE_CATEGORIES.index(name)))
    return best if scores[best] else "AI普通使用者"


def audience_user(old_value, *extra_texts) -> str:
    """面向用户：优先旧「面向谁」，并附上 5 类受众中的匹配项。"""
    base = filled(old_value)
    if not base:
        base = "待验证"
    category = audience_category(base, *extra_texts)
    if "受众匹配" in base or category in base:
        return base
    return f"{base}（受众匹配：{category}）"


def normalize_china_usability(value) -> str:
    """国内可用口径：可用 / 需梯子 / 需外币卡 / 不可用 / 待验证 + 说明。"""
    text = filled(value)
    if not text:
        return "待验证"
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
        return f"待验证：{text}"
    if text.startswith(label):
        return text
    return f"{label}：{text}"


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
