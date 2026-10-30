"""选题引擎（PRD §5.4）：高分素材 → 5–10 张选题卡。

诚实性约束：
- 需求证据无直接证据时必须写「待验证：…」，禁止编造“很多人都在问”；
- 最强反证必填：取评分中最弱维度的理由，绝不允许只有正面理由的卡；
- 平台建议为初始运营假设（标注假设，不写成平台规则）。
"""

from __future__ import annotations

from datetime import datetime, timezone

from src.analysis.classify import classify_item
from src.analysis.scoring import SCORING_VERSION, score_item

# 内容类型 → (标题模板, 建议平台)。建议平台是初始假设，用户可在关口改。
TYPE_TEMPLATES = {
    "工具教程": ("实操教程｜{title}", "两者"),
    "产品发布": ("上新了｜{title}", "抖音"),
    "行业动态": ("行业快讯｜{title}", "抖音"),
    "研究解读": ("一分钟看懂｜{title}", "小红书"),
    "观点评论": ("观点拆解｜{title}", "抖音"),
    "安全事件": ("安全警示｜{title}", "小红书"),
}

AUDIENCE_BY_TYPE = {
    "工具教程": "想把 AI 用于工作提效的职员/小老板（无需编程）",
    "产品发布": "关注新工具能否替自己省钱的普通使用者",
    "行业动态": "想跟上 AI 行业变化、怕信息落后的职场人",
    "研究解读": "对 AI 能力边界好奇的非技术读者",
    "观点评论": "想听人话版分析的个人项目/副业关注者",
    "安全事件": "用 AI 办公但担心数据与账号安全的人",
}


def build_topic_cards(materials: list[dict], top_k: int = 8,
                      now: datetime | None = None,
                      profile=None) -> tuple[list[dict], list[dict]]:
    """返回 (选题卡列表, 打分明细列表)。

    卡片按总分降序取前 top_k（上限 10）；不足 5 张时原样返回并打
    insufficient 标记（调用方决定是否仍写入 Notion）。
    profile：可选受众画像（任务05），传入则评分使用画像信号。
    """
    now = now or datetime.now(timezone.utc)
    details = []
    for it in materials:
        ctype, creason = classify_item(it)
        sc = score_item(it, now=now, profile=profile)
        details.append({"item": it, "content_type": ctype, "classify_reason": creason,
                        "score": sc})
    details.sort(key=lambda d: (-d["score"]["total"], d["item"]["item_id"]))

    top_k = min(top_k, 10)
    cards: list[dict] = []
    for i, d in enumerate(details[:top_k], start=1):
        it, sc, ctype = d["item"], d["score"], d["content_type"]
        title_tpl, platform = TYPE_TEMPLATES[ctype]
        weak = sorted(sc["dimensions"], key=lambda x: x["points"])[:2]
        weakest_txt = "；".join(f"{w['name']}({w['grade']}：{w['reason']})" for w in weak)
        strong = next(x for x in sc["dimensions"] if x["name"] == "信息可信度")
        cost = next(x for x in sc["dimensions"] if x["name"] == "制作成本")
        audience = AUDIENCE_BY_TYPE[ctype]
        cards.append({
            "topic_id": f"T-{now.strftime('%Y%m%d')}-{i:03d}",
            "拟定标题": title_tpl.format(title=(it.get("title_or_summary") or "")[:60]),
            "建议平台": platform,
            "面向谁": f"{audience}；他们想解决：{_audience_problem(ctype)}",
            "核心价值": f"用一条内容讲清「{ctype}」：{(it.get('title_or_summary') or '')[:50]}",
            "为什么现在": (d["score"]["dimensions"][3]["reason"]
                          or "未取得发布时间") + "；事件仍在关注窗口内",
            "关联素材": [it["item_id"]],
            "_source_urls": [it["source_url"]] if it.get("source_url") else [],
            "需求证据": "待验证：本轮素材池无直接用户需求证据（无评论/帖子数据）；"
                        f"仅假设「{audience}」关心此类内容，须在文案阶段或评论区补证",
            "可信度与风险": f"证据等级 {it.get('evidence_status')}（{strong['reason']}）；"
                           f"风险：二手转述可能失真，引用前须打开原始链接核验",
            "最强反证": f"最弱两维 → {weakest_txt}；此外若该消息已被大量账号报道，"
                      "同题内容竞争激烈，新号未必有差异化空间",
            "差异与难度": f"本轮为账号首轮内容，暂无历史对比（差异待下轮复盘补）；"
                        f"制作难度：{cost['grade']}（{cost['reason']}）",
            "推荐级别": sc["level"],
            "推荐理由": f"总分 {sc['total']}/14（{SCORING_VERSION}，启发式未验证）："
                      + "；".join(f"{x['name']}{x['grade']}" for x in sc["dimensions"]),
            "内容类型": ctype,
            "用户选择结果": "待用户选择",
        })
    return cards, details


def _audience_problem(ctype: str) -> str:
    return {
        "工具教程": "想把新工具用到自己的文档/表格流程里但不知道从哪步开始",
        "产品发布": "不知道新功能对自己有没有用、要不要换工具",
        "行业动态": "信息过载，分不清哪些变化和自己有关",
        "研究解读": "看不懂论文，想知道能力边界和自己有什么关系",
        "观点评论": "想判断各方说法谁有道理，避免被带节奏",
        "安全事件": "担心自己用 AI 处理的数据和账号会不会出事",
    }[ctype]


REQUIRED_CARD_FIELDS = ["topic_id", "拟定标题", "建议平台", "面向谁", "核心价值",
                        "为什么现在", "关联素材", "需求证据", "可信度与风险",
                        "最强反证", "差异与难度", "推荐级别", "推荐理由"]


def validate_topic_cards(cards: list[dict], material_ids: set[str],
                         strict: bool = True) -> None:
    """校验选题卡满足 PRD 5.4（字段完整、引用真实素材、数量 5—10）。

    strict=False 时允许不足 5 张（小样例场景，由调用方标记 insufficient）。
    """
    if strict and not 5 <= len(cards) <= 10:
        raise ValueError(f"选题卡数量须为 5—10，当前 {len(cards)}")
    for c in cards:
        for f in REQUIRED_CARD_FIELDS:
            if not c.get(f):
                raise ValueError(f"{c.get('topic_id', '?')} 缺少字段 {f}")
        for iid in c["关联素材"]:
            if iid not in material_ids:
                raise ValueError(f"{c['topic_id']} 引用了不存在的素材 {iid}")
        if not str(c.get("需求证据", "")).startswith("待验证") and \
                "证据" not in str(c.get("需求证据", "")):
            raise ValueError(f"{c['topic_id']} 需求证据必须诚实标注（待验证/证据说明）")
