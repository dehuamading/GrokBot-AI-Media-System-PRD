"""评分引擎（PRD §5.3）：七维可解释评分 → 推荐级别。

设计约束（红线级）：
- 每个维度只有 高(2)/中(1)/低(0) 三档，附可解释理由 —— 不伪装成精确模型；
- 「传播潜力」维度显式标注为假设，不得用于宣称预测传播；
- 评分版本号 SCORING_VERSION 随结果一起入库，未来调权重必须换版本并验证（PRD 5.3）。

本引擎为启发式（AI 推断假设 HYP-002），用途仅限排序辅助。
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from src.analysis.audience import match_audience

SCORING_VERSION = "v2-audience"  # v1-heuristic → 任务05：受众画像驱动（负面信号降权）

DIMS = ["真实用户价值", "信息可信度", "受众匹配", "新颖与时效",
        "可讲清与可验证", "传播潜力假设", "制作成本"]

# 普通人可上手信号（PRD 1.1 目标用户：职场提效者/非程序员/个人项目副业）
HANDS_ON_KEYWORDS = ["教程", "如何", "指南", "免费", "开源", "本地", "上手", "步骤",
                     "实测", "模板", "提示词", "prompt", "无需编程"]
AUDIENCE_KEYWORDS = ["办公", "文档", "表格", "pdf", "ppt", "演示", "笔记", "工作流",
                     "副业", "自动化", "效率", "小企业", "团队", "写作", "翻译"]
TENSION_KEYWORDS = ["翻倍", "首个", "开源", "免费", "暴跌", "裁员", "警告", "停服",
                    "大涨", "首度", "首次", "突破", "泄露"]
EVIDENCE_POINTS = {"verified": 2, "self_reported": 1, "third_party": 1, "unverified": 0}


def _grade(points: int) -> str:
    return {2: "高", 1: "中", 0: "低"}[points]


def _dim(name: str, points: int, reason: str) -> dict:
    return {"name": name, "grade": _grade(points), "points": points, "reason": reason}


def score_item(item: dict, now: datetime | None = None,
                profile=None) -> dict:
    """对单条素材评分，返回 {total, level, version, dimensions[], notes}。

    profile：可选 src.analysis.audience.AudienceProfile（任务05）。传入时：
    - 「受众匹配」维度改用画像信号（高/中信号 + 负面信号硬归零）；
    - 命中负面信号（受众无关，如加密货币/摃毛/外国政治）再对总分降权，
      保证与受众无关的内容低分（任务05 硬要求）；评分版本号携带 profile_id。
    """
    now = now or datetime.now(timezone.utc)
    text = ((item.get("title_or_summary") or "") + " " +
            " ".join(item.get("claims") or [])).lower()

    # 1) 真实用户价值：动手型内容 + 类目偏向
    hands = [k for k in HANDS_ON_KEYWORDS if k in text]
    cat = (item.get("extra") or {}).get("category") or ""
    value_pts = min(2, len(hands) + (1 if cat == "tip" else 0))
    r1 = f"命中可上手信号 {len(hands)} 个" + ("，类目 tip 加分" if cat == "tip" else "")

    # 2) 信息可信度：证据等级映射
    ev = item.get("evidence_status") or "unverified"
    r2 = f"证据等级 {ev}（{'官方渠道' if ev == 'verified' else '具名第三方' if ev == 'third_party' else '来源可核' if ev == 'self_reported' else '无交叉核验'}）"

    # 3) 受众匹配：画像信号优先（任务05）；无画像时回退 v1 人群场景词
    aud_notes: list[str] = []
    if profile is not None:
        aud_pts, aud_notes, neg_hits = match_audience(text, profile)
        r3 = "; ".join(aud_notes)
    else:
        aud = [k for k in AUDIENCE_KEYWORDS if k in text]
        aud_pts = 2 if len(aud) >= 2 else (1 if len(aud) == 1 else 0)
        r3 = f"命中目标人群场景词 {aud[:3]}" if aud else "未命中显式人群场景词（待验证）"
        neg_hits = []

    # 4) 新颖与时效：发布时间距 now
    pub = item.get("published_at")
    fresh_pts, r4 = 0, "无发布时间（按最低档计）"
    if pub:
        try:
            dt = datetime.fromisoformat(pub.replace("Z", "+00:00"))
            if dt.tzinfo is None:  # 无时区标记（如 aihot 归一化后的截断串）→ 视为 UTC
                dt = dt.replace(tzinfo=timezone.utc)
            age = now - dt
            if age <= timedelta(days=2):
                fresh_pts, r4 = 2, f"发布于 {age.days} 天内（新鲜）"
            elif age <= timedelta(days=7):
                fresh_pts, r4 = 1, f"发布于 {age.days} 天内（一周内）"
            else:
                r4 = f"发布已超过 7 天（{age.days} 天）"
        except ValueError:
            r4 = f"发布时间无法解析：{pub}"

    # 5) 可讲清与可验证：有可打开出处 + 可复现要素
    url = item.get("source_url") or ""
    steps = [k for k in ("步骤", "指南", "教程", "实测", "如何") if k in text]
    ver_pts = (1 if url else 0) + (1 if steps else 0)
    r5 = ("有原始出处链接" if url else "缺原始出处链接") + ("；含可复现要素" if steps else "；无可复现要素")

    # 6) 传播潜力（假设维度）
    tension = [k for k in TENSION_KEYWORDS if k in text]
    spread_pts = min(2, len(tension))
    r6 = f"标题张力词 {tension[:3]}（仅假设，未经数据验证）"

    # 7) 制作成本：反向计分（低门槛→高分）
    costly = [k for k in ("融资", "上市", "s-1", "股价", "招股", "市值", "间谍") if k in text]
    cost_pts = 2 if not costly and hands else (0 if costly else 1)
    r7 = ("无需实测/无需授权素材，普通人可自行复现" if cost_pts == 2 else
          "需自行核验部分事实" if cost_pts == 1 else
          f"涉及 {costly[:2]}，事实核验/素材成本高")

    dims = [
        _dim(DIMS[0], value_pts, r1), _dim(DIMS[1], EVIDENCE_POINTS[ev], r2),
        _dim(DIMS[2], aud_pts, r3), _dim(DIMS[3], fresh_pts, r4),
        _dim(DIMS[4], min(2, ver_pts), r5), _dim(DIMS[5], spread_pts, r6),
        _dim(DIMS[6], cost_pts, r7),
    ]
    total = sum(d["points"] for d in dims)  # 0–14
    neg_penalty = 0
    if neg_hits:
        neg_penalty = 2 * min(2, len(neg_hits))
        total = max(0, total - neg_penalty)
    level = "推荐" if total >= 10 else ("备选" if total >= 7 else "观察")
    version = SCORING_VERSION if profile is None else f"{SCORING_VERSION}+{profile.profile_id}"
    notes = ("七维启发式评分（HYP-002，未验证），仅用于排序辅助，不承诺传播效果")
    if neg_penalty:
        notes += f"；受众无关信号降权 -{neg_penalty}（画像 {profile.profile_id}）"
    return {
        "total": total,
        "level": level,
        "version": version,
        "dimensions": dims,
        "notes": notes,
    }


# ===========================================================================
# v3-dual（阶段3）：9 维计分 + 双轨排序；保留上方 score_item v2 可回退。
# ===========================================================================
from src.analysis.demand import (demand_evidence_for_item,  # noqa: E402
                                 sanitize_evidence)

SCORING_VERSION_V3 = "v3-dual"
V3_DIMS = ["真实用户价值", "信息可信度", "受众匹配", "新颖与时效",
           "可讲清与可验证", "制作成本", "国内适用性", "新技术机会", "副业潜力"]
V3_MAX_TOTAL = 18

# 阈值校准（写成常量便于审计与回退）：
# - 旧 7 维满分 14，推荐 ≥10（≈71%）、备选 ≥7（≈50%）；
# - 新 9 维满分 18，按同等比例取整：推荐 ≥12（≈67%）、备选 ≥9（≈50%）。
# - 推荐还必须满足对应轨道分 ≥3，避免“总分高但本轨道没证据”的误推。
V3_RECOMMEND_TOTAL = 12
V3_ALTERNATIVE_TOTAL = 9
V3_TRACK_RECOMMEND_MIN = 3

TRACK_WEIGHTS = {
    "需求型": ["真实用户价值", "可讲清与可验证", "国内适用性"],
    "机会型": ["新技术机会", "新颖与时效", "国内适用性"],
}

_SIDE_HUSTLE_LABELS = {
    "真实案例": 2,
    "潜在机会": 1,
    "个人经验": 1,
    "未经验证宣传": 0,
}
_UNVERIFIED_PROMO = ["月入", "日入", "躺赚", "稳赚", "保证收益", "保证赚",
                     "轻松赚", "暴富", "包赚", "零成本月入", "收益翻倍"]
_REAL_CASE = ["真实案例", "亲测收入", "学员案例", "实测收入", "案例：", "客户案例"]
_PERSONAL_EXP = ["个人经验", "我试过", "我自己", "自述", "亲身"]
_POTENTIAL = ["副业", "变现", "接单", "赚钱", "创业", "机会", "私域", "带货"]


def _side_hustle(item: dict, text: str) -> tuple[int, str, str]:
    """副业证据分级：真实案例 2 / 潜在机会 1 / 个人经验 1 / 未经验证宣传 0。"""
    explicit = (item.get("side_hustle_evidence") or item.get("副业证据等级")
                or (item.get("extra") or {}).get("side_hustle_evidence") or "")
    explicit = str(explicit).strip()
    if explicit in _SIDE_HUSTLE_LABELS:
        return (_SIDE_HUSTLE_LABELS[explicit], explicit,
                f"用户/素材标注证据等级「{explicit}」")
    if "未经验证宣传" in text or "未经验证" in text:
        return 0, "未经验证宣传", "标注为未经验证宣传，按 0 分处理（不作任何承诺）"
    promo = [k for k in _UNVERIFIED_PROMO if k in text]
    if promo:
        return 0, "未经验证宣传", "命中未经验证的赚钱宣传用语，按 0 分处理（不作任何承诺）"
    real = [k for k in _REAL_CASE if k in text]
    if real:
        return 2, "真实案例", "出现可追溯的真实案例信号，最高按 2 分"
    personal = [k for k in _PERSONAL_EXP if k in text]
    if personal:
        return 1, "个人经验", "个人经验/自述，按 1 分封顶"
    potential = [k for k in _POTENTIAL if k in text]
    if potential:
        return 1, "潜在机会", "存在潜在机会信号，但无已核验案例，按 1 分封顶"
    return 0, "无证据", "未提供副业相关证据"


def _china_usability(item: dict, text: str) -> tuple[int, str, bool]:
    """国内适用性：可用 2 / 需梯子或外币卡 1 / 不可用 0 / 待验证 0。"""
    explicit = (item.get("china_usability") or item.get("国内可用性")
                or (item.get("extra") or {}).get("china_usability") or "")
    explicit = str(explicit).strip()
    if explicit:
        if any(k in explicit for k in ("不可用", "无法访问", "被墙", "封禁")):
            return 0, f"素材标注：{explicit}", False
        if any(k in explicit for k in ("需梯子", "外币卡", "需要订阅", "需付费", "受限")):
            return 1, f"素材标注：{explicit}", False
        if any(k in explicit for k in ("待验证", "未知", "不确定")):
            return 0, f"素材标注：{explicit}（待验证）", True
        if "可用" in explicit or "能用" in explicit:
            return 2, f"素材标注：{explicit}", False

    if any(k in text for k in ("国内不可用", "无法访问", "被墙", "封禁", "不支持国内")):
        return 0, "文本提示国内不可用", False
    if any(k in text for k in ("能用吗", "可用吗", "能用？", "可用？",
                               "有没有国内能用", "能不能用")):
        return 0, "待验证：文本是询问而非可用性证据", True
    if any(k in text for k in ("国内可用", "国内能用", "不用翻墙", "免翻墙",
                               "无需翻墙", "直接可用", "支持国内", "国内直接")):
        return 2, "文本提示国内可直接使用", False
    if any(k in text for k in ("需要梯子", "需梯子", "翻墙", "科学上网",
                               "外币卡", "国外信用卡", "需要订阅", "需付费")):
        return 1, "文本提示需要梯子/外币卡/付费订阅", False
    return 0, "待验证：未取得国内可用性证据", True


def _new_tech_opportunity(item: dict, text: str) -> tuple[int, str]:
    cat = (item.get("extra") or {}).get("category") or ""
    signals = [k for k in ("新能力", "新模型", "新功能", "首次", "首个", "突破",
                           "多模态", "agent", "智能体", "降低门槛", "零基础",
                           "免费", "开源", "降价", "成本降低", "效率提升")
               if k in text]
    if cat in ("ai-products", "ai-models"):
        signals.append(f"类目 {cat}")
    if len(signals) >= 2:
        return 2, f"命中新能力/降门槛/降成本信号 {len(signals)} 个"
    if len(signals) == 1:
        return 1, f"命中单个新技术机会信号「{signals[0]}」"
    return 0, "未命中新能力/降门槛/降成本信号"


def _propagation_hypothesis(text: str) -> dict:
    tension = [k for k in TENSION_KEYWORDS if k in text]
    if tension:
        detail = f"标题张力词 {tension[:3]}"
    else:
        detail = "无显式张力词"
    return {
        "text": f"传播潜力假设：{detail}（仅文字假设，未计分，不承诺传播效果）",
        "not_scored": True,
    }


def _demand_stats(item: dict) -> tuple[list[dict], dict[str, int], bool]:
    evidence = demand_evidence_for_item(item)
    counts: dict[str, int] = {}
    for ev in evidence:
        for tag in ev["tags"]:
            if tag != "其他":
                counts[tag] = counts.get(tag, 0) + 1
    return evidence, counts, bool(counts)


def level_v3(total: int, track_score: int) -> str:
    """v3 推荐级别：总分阈值 + 对应轨道分下限。"""
    if total >= V3_RECOMMEND_TOTAL and track_score >= V3_TRACK_RECOMMEND_MIN:
        return "推荐"
    if total >= V3_ALTERNATIVE_TOTAL:
        return "备选"
    return "观察"


def _rule_adjustment(text: str, active_rules: list[str] | None) -> tuple[int, list[str]]:
    """启发式评分只允许「正在使用」规则加减分；示例/候选规则不得传入。"""
    if not active_rules:
        return 0, []
    text_l = text.lower()
    adjustment, matches = 0, []
    stop = {"优先选择", "优先", "避免", "选择", "多做", "少做", "不要",
            "用户", "认可", "否决", "规则", "候选", "选题", "方向",
            "保持", "当前", "标准", "类选题"}
    for rule in active_rules:
        rule_text = str(rule or "")
        negative = any(k in rule_text for k in ("避免", "否决", "不要", "少做"))
        positive = any(k in rule_text for k in ("优先", "多做", "选择", "认可", "保持"))
        if not (negative or positive):
            continue
        tokens = re.findall(r"[\u4e00-\u9fff]{2,}|[A-Za-z]{3,}", rule_text)
        tokens = [t for t in tokens if t not in stop]
        if any(t.lower() in text_l for t in tokens):
            if negative:
                adjustment -= 1
                matches.append(f"-1：{rule_text[:40]}")
            else:
                adjustment += 1
                matches.append(f"+1：{rule_text[:40]}")
    return max(-2, min(2, adjustment)), matches


def score_item_v3(item: dict, now: datetime | None = None,
                  profile=None, demand: list[dict] | None = None,
                  active_rules: list[str] | None = None) -> dict:
    """9 维 0/1/2 可解释评分（满分 18）+ 双轨得分；传播潜力仅文字假设不计分。

    demand：可选的已分类评论需求证据（含 tags/text/source_url）；不传时从
    item.comments[].demand_tags 读取。
    """
    now = now or datetime.now(timezone.utc)
    text = ((item.get("title_or_summary") or "") + " " +
            " ".join(item.get("claims") or [])).lower()

    evidence, demand_counts, has_demand = _demand_stats(item)
    if demand is not None:
        evidence = demand
        demand_counts = {}
        for ev in evidence:
            for tag in ev.get("tags") or []:
                if tag != "其他":
                    demand_counts[tag] = demand_counts.get(tag, 0) + 1
        has_demand = bool(demand_counts)

    # 1) 真实用户价值（需求真实性并入；≥3 条同类评论原文可升档）
    hands = [k for k in HANDS_ON_KEYWORDS if k in text]
    cat = (item.get("extra") or {}).get("category") or ""
    value_pts = min(2, len(hands) + (1 if cat == "tip" else 0))
    r1 = f"命中可上手信号 {len(hands)} 个" + ("，类目 tip 加分" if cat == "tip" else "")
    if demand_counts:
        top_cat, top_n = max(demand_counts.items(), key=lambda kv: kv[1])
        if top_n >= 3:
            value_pts = min(2, value_pts + 1)
            r1 += f"；同类评论原文 {top_n} 条（{top_cat}）→ 升档"

    # 2) 信息可信度
    ev = item.get("evidence_status") or "unverified"
    cred_pts = EVIDENCE_POINTS.get(ev, 0)
    r2 = f"证据等级 {ev}（{'官方渠道' if ev == 'verified' else '具名第三方' if ev == 'third_party' else '来源可核' if ev == 'self_reported' else '无交叉核验'}）"

    # 3) 受众匹配
    aud_notes: list[str] = []
    if profile is not None:
        aud_pts, aud_notes, _neg = match_audience(text, profile)
        r3 = "; ".join(aud_notes)
    else:
        aud = [k for k in AUDIENCE_KEYWORDS if k in text]
        aud_pts = 2 if len(aud) >= 2 else (1 if len(aud) == 1 else 0)
        r3 = f"命中目标人群场景词 {aud[:3]}" if aud else "未命中显式人群场景词（待验证）"

    # 4) 新颖与时效
    pub = item.get("published_at")
    fresh_pts, r4 = 0, "无发布时间（按最低档计）"
    if pub:
        try:
            dt = datetime.fromisoformat(str(pub).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            age = now - dt
            if age <= timedelta(days=2):
                fresh_pts, r4 = 2, f"发布于 {age.days} 天内（新鲜）"
            elif age <= timedelta(days=7):
                fresh_pts, r4 = 1, f"发布于 {age.days} 天内（一周内）"
            else:
                r4 = f"发布已超过 7 天（{age.days} 天）"
        except ValueError:
            r4 = f"发布时间无法解析：{pub}"

    # 5) 可讲清与可验证
    url = item.get("source_url") or ""
    steps = [k for k in ("步骤", "指南", "教程", "实测", "如何") if k in text]
    ver_pts = (1 if url else 0) + (1 if steps else 0)
    r5 = ("有原始出处链接" if url else "缺原始出处链接") + (
        "；含可复现要素" if steps else "；无可复现要素")

    # 6) 制作成本（低成本=2）
    costly = [k for k in ("融资", "上市", "s-1", "股价", "招股", "市值", "间谍") if k in text]
    cost_pts = 2 if not costly and hands else (0 if costly else 1)
    r6 = ("无需实测/无需授权素材，普通人可自行复现" if cost_pts == 2 else
          "需自行核验部分事实" if cost_pts == 1 else
          f"涉及 {costly[:2]}，事实核验/素材成本高")

    # 7) 国内适用性
    china_pts, r7, china_pending = _china_usability(item, text)
    if china_pending:
        r7 = "待验证：" + r7.replace("待验证：", "")

    # 8) 新技术机会
    tech_pts, r8 = _new_tech_opportunity(item, text)

    # 9) 副业潜力（证据分级，任何情况不写收益承诺）
    side_pts, side_level, r9 = _side_hustle(item, text)

    dims = [
        _dim(V3_DIMS[0], value_pts, r1),
        _dim(V3_DIMS[1], cred_pts, r2),
        _dim(V3_DIMS[2], aud_pts, r3),
        _dim(V3_DIMS[3], fresh_pts, r4),
        _dim(V3_DIMS[4], ver_pts, r5),
        _dim(V3_DIMS[5], cost_pts, r6),
        _dim(V3_DIMS[6], china_pts, r7),
        _dim(V3_DIMS[7], tech_pts, r8),
        _dim(V3_DIMS[8], side_pts, r9),
    ]
    total = sum(d["points"] for d in dims)
    track = "需求型" if has_demand else "机会型"
    by_name = {d["name"]: d["points"] for d in dims}
    track_score = sum(by_name[n] for n in TRACK_WEIGHTS[track])
    rule_adjustment, rule_matches = _rule_adjustment(text, active_rules)
    adjusted_total = max(0, min(V3_MAX_TOTAL, total + rule_adjustment))
    adjusted_track_score = max(0, min(6, track_score + rule_adjustment))
    level = level_v3(adjusted_total, adjusted_track_score)

    sanitized_evidence = [{
        "text": sanitize_evidence(e.get("text", "")),
        "tags": list(e.get("tags") or []),
        "source_url": e.get("source_url", ""),
    } for e in evidence]

    return {
        "total": total,
        "max_total": V3_MAX_TOTAL,
        "rule_adjustment": rule_adjustment,
        "rule_matches": rule_matches,
        "adjusted_total": adjusted_total,
        "adjusted_track_score": adjusted_track_score,
        "ranking_total": adjusted_total,
        "ranking_track_score": adjusted_track_score,
        "level": level,
        "version": SCORING_VERSION_V3,
        "score_version": SCORING_VERSION_V3,
        "type": track,
        "track": track,
        "track_score": track_score,
        "track_dimensions": TRACK_WEIGHTS[track],
        "track_reason": " + ".join(f"{n}{by_name[n]}" for n in TRACK_WEIGHTS[track]),
        "dimensions": dims,
        "propagation_hypothesis": _propagation_hypothesis(text),
        "propagation_not_scored": True,
        "side_hustle_evidence_level": side_level,
        "demand_evidence_count": len(evidence),
        "demand_evidence": sanitized_evidence,
        "china_pending": china_pending,
        "notes": ("九维启发式评分（v3-dual，未验证），仅用于排序辅助，"
                  "不承诺传播效果；传播潜力假设不计分"),
    }


def _track_of(scored: dict) -> str:
    t = scored.get("type") or scored.get("track") or scored.get("选题类型")
    return t if t in ("需求型", "机会型") else "机会型"


def _sort_key(scored: dict):
    ident = scored.get("item_id") or scored.get("id") or scored.get("topic_id") or ""
    track_score = (scored.get("ranking_track_score")
                   or scored.get("track_score")
                   or scored.get("轨道得分") or 0)
    total = (scored.get("ranking_total")
             or scored.get("total")
             or scored.get("综合评分") or 0)
    return (-int(track_score), -int(total), str(ident))


def _source_of(scored: dict) -> str:
    source = str(scored.get("_source_group") or scored.get("source_group")
                 or scored.get("source_type") or scored.get("platform") or "")
    return "x_posts" if source == "x" else source


def _reaches_alternative(scored: dict) -> bool:
    level = scored.get("推荐级别") or scored.get("level")
    if level in ("推荐", "备选"):
        return True
    total = (scored.get("ranking_total") or scored.get("adjusted_total")
             or scored.get("综合评分") or scored.get("total") or 0)
    try:
        return int(total) >= V3_ALTERNATIVE_TOTAL
    except (TypeError, ValueError):
        return False


def merge_dual_tracks(scored_items: list[dict], top_n: int = 10,
                      opportunity_sources: tuple[str, ...] | None = None
                      ) -> list[dict]:
    """双轨分别排序后按「需求型/机会型」交替合并，保证两类都有（除非某类为 0）。

    opportunity_sources：机会型来源配额。对每个来源，若存在达到「备选」及以上的
    机会型卡，则优先纳入合并结果（每类来源至少尝试 1 张）；未达备选的不强塞。
    """
    demand = sorted([x for x in scored_items if _track_of(x) == "需求型"],
                    key=_sort_key)
    opportunity = sorted([x for x in scored_items if _track_of(x) == "机会型"],
                         key=_sort_key)
    mandatory: list[dict] = []
    for source in opportunity_sources or ():
        for item in opportunity:
            if _source_of(item) == source and _reaches_alternative(item):
                if item not in mandatory:
                    mandatory.append(item)
                break

    limit = max(0, int(top_n))
    merged: list[dict] = []
    while len(merged) < limit and (demand or opportunity):
        if demand:
            merged.append(demand.pop(0))
        if len(merged) < limit and opportunity:
            pick = None
            for item in mandatory:
                if item in opportunity:
                    pick = item
                    break
            if pick is None:
                pick = opportunity[0]
            opportunity.remove(pick)
            merged.append(pick)

    # 若交替合并后配额卡仍未进入（例如需求型占满），替换末尾的非配额卡。
    for item in mandatory:
        if item in merged:
            continue
        for i in range(len(merged) - 1, -1, -1):
            if merged[i] not in mandatory:
                merged[i] = item
                break
    return merged
