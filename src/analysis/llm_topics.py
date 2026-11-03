"""任务05：LLM 辅助分析（GLM）——帖子打分 + 选题卡撰写，附硬校验。

流程（两段式，控制调用成本与幻觉面）：
1) shortlist()：本地启发式（受众画像信号 + 互动数据）从大池子（如 1000+ 帖）预筛
   出 top-N（默认 60）——不花 LLM 调用；
2) glm_score_posts()：GLM 分批（默认 10 条/批）按受众画像打 0-10 分并给理由；
3) glm_build_cards()：取打分 top 的帖子（默认 24 条）让 GLM 撰写 5-8 张中文选题卡。

红线与防幻觉约束（代码层强制，不信任 LLM 输出）：
- 选题卡引用的帖子只允许来自输入素材池（refs 按 n 序号映射回 item_id）；
- 帖子链接一律取素材记录里的 source_url（即输入文件 url 字段原样），绝不用 LLM 生成的链接；
- 每张卡必填字段缺失 → 丢弃该卡（宁缺毋滥），数量 <5 时标记 insufficient 由调用方决定；
- GLM 失败抛异常，由调用方回退启发式路径（绝不静默编造）。
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

from src.analysis.audience import AudienceProfile, match_audience
from src.analysis.demand import short_evidence_texts
from src.analysis.fields import (audience_user, filled, normalize_china_usability,
                                 paid_label, parse_audience_categories)
from src.analysis.scoring import (OVERSEAS_OPPORTUNITY_LABEL,
                                  SCORING_VERSION_V3, score_item_v3)
from src.analysis.safety import scrub_card_forbidden_steps
from src.llm.glm import GLMChat, GLMParseError

ENGLISH_STOP = re.compile(r"[a-zA-Z]")


def _chat_json(chat, system: str, user: str, **kw) -> dict:
    """兼容只接受 (system, user) 的测试/第三方客户端。"""
    try:
        return chat.chat_json(system, user, **kw)
    except TypeError:
        return chat.chat_json(system, user)


# ---------------------------------------------------------------------------
# 1) 本地预筛（无 LLM 调用）
# ---------------------------------------------------------------------------
def _engagement(m: dict) -> int:
    e = m.get("extra") or {}
    return ((e.get("likes") or 0) + 2 * (e.get("reposts") or 0) +
            (e.get("replies") or 0) + (e.get("quotes") or 0))


def _shortlist_source(m: dict) -> str:
    source = str(m.get("source_type") or m.get("platform") or "")
    return "x_posts" if source == "x" else source


def shortlist(materials: list[dict], k: int = 60,
              profile: AudienceProfile | None = None,
              ensure_sources: tuple[str, ...] = (),
              per_source_min: int = 1) -> list[dict]:
    """受众信号 + 互动数据的本地预筛。负面信号（加密货币/撸毛/政治等）重罚出局优先级。

    - 命中受众无关信号（负面词表：加密货币/摃毛/外国政治等）→ 直接不入候选；
    - 语言：只留画像语言（默认 zh/en）；无 lang 标记（如 aihot 条目）视为可留；
    - 文本过短（<15 字符）跳过（多为纯链接/表情，讲不成选题）；
    - ensure_sources/per_source_min：为指定来源保留最低候选数（默认不启用，
      保持旧调用行为），确保 aihot/github/official 有机会进入后续 LLM 出卡。
    """
    scored = []
    for m in materials:
        extra = m.get("extra") or {}
        lang = extra.get("lang")
        if lang and profile and lang not in profile.languages:
            continue
        base_text = (m.get("title_or_summary") or
                     ((m.get("claims") or [""])[0]))
        # 长度检查前去链接：纯链接/表情短帖讲不成选题
        bare = re.sub(r"https?://\S+", "", base_text).strip()
        if len(bare) < 15:
            continue
        text = base_text + " " + " ".join(m.get("claims") or [])
        pts = 0
        if profile is not None:
            p, _reasons, neg = match_audience(text, profile)
            if neg:
                continue  # 命中受众无关信号（加密货币/摃毛/政治等）→ 不进候选
            pts += 2 * p
        eng = _engagement(m)
        pts += min(4, eng // 50)  # 互动信号封顶 4，防大V碾压内容相关性
        scored.append((pts, eng, m))
    scored.sort(key=lambda t: (-t[0], -t[1]))
    ordered = [m for _, _, m in scored]
    limit = max(0, int(k))
    if not ensure_sources or limit == 0:
        return ordered[:limit]

    reserved: list[dict] = []
    reserved_ids: set[int] = set()
    for source in ensure_sources:
        picks = [m for m in ordered
                 if _shortlist_source(m) == source
                 and id(m) not in reserved_ids][:max(0, per_source_min)]
        for m in picks:
            reserved.append(m)
            reserved_ids.add(id(m))
    if len(reserved) >= limit:
        return reserved[:limit]
    for m in ordered:
        if len(reserved) >= limit:
            break
        if id(m) not in reserved_ids:
            reserved.append(m)
            reserved_ids.add(id(m))
    selected_ids = {id(m) for m in reserved}
    return [m for m in ordered if id(m) in selected_ids]


# ---------------------------------------------------------------------------
# 2) GLM 打分
# ---------------------------------------------------------------------------
SCORE_SYSTEM = (
    "你是中文 AI 自媒体选题分析师。给定受众画像和一批 X 帖子，逐条按「对该受众的"
    "选题价值」打 0-10 分（10=非常适合做成一条短视频选题）。评分要点："
    "①与受众兴趣直接相关且可操作（教程/工具/方法/避坑）加分；"
    "②加密货币、撸毛空投、美国政治选举、纯金融行情类与受众无关 → 0-2 分；"
    "③单一帖子信息量太小讲不成一条内容 → 低分；互动高说明话题热 → 适当加分；"
    "④中文受众可消费（能翻译/转述清楚）为准，英文帖不因语言本身扣分。"
    '严格只输出 JSON：{"results":[{"n":1,"score":7,"reason":"一句话理由"}]}'
)


def _post_block(n: int, m: dict, with_url: bool = False) -> str:
    e = m.get("extra") or {}
    text = (m.get("title_or_summary") or "").replace("\n", " ")[:420]
    author = (m.get("author_or_publisher") or "").split(" (")[0]
    parts = [f"#{n} 作者:{author}", f"互动:赞{e.get('likes', 0)}/转{e.get('reposts', 0)}"
             f"/阅读{e.get('impressions', 0)}", f"内容:{text}"]
    if with_url:
        parts.append(f"链接:{m.get('source_url') or ''}")
    return " | ".join(parts)


def glm_score_posts(chat: GLMChat, materials: list[dict],
                    profile: AudienceProfile, batch: int = 10,
                    preference_text: str = "") -> dict[str, dict]:
    """GLM 分批打分。返回 {item_id: {"score": float, "reason": str}}。单批解析失败重试一次。"""
    out: dict[str, dict] = {}
    for start in range(0, len(materials), batch):
        chunk = materials[start:start + batch]
        if not chunk:
            continue
        lines = [f"{_post_block(i + 1, m)}" for i, m in enumerate(chunk)]
        user = (f"受众画像：{profile.summary()}\n\n帖子列表（共 {len(chunk)} 条）：\n"
                + "\n".join(lines))
        system = SCORE_SYSTEM
        if preference_text:
            system += f"\n\n【用户偏好参考（仅参考）】\n{preference_text}"
        data = None
        for attempt in range(2):
            try:
                data = _chat_json(chat, system, user,
                                  temperature=0.1 if attempt else 0.2,
                                  max_tokens=8000)
                break
            except GLMParseError:  # noqa: PERF203
                if attempt:
                    raise
        for r in data.get("results") or []:
            try:
                n = int(r["n"])
                sc = float(r["score"])
            except (KeyError, TypeError, ValueError):
                continue
            if not 1 <= n <= len(chunk):
                continue
            out[chunk[n - 1]["item_id"]] = {"score": max(0.0, min(10.0, sc)),
                                            "reason": str(r.get("reason") or "")[:300]}
    return out


# ---------------------------------------------------------------------------
# 3) GLM 选题卡
# ---------------------------------------------------------------------------
CARD_SYSTEM = (
    "你是中文 AI 自媒体选题编辑。基于受众画像与已打分的 X 帖子，产出 5-8 张短视频选题卡。"
    "硬性要求：\n"
    "1) 每张卡引用 1-3 条帖子（给序号 refs），只能引用列表中的帖子，禁止编造；\n"
    "2) 加密货币/撸毛空投/美国政治类帖子绝不入卡；\n"
    "3) 标题、为什么适合、钩子、国内可用性、风险、最强反证全部中文；"
    "「国内可用性」写该工具/方法在国内能否直接用、有什么替代方案；不确定就写「待核实：…」，"
    "禁止编造事实；\n"
    "4) hook 是一句话短视频开头钩子（口语化，能留人）；counter 是最强反证"
    "（这条内容做出来最可能被质疑/扑街的点）；\n"
    "5) platform ∈ {抖音,小红书,两者}；score 为你给的 0-10 综合分；\n"
    "6) 标题必须是中文概括改写，禁止照抄帖子原文（尤其禁止连续 >20 字原文），"
    "禁止写入作者显示名、username 或 @handle；引用链接由程序从素材注入；\n"
    "7) 新字段必须尽量给出：target_user（只能从白名单选，可多选，用“、”连接："
    "AI普通使用者、AI内容创作者、AI编程与应用开发者、AI副业与创业探索者、"
    "国外AI工具的国内使用者；不确定则写“待验证”）、"
    "pain_or_opportunity、why_now、angle、paid（是否付费）、verifiable（能否实际验证）；"
    "确实不知道才写「待验证」，不得用空话；\n"
    '严格只输出 JSON：{"cards":[{"title":"…","why_fit":"…","hook":"…","refs":[1],'
    '"china":"…","risk":"…","counter":"…","difficulty":"…","platform":"抖音","score":8,'
    '"target_user":"…","pain_or_opportunity":"…","why_now":"…","angle":"…",'
    '"paid":"…","verifiable":"…"}]}'
)


def glm_build_cards(chat: GLMChat, materials: list[dict],
                    profile: AudienceProfile, now: datetime | None = None,
                    glm_scores: dict[str, dict] | None = None,
                    min_cards: int = 5, max_cards: int = 8,
                    pool_size: int = 16, preference_text: str = "",
                    card_type: str | None = None) -> tuple[list[dict], dict]:
    """GLM 撰写选题卡 → 硬校验 → 组装为 PRD 5.4 卡片结构。

    返回 (cards, meta)；meta 含 glm_model/calls/insufficient/dropped 等审计信息。
    """
    now = now or datetime.now(timezone.utc)
    top = materials
    if glm_scores:
        top = sorted(materials,
                     key=lambda m: -glm_scores.get(m["item_id"], {}).get("score", 0))
    refs_pool = top[:pool_size]
    by_n = {i + 1: m for i, m in enumerate(refs_pool)}

    lines = [f"{_post_block(n, m, with_url=True)}" for n, m in by_n.items()]
    user = (f"受众画像：{profile.summary()}\n\n候选帖子（{len(refs_pool)} 条，按打分降序）：\n"
            + "\n".join(lines) +
            f"\n\n请产出 {min_cards}-{max_cards} 张选题卡（JSON）。")
    data, last_err = None, None
    for attempt in range(3):
        sys_prompt = CARD_SYSTEM
        if preference_text:
            sys_prompt += f"\n\n【用户偏好参考（仅参考）】\n{preference_text}"
        if card_type in ("需求型", "机会型"):
            sys_prompt += (f"\n本批只生成「{card_type}」卡；"
                           + ("需求型卡必须引用评论原文证据（refs 指向含评论的素材）。"
                              if card_type == "需求型"
                              else "机会型卡来自 aihot/github/official/X 帖子。"))
        if attempt:
            sys_prompt += ("\n再次提醒：不要输出任何推理过程或 JSON 以外的文字，"
                           "直接以 { 开头、以 } 结尾输出完整 JSON；卡片数可减到下限。")
        try:
            data = _chat_json(chat, sys_prompt, user,
                              temperature=0.4 if attempt == 0 else 0.2,
                              max_tokens=12000)
            break
        except GLMParseError as e:  # 截断/夹带文本 → 降温重就（最多 3 次）
            last_err = e
    if data is None:
        raise last_err

    service_label = _service_label(chat)
    cards, dropped = [], []
    seen_titles: set[str] = set()
    for raw in (data.get("cards") or [])[: max_cards + 4]:
        card, why_drop = _assemble_card(raw, by_n, profile, now,
                                        glm_scores or {}, len(cards) + 1,
                                        default_type=card_type,
                                        service_label=service_label)
        if card is None:
            dropped.append(why_drop)
            continue
        title_key = card["拟定标题"].strip()
        if title_key in seen_titles:
            dropped.append(f"标题重复：{title_key[:30]}")
            continue
        seen_titles.add(title_key)
        cards.append(card)
        if len(cards) >= max_cards:
            break
    cards.sort(key=lambda c: -c["综合评分"])
    meta = {
        "engine": f"glm:{getattr(chat, 'model', 'unknown')}",
        "glm_calls": getattr(chat, "calls", 0),
        "pool": len(materials), "cards": len(cards),
        "dropped": dropped,
        "insufficient": len(cards) < min_cards,
    }
    return cards, meta


def _service_label(chat) -> str:
    """实际服务 provider/model；优先 provider_chain 的 last_provider/last_model。"""
    provider = (getattr(chat, "last_provider", None)
                or getattr(chat, "provider", None) or "llm")
    model = (getattr(chat, "last_model", None)
             or getattr(chat, "model", None) or "unknown")
    return f"{provider}/{model}"


def _assemble_card(raw: dict, by_n: dict[int, dict], profile: AudienceProfile,
                   now: datetime, glm_scores: dict, seq: int,
                   default_type: str | None = None,
                   service_label: str = "llm") -> tuple[dict | None, str]:
    """把 LLM 原始卡组装为完整卡片；字段缺失/引用非法 → (None, 丢弃原因)。"""
    title = str(raw.get("title") or "").strip()
    why = str(raw.get("why_fit") or "").strip()
    hook = str(raw.get("hook") or "").strip()
    china = str(raw.get("china") or "").strip()
    risk = str(raw.get("risk") or "").strip()
    counter = str(raw.get("counter") or "").strip()
    for name, v in (("title", title), ("why_fit", why), ("hook", hook),
                    ("china", china), ("risk", risk), ("counter", counter)):
        if not v:
            return None, f"缺必填字段 {name}"
    refs = raw.get("refs") or []
    try:
        refs = [int(r) for r in refs]
    except (TypeError, ValueError):
        return None, "refs 非法（非整数列表）"
    refs = [r for r in refs if r in by_n]
    if not 1 <= len(refs) <= 3:
        return None, f"refs 数量非法（有效 {len(refs)}，须 1-3）"
    mats = [by_n[r] for r in refs]

    source_urls = [m.get("source_url") for m in mats if m.get("source_url")]
    if not source_urls:
        return None, "缺原始来源（素材无 source_url）"

    demand_evidence: list[str] = []
    for m in mats:
        for text in short_evidence_texts(m):
            if text not in demand_evidence:
                demand_evidence.append(text)
        if len(demand_evidence) >= 3:
            break
    demand_evidence = demand_evidence[:3]
    chosen_type = str(raw.get("选题类型") or raw.get("type") or "").strip()
    if chosen_type not in ("需求型", "机会型"):
        if default_type in ("需求型", "机会型"):
            chosen_type = default_type
        else:
            chosen_type = "需求型" if demand_evidence else "机会型"
    if chosen_type == "需求型" and not demand_evidence:
        return None, "需求型卡缺评论原文证据"

    v3_primary = (next((m for m in mats if short_evidence_texts(m)), mats[0])
                  if chosen_type == "需求型" else mats[0])
    v3 = score_item_v3(v3_primary, now=now, profile=profile)

    item_ids = [m["item_id"] for m in mats]
    try:
        score = max(0.0, min(10.0, float(raw.get("score") or 0)))
    except (TypeError, ValueError):
        score = 0.0
    if score == 0.0 and glm_scores:
        # 卡未给分时，用该卡引用素材的 GLM 打分兜底，避免高分素材被误判为观察。
        fallback = [glm_scores.get(iid, {}).get("score") for iid in item_ids]
        fallback = [float(v) for v in fallback if isinstance(v, (int, float))
                    and not isinstance(v, bool)]
        if fallback:
            score = max(0.0, min(10.0, max(fallback)))
    platform = raw.get("platform") if raw.get("platform") in ("抖音", "小红书", "两者") else "两者"

    # 链接一律取素材记录（输入文件 url 原样），作者取素材记录
    cited = "；".join(f"{m['author_or_publisher']} {m['source_url']}" for m in mats)
    reasons = [glm_scores.get(m["item_id"], {}).get("reason", "") for m in mats]
    reasons = [r for r in reasons if r]
    latest = max((m.get("published_at") or "" for m in mats), default="")
    eng_total = sum(_engagement(m) for m in mats)
    primary_text = " ".join(str(m.get("title_or_summary") or "") for m in mats)

    why_now = f"来源帖子发布于近 24 小时窗口（最新 {latest or '待验证'}）；引用帖合计互动（赞+转+评+引）约 {eng_total}"
    demand_text = ("评论证据（脱敏，≤3条）："
                   + "；".join(f"「{e}」" for e in demand_evidence)
                   if demand_evidence else
                   "待验证：本轮引用素材无直接评论证据；须在文案阶段补证")
    card = {
        "topic_id": f"TX-{now.strftime('%Y%m%d')}-{seq:03d}",
        "拟定标题": title,
        "建议平台": platform,
        "面向谁": f"{profile.name}（画像 {profile.profile_id}）；他们想解决：{profile.pain_points[0] if profile.pain_points else '待核实'}",
        "核心价值": hook,
        "为什么现在": why_now,
        "关联素材": item_ids,
        "_source_urls": source_urls,
        "需求证据": demand_text,
        "可信度与风险": f"{risk}；证据等级 self_reported（X 帖子为作者自述，引用前须打开原帖核验）",
        "最强反证": counter,
        "差异与难度": str(raw.get("difficulty") or "待核实"),
        "推荐级别": v3["level"],
        "推荐理由": f"{service_label} 综合评分 {score}/10（画像 {profile.profile_id}）："
                  + ("；".join(reasons) or "未给逐条理由"),
        "内容类型": "LLM选题",
        "综合评分": v3["total"],
        "为什么适合": why,
        "切入角度与钩子": hook,
        # 内部审计字段：LLM 分只作文字参考，最终级别只按 v3-dual。
        "_llm_score": score,
        "_llm_service": service_label,
        "_llm_level": "推荐" if score >= 8 else ("备选" if score >= 6 else "观察"),
        "国内可用性": china,
        "引用帖子": cited,
        "用户选择结果": "待用户选择",
        # 阶段3 新字段（只增不改旧字段；空/未知统一「待验证」）
        "类型": chosen_type,
        "选题类型": chosen_type,
        "面向用户": (
            "、".join(parse_audience_categories(filled(raw.get("target_user"))))
            if parse_audience_categories(filled(raw.get("target_user"))) else
            audience_user("", title, why, hook, primary_text,
                          filled(raw.get("target_user")))),
        "痛点或机会": filled(raw.get("pain_or_opportunity")) or why,
        "原始来源": source_urls[0],
        "为何现在做": filled(raw.get("why_now")) or why_now,
        "国内可用": normalize_china_usability(
            filled(raw.get("china")) or china),
        "是否付费": filled(raw.get("paid")) or paid_label(primary_text),
        "能否实际验证": filled(raw.get("verifiable")) or
                    ("可验证：有原始出处链接" if source_urls else "待验证"),
        "切入角度": filled(raw.get("angle")) or hook,
        "主要风险": risk,
        "评分版本": SCORING_VERSION_V3,
        "轨道得分": v3["track_score"],
        "副业证据等级": v3["side_hustle_evidence_level"],
        "传播潜力假设(不计分)": v3["propagation_hypothesis"]["text"],
        "_v3": v3,
    }
    if v3.get("overseas_new_opportunity"):
        card["国内可用"] = OVERSEAS_OPPORTUNITY_LABEL
        card["国外新机会"] = OVERSEAS_OPPORTUNITY_LABEL
        scrub_card_forbidden_steps(card)
    return card, ""


def _pending(value) -> str:
    text = str(value or "").strip()
    if not text or text in ("未知", "待核实", "待确认"):
        return "待验证"
    if text.startswith("待验证：待验证"):
        text = "待验证" + text[len("待验证：待验证"):]
    if text.startswith("待验证："):
        rest = text[len("待验证："):].strip()
        return "待验证" if not rest else f"待验证：{rest}"
    return text
