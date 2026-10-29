"""离线模板引擎（默认 LLM 实现）：确定性生成抖音口播稿 / 小红书图文稿。

设计动机（红线）：未经用户批准不购买任何 LLM 服务 → 默认引擎零网络零成本；
模板规则来自 prompts/script-douyin.md 与 prompts/script-xiaohongshu.md 的初始写作假设。
文本只由选题卡与素材 claims 拼装，不编造数字；无法溯源的内容进 unverified_warnings。
"""

from __future__ import annotations

from src.llm.base import Draft, DraftClient

ENGINE = "template@v1"


class TemplateClient(DraftClient):
    def generate(self, platform: str, card: dict, materials: list[dict]) -> Draft:
        facts: list[str] = []
        sources: list[str] = []
        for m in materials:
            for c in m.get("claims") or []:
                if c and c not in facts:
                    facts.append(c)
            if m.get("source_url"):
                sources.append(m["source_url"])

        title_core = card["拟定标题"].split("｜", 1)[-1]
        warnings = [card.get("需求证据", "").split("；")[0]]  # 需求假设必须显式提醒
        if not sources:
            warnings.append("本选题缺少可打开的原始出处链接，发布前必须补证")

        if platform == "抖音":
            draft = self._douyin(card, title_core, facts)
        elif platform == "小红书":
            draft = self._xhs(card, title_core, facts)
        else:
            raise ValueError(f"不支持的平台：{platform}")

        draft.sources = sources
        draft.unverified_warnings = [w for w in warnings if w]
        draft.fact_checklist = [f"素材出处：{m.get('author_or_publisher')}｜{m.get('title_or_summary')}"
                                for m in materials] or ["（无素材，选题卡为唯一来源）"]
        return draft

    # ------------------------------------------------------------------
    def _douyin(self, card: dict, title_core: str, facts: list[str]) -> Draft:
        hook = f"如果你还在{card['面向谁'].split('；')[0]}，这条建议先收藏。"
        fact_lines = [f"第{i}点：{f}" for i, f in enumerate(facts[:3], 1)]
        if not fact_lines:
            fact_lines = ["（本轮素材未提供可引用事实，请勿凭空补充）"]
        body = "\n".join([
            hook,
            f"今天讲清楚一件事：{title_core}。",
            "",
            "先说事实（每一条都有出处，我放在文案最后）：",
            *fact_lines,
            "",
            f"这件事跟你有什么关系？{card['核心价值']}。",
            "一个动作带走：打开来源链接自己核验一遍，再决定要不要用到自己的工作流里。",
        ])
        return Draft(topic_id=card["topic_id"], platform="抖音",
                     title=card["拟定标题"], body=body, engine=ENGINE)

    def _xhs(self, card: dict, title_core: str, facts: list[str]) -> Draft:
        audience = card["面向谁"].split("；")[0]
        problem = card["面向谁"].split("：", 1)[-1] if "：" in card["面向谁"] else "见正文"
        fact_lines = [f"{i}. {f}" for i, f in enumerate(facts[:5], 1)]
        if not fact_lines:
            fact_lines = ["（本轮素材未提供可引用事实）"]
        lines = [
            f"📌 适合谁：{audience}",
            f"❓ 解决什么：{problem}",
            "",
            "✅ 事实要点（附来源链接，可自行核验）：",
            *fact_lines,
            "",
            f"💡 一句话价值：{card['核心价值']}",
            f"⚠️ 冷静提醒：{card['最强反证'].split('；')[0]}",
            "",
            "#AI工具 #AI教程 #效率工具 #普通人用AI",
        ]
        return Draft(topic_id=card["topic_id"], platform="小红书",
                     title=card["拟定标题"], body="\n".join(lines), engine=ENGINE)
