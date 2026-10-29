"""LLM 客户端协议：输入（选题卡 + 素材事实），输出结构化初稿。

红线：DraftClient 只负责生成文本，不得携带任何“发布”能力；
输出中的收益/时间/效果/价格一律须能追溯到素材 claims，否则进 unverified_warnings。
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Draft:
    topic_id: str
    platform: str                 # 抖音 | 小红书
    title: str
    body: str
    fact_checklist: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    unverified_warnings: list[str] = field(default_factory=list)
    engine: str = "template@v1"
    version: int = 1              # PRD 6.1：每题×每平台只有 V1 起点一份，之后修订同一稿

    def to_text(self) -> str:
        def bullets(items, prefix="-"):
            return [f"{prefix} {x}" for x in items] or [f"{prefix}（无）"]

        parts = [f"【标题】{self.title}", "", self.body, "",
                 "【事实核对清单】", *bullets(self.fact_checklist),
                 "【来源链接】", *bullets(self.sources),
                 "【未证实内容提醒】", *bullets(self.unverified_warnings)]
        return "\n".join(parts)


class DraftClient:
    """协议类：实现 generate(platform, card, materials) -> Draft。"""

    def generate(self, platform: str, card: dict, materials: list[dict]) -> Draft:
        raise NotImplementedError


def platforms_of(card: dict) -> list[str]:
    """建议平台 → 具体平台列表（抖音/小红书；『两者』= 两份稿）。"""
    p = card.get("建议平台") or card.get("用户指定平台") or "两者"
    return ["抖音", "小红书"] if p == "两者" else [p]
