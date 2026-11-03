"""阶段4：结构化文案生成（3 标题 / 2 开头 / 1 口播稿 / 演示 / 核验 / 来源）。

约束（代码层强制，不信任 LLM 输出）：
- 只使用 03 卡片与素材中的事实；链接只能来自卡片原始来源/引用帖子/素材 source_url；
- 未验证声明写「待验证」；出现收益承诺（月入/躺赚/保证/稳赚/一定能等）→ 删除并警告；
- 国外新工具只讲能力，禁止注册/翻墙/VPN/梯子/代充/虚拟卡/外币卡/绕过地区限制等步骤；
- X 来源不写用户名/@handle，不引用 >20 字连续原文（复用 analysis.privacy）；
- 任何生成失败回退 TemplateClient，并在“生成引擎”如实标注；
- 本模块只产文本，不发布、不写 03。
"""

from __future__ import annotations

import json
import re
from dataclasses import replace

from src.analysis.privacy import (contains_verbatim, scrub_identifiers,
                                  x_identifiers, x_raw_texts)
from src.analysis.safety import (forbidden_steps_check, income_promise_check,
                                 scrub_forbidden_steps, scrub_income_promises)
from src.llm.base import Draft, DraftClient
from src.llm.glm import parse_json_loose
from src.llm.template_client import TemplateClient

MIN_SCRIPT_CHARS = 600
MAX_SCRIPT_CHARS = 1200
FALLBACK_MARK = "LLM失败回退"
ALLOWED_PLACEHOLDER = "（链接见来源）"

DRAFT_SYSTEM = (
    "你是中文 AI 自媒体文案编辑。只基于给定的选题卡与素材事实写初稿，不得编造。"
    "严格输出 JSON：{\"titles\":[\"标题1\",\"标题2\",\"标题3\"],"
    "\"openings\":[\"开头1\",\"开头2\"],\"script\":\"600-1200字完整口播稿\","
    "\"demo_suggestions\":[\"操作演示建议\"],"
    "\"fact_checklist\":[{\"claim\":\"声明\",\"status\":\"已核实或待验证\","
    "\"source\":\"原始链接\"}],\"sources\":[\"原始来源链接\"]}。\n"
    "硬性要求：\n"
    "1) 3 个标题、2 个开头、1 份 600-1200 字完整口播稿，另给操作演示建议；\n"
    "2) 不得承诺收益或效果：禁止“月入/躺赚/保证/稳赚/一定能”等表述；\n"
    "3) 未验证声明必须写“待验证”；每条核验项必须给状态与来源；\n"
    "4) 国外刚出、国内暂不可用的新工具：只讲能力、不讲开通，"
    "禁止讲注册、翻墙、VPN、梯子、代充、虚拟卡、外币卡开通、绕过地区限制等步骤；\n"
    "5) 链接只能原样使用输入的原始来源，禁止编造链接；\n"
    "6) X 来源不写作者显示名、username 或 @handle，不引用超过 20 字的连续原文；\n"
    "7) 平台为“两者”时写一份通用稿，不分别写抖音/小红书版本。"
)


def _service_label(chat) -> str:
    provider = (getattr(chat, "last_provider", None)
                or getattr(chat, "provider", None) or "llm")
    model = (getattr(chat, "last_model", None)
             or getattr(chat, "model", None) or "unknown")
    return f"chain:{provider}/{model}"


def _chat_json(chat, system: str, user: str, **kw) -> dict:
    """兼容测试/第三方客户端：优先 chat_json，其次 chat + 宽松解析。"""
    if hasattr(chat, "chat_json"):
        try:
            return chat.chat_json(system, user, **kw)
        except TypeError:
            return chat.chat_json(system, user)
    content = chat.chat(system, user, **kw)
    return parse_json_loose(content)


def build_draft_injection(card: dict, prefs: dict | None = None,
                          feedbacks: list[dict] | None = None) -> str:
    """构造并返回注入文本：03 全部字段 + 05 正在使用规则 + 该卡反馈原话。"""
    lines = ["【03 选题卡全部字段】",
             json.dumps(card, ensure_ascii=False, indent=2, default=str), ""]
    lines.append("【05 正在使用规则】")
    rules = (prefs or {}).get("rules") or []
    if rules:
        lines += [f"- {r.get('内容') or r.get('原始选题') or ''}"
                  for r in rules if (r.get("内容") or r.get("原始选题"))]
    else:
        lines.append("（无）")
    lines += ["", "【该卡用户反馈原话】"]
    feedbacks = feedbacks or []
    if feedbacks:
        for fb in feedbacks:
            lines.append(
                f"- 用户选择：{fb.get('用户选择') or '未填写'}；"
                f"原话：{fb.get('原话反馈') or '未填写'}")
    else:
        lines.append("（无）")
    return "\n".join(lines)


def _split_urls(value) -> list[str]:
    if isinstance(value, (list, tuple)):
        raw = [str(v) for v in value]
    else:
        raw = re.split(r"[；;,\s]+", str(value or ""))
    return [u for u in raw if u.startswith(("http://", "https://"))]


def collect_sources(card: dict, materials: list[dict]) -> list[str]:
    """原始来源白名单：只取卡片字段与素材记录中的真实链接。"""
    urls: list[str] = []
    for key in ("_source_urls", "原始来源", "引用帖子", "source_url"):
        urls.extend(_split_urls(card.get(key)))
    for m in materials:
        urls.extend(_split_urls(m.get("source_url")))
    out: list[str] = []
    for url in urls:
        url = url.strip().rstrip("。，；;,)）]")
        if url and url not in out:
            out.append(url)
    return out


def _strip_unknown_urls(text: str, allowed: set[str]) -> str:
    def repl(match: re.Match) -> str:
        url = match.group(0).rstrip("。，；;,)）]")
        return url if url in allowed else ALLOWED_PLACEHOLDER

    return re.sub(r"https?://[^\s，。；、）)\]]+", repl, str(text or ""))


def _redact_verbatim(text: str, raw_texts: list[str],
                     min_len: int = 21) -> str:
    out = str(text or "")
    for raw in raw_texts:
        compact = re.sub(r"\s+", "", raw or "")
        if len(compact) < min_len:
            continue
        for i in range(0, len(compact) - min_len + 1):
            window = compact[i:i + min_len]
            if window and window in out:
                out = out.replace(window, "[已脱敏]")
    return out


def _safe_text(value: str, *, x_raw: list[str], x_ids: list[str],
               allowed_urls: set[str]) -> tuple[str, list[str], list[str]]:
    text = scrub_identifiers(str(value or ""), x_ids)
    text = _redact_verbatim(text, x_raw)
    text = _strip_unknown_urls(text, allowed_urls)
    text, step_hits = scrub_forbidden_steps(text)
    text, income_hits = scrub_income_promises(text)
    return text, step_hits, income_hits


def _status_for(material: dict) -> str:
    return ("已核实" if material.get("evidence_status") in
            ("verified", "third_party") else "待验证")


def build_fact_checklist(card: dict, materials: list[dict]) -> list[str]:
    """每条声明标 已核实/待验证 + 来源；来源只取真实链接。"""
    default_source = (collect_sources(card, materials) or ["无来源"])[0]
    out: list[str] = []

    def add(status: str, claim: str, source: str):
        claim = re.sub(r"\s+", " ", str(claim or "")).strip()
        if not claim:
            return
        entry = f"{status}：{claim[:140]}｜来源：{source or default_source}"
        if entry not in out:
            out.append(entry)

    for m in materials:
        source = m.get("source_url") or default_source
        claims = [str(c) for c in (m.get("claims") or []) if str(c).strip()]
        if not claims:
            claims = [m.get("title_or_summary") or ""]
        for claim in claims[:5]:
            add(_status_for(m), claim, source)

    for field in ("核心价值", "为什么现在", "需求证据", "可信度与风险"):
        value = str(card.get(field) or "").strip()
        if not value:
            continue
        status = "待验证"
        add(status, value, default_source)

    if not out:
        add("待验证", "选题卡未提供可核验声明", default_source)
    return out


def _title_variants(card: dict, titles: list[str]) -> list[str]:
    core = (titles[0] if titles else
            card.get("拟定标题") or "AI 新工具：能做什么、适合谁")
    candidates = list(titles)
    candidates += [
        core,
        f"{core}｜3 分钟看懂",
        f"只讲能力：{core}",
        f"这条视频讲清：{core}",
    ]
    out: list[str] = []
    for item in candidates:
        value = str(item or "").strip()
        if value and value not in out:
            out.append(value)
        if len(out) >= 3:
            break
    return out[:3]


def _opening_variants(card: dict, openings: list[str]) -> list[str]:
    core = card.get("拟定标题") or "这个 AI 新工具"
    candidates = list(openings) + [
        f"如果你也在关注「{core}」，这条只回答一个问题：它能做什么、适合谁。",
        f"先别急着下结论：{card.get('核心价值') or core}，看完再判断值不值得关注。",
    ]
    out: list[str] = []
    for item in candidates:
        value = str(item or "").strip()
        if value and value not in out:
            out.append(value)
        if len(out) >= 2:
            break
    return out[:2]


def _pad_script(body: str, card: dict) -> str:
    body = str(body or "").strip()
    if len(body) >= MIN_SCRIPT_CHARS:
        return body[:MAX_SCRIPT_CHARS]
    audience = card.get("面向用户") or card.get("面向谁") or "想了解 AI 新工具的普通人"
    value = card.get("核心价值") or "讲清它能做什么、为什么值得关注"
    risk = card.get("主要风险") or card.get("可信度与风险") or "待验证"
    demand = card.get("需求证据") or "待验证"
    chunks = [
        body,
        f"这条内容只讨论能力与适用人群：{value}。",
        f"适合谁：{audience}。如果你不属于这类人，可以先收藏，不急着用。",
        f"需要冷静看待的地方：{risk}。{demand}",
        "判断标准只有三个：它是否提供新能力、是否降低使用门槛、是否和你的实际场景有关。",
        "国外刚出、国内暂时用不上的工具，也只讲能力和机会，不讨论任何账号、支付或网络访问步骤。",
        "所有事实以原始来源链接为准；没有交叉核验的内容，一律按“待验证”处理。",
        "如果你觉得这条有用，先收藏，再打开来源自己核验一遍，别只听转述。",
    ]
    text = "\n\n".join(c for c in chunks if c)
    if len(text) < MIN_SCRIPT_CHARS:
        filler = "以上是能力层面的判断，具体效果仍需你结合来源和自身场景核验。"
        while len(text) < MIN_SCRIPT_CHARS:
            text += "\n\n" + filler
    return text[:MAX_SCRIPT_CHARS]


def _draft_from_data(data: dict, card: dict, platform: str, chat) -> Draft:
    if not isinstance(data, dict):
        raise ValueError("LLM 返回非 JSON 对象")
    titles = data.get("titles") or data.get("标题候选") or data.get("title")
    if isinstance(titles, str):
        titles = [titles]
    if not isinstance(titles, list):
        titles = []
    titles = [str(t) for t in titles if str(t).strip()]
    openings = data.get("openings") or data.get("开头候选") or []
    if isinstance(openings, str):
        openings = [openings]
    if not isinstance(openings, list):
        openings = []
    script = (data.get("script") or data.get("body")
              or data.get("口播稿") or data.get("正文") or "")
    if not str(script).strip():
        raise ValueError("LLM 未返回口播稿")
    demo = data.get("demo_suggestions") or data.get("演示建议") or []
    if isinstance(demo, str):
        demo = [demo]
    if not isinstance(demo, list):
        demo = []
    checklist: list[str] = []
    for item in data.get("fact_checklist") or []:
        if isinstance(item, dict):
            claim = item.get("claim") or item.get("声明") or ""
            status = item.get("status") or item.get("状态") or "待验证"
            source = item.get("source") or item.get("来源") or ""
            if claim:
                checklist.append(f"{status}：{claim}｜来源：{source or '待验证'}")
        elif str(item).strip():
            checklist.append(str(item))
    sources = [str(s) for s in (data.get("sources") or []) if str(s).strip()]
    return Draft(
        topic_id=card.get("topic_id") or "",
        platform=platform,
        title=titles[0] if titles else (card.get("拟定标题") or "待审核文案"),
        body=str(script),
        fact_checklist=checklist,
        sources=sources,
        engine=_service_label(chat),
        titles=titles,
        openings=[str(o) for o in openings if str(o).strip()],
        demo_suggestions=[str(d) for d in demo if str(d).strip()],
    )


def finalize_draft(draft: Draft, card: dict, materials: list[dict], *,
                   platform: str | None = None) -> Draft:
    """统一清洗/补齐结构：3 标题、2 开头、600–1200 字稿、演示、核验、来源。"""
    allowed = set(collect_sources(card, materials))
    x_mats = [m for m in materials if str(m.get("source_type") or "").lower()
              in ("x", "x_posts") or str(m.get("platform") or "").lower() == "x"]
    x_raw = x_raw_texts(x_mats)
    x_ids = x_identifiers(x_mats)

    step_hits: list[str] = []
    income_hits: list[str] = []

    def safe(value) -> str:
        text, steps, incomes = _safe_text(
            str(value or ""), x_raw=x_raw, x_ids=x_ids, allowed_urls=allowed)
        step_hits.extend(h for h in steps if h not in step_hits)
        income_hits.extend(h for h in incomes if h not in income_hits)
        return text

    raw_titles = [safe(t) for t in (draft.titles or [draft.title])]
    titles = _title_variants(card, [t for t in raw_titles if t])
    titles = [safe(t) for t in titles if safe(t)]
    if not titles:
        titles = [safe(card.get("拟定标题") or "AI 新工具：只讲能力")]
    base_title = titles[0] or "AI 新工具：只讲能力"
    while len(titles) < 3:
        titles.append(safe(f"{base_title}｜看点{len(titles) + 1}"))
    titles = [t for t in titles if t][:3]
    raw_openings = [safe(o) for o in (draft.openings or [])]
    openings = _opening_variants(card, [o for o in raw_openings if o])
    openings = [safe(o) for o in openings if safe(o)]
    while len(openings) < 2:
        openings.append(safe("这条只回答一个问题：它能做什么、适合谁，"
                             "以及为什么值得关注。"))
    openings = [o for o in openings if o][:2]
    body = safe(draft.body)
    safe_card = {k: safe(v) for k, v in card.items()
                 if isinstance(v, str)}
    body = _pad_script(body, safe_card)
    # 补齐语料也可能来自卡片字段，再清洗一次，防止敏感步骤随补白进入正文。
    body = safe(body)
    if len(body) < MIN_SCRIPT_CHARS:
        body = _pad_script(body, safe_card)
    demos = [safe(d) for d in (draft.demo_suggestions or []) if safe(d)]
    if not demos:
        demos = ["演示建议：只展示公开能力与适用人群，不展示任何账号、支付或网络访问步骤。"]

    checklist = [safe(c) for c in
                 (draft.fact_checklist or build_fact_checklist(card, materials))]
    checklist = [c for c in checklist if c]
    if not checklist:
        checklist = [safe(c) for c in build_fact_checklist(card, materials)
                     if safe(c)]
    # 每条核验项必须显式带状态与来源；缺少时补「待验证」/卡片原始来源。
    default_source = (collect_sources(card, materials) or ["待验证"])[0]
    normalized_checklist: list[str] = []
    for item in checklist:
        entry = (item if (item.startswith("已核实") or item.startswith("待验证"))
                 else f"待验证：{item}")
        if "来源" not in entry:
            entry = f"{entry}｜来源：{default_source}"
        normalized_checklist.append(entry)
    checklist = normalized_checklist
    if not checklist:
        checklist = [f"待验证：选题卡未提供可核验声明｜来源：{default_source}"]

    # 原始来源以卡片/素材记录为唯一事实源，LLM 提供的链接不参与。
    sources = collect_sources(card, materials)
    if not sources:
        sources = [s for s in (draft.sources or []) if s in allowed]

    warnings = [safe(w) for w in (draft.unverified_warnings or [])]
    warnings = [w for w in warnings if w]
    if step_hits:
        warnings.append("已移除敏感步骤；只讲能力，不讲账号、支付或网络访问流程。")
    if income_hits:
        warnings.append("已移除收益承诺表述；本稿不承诺任何收益或效果。")
    if not sources:
        warnings.append("本选题缺少可打开的原始出处链接，发布前必须补证。")

    return replace(
        draft,
        platform=platform or draft.platform,
        title=titles[0],
        body=body,
        titles=titles[:3],
        openings=openings[:2],
        demo_suggestions=demos,
        fact_checklist=checklist,
        sources=sources,
        unverified_warnings=[w for w in warnings if w],
    )


class DraftWriter(DraftClient):
    """LLM 写稿器：成功走 provider chain，失败回退离线模板并如实标注。"""

    def __init__(self, chat=None, fallback: DraftClient | None = None,
                 injection_text: str = ""):
        self.chat = chat
        self.fallback = fallback or TemplateClient()
        self.injection_text = injection_text

    def generate(self, platform: str, card: dict, materials: list[dict]) -> Draft:
        if self.chat is None:
            return self._fallback(platform, card, materials)

        user = "\n\n".join([
            f"平台：{platform}",
            "【选题卡】",
            json.dumps(card, ensure_ascii=False, default=str),
            "【素材事实（只可使用这些事实和链接）】",
            json.dumps([{
                "item_id": m.get("item_id"),
                "title_or_summary": m.get("title_or_summary"),
                "claims": m.get("claims") or [],
                "source_url": m.get("source_url"),
                "evidence_status": m.get("evidence_status"),
            } for m in materials], ensure_ascii=False, default=str),
            self.injection_text,
        ])
        try:
            data = _chat_json(self.chat, DRAFT_SYSTEM, user,
                              temperature=0.4, max_tokens=12000)
            draft = _draft_from_data(data, card, platform, self.chat)
        except Exception:  # noqa: BLE001 - 任何 LLM 失败都回退模板，绝不编造
            draft = self._fallback(platform, card, materials)
            draft.engine = f"{draft.engine}（{FALLBACK_MARK}）"
        return finalize_draft(draft, card, materials, platform=platform)

    def _fallback(self, platform: str, card: dict,
                  materials: list[dict]) -> Draft:
        # 模板客户端只认抖音/小红书；“两者”先按抖音出通用稿，再标注平台=两者。
        target = "抖音" if platform == "两者" else platform
        draft = self.fallback.generate(target, card, materials)
        draft.platform = platform
        draft.engine = draft.engine or "template@v1"
        return draft


def render_markdown(draft: Draft) -> str:
    """导出 markdown（文件名为 <topic_id>.md）。"""
    lines = [f"# {draft.title}", "",
             f"- topic_id：{draft.topic_id}",
             f"- 平台：{draft.platform}",
             f"- 生成引擎：{draft.engine}",
             f"- 版本：v{draft.version}", "",
             "## 标题候选（3 个）"]
    lines += [f"{i}. {t}" for i, t in enumerate(draft.titles, 1)]
    lines += ["", "## 开头候选（2 个）"]
    lines += [f"{i}. {o}" for i, o in enumerate(draft.openings, 1)]
    lines += ["", "## 完整口播稿", "", draft.body, "",
              "## 操作演示建议"]
    lines += [f"- {d}" for d in draft.demo_suggestions] or ["- （无）"]
    lines += ["", "## 事实核验清单"]
    lines += [f"- {c}" for c in draft.fact_checklist] or ["- （无）"]
    lines += ["", "## 原始来源链接"]
    lines += [f"- {s}" for s in draft.sources] or ["- （无）"]
    lines += ["", "## 未证实内容提醒"]
    lines += [f"- {w}" for w in draft.unverified_warnings] or ["- （无）"]
    lines += ["", "⏸ GATE-2：等待用户审核，未发布。系统无发布能力，绝不自动发布。"]
    return "\n".join(lines)
