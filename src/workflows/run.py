"""任务03 工作流 CLI（手动触发，无任何定时调度）。

子命令：
  collect   S1抓取+S2分析+S3写选题卡 → 停在 GATE-1（默认离线回放假数据文件）
  gate1     S4 读取用户在 Notion 的选题选择结果（只读）；--draft 时直接续写初稿
  draft     S4+S5：读取 GATE-1 → 已选定则生成初稿写 Notion 04 → 停在 GATE-2
  gate2     S6 读取 04 库各稿状态（只读观测；无发布动作）
  status    打印本地状态；--live 附带 Notion 关口实况
  selfcheck 连通性自检：aihot 1 条真实请求 + Notion 权限/结构检查 + 1 条审计
  feedback  记录 GATE-1 反馈到 05（不改 03）
  feedback-seed  从本地 JSON 批量写反馈
  rules     候选规则：--propose 生成待确认规则；--confirm 只提示手动改状态
  topics-round   选题轮：采集→需求分类→偏好注入→出卡→写 03/02→停 GATE-1

通用：--dry-run 全程离线不写 Notion 不落状态；NOTION_TOKEN 只从环境变量读。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.analysis.audience import load_profile  # noqa: E402
from src.analysis.fields import (audience_user, filled,  # noqa: E402
                                 is_simulated_source,
                                 normalize_china_usability, paid_label,
                                 parse_audience_categories)
from src.analysis.safety import scrub_card_forbidden_steps  # noqa: E402
from src.analysis.scoring import (OVERSEAS_OPPORTUNITY_LABEL,  # noqa: E402
                                  SCORING_VERSION, SCORING_VERSION_V3,
                                  merge_dual_tracks, score_item_v3)
from src.connectors.github_source import (DEFAULT_SNAPSHOTS, fetch_github,  # noqa: E402
                                          load_config as load_github_config)
from src.cost_log import UNKNOWN, append_cost  # noqa: E402
from src.llm.base import DraftClient  # noqa: E402
from src.llm.template_client import TemplateClient  # noqa: E402
from src.normalize import iso_now  # noqa: E402
from src.notion_store.audience_page import ensure_audience_page  # noqa: E402
from src.notion_store.bootstrap import ensure_kb, ensure_kb_upgrades  # noqa: E402
from src.notion_store.client import (NotionClient, date_prop, rt,  # noqa: E402
                                     select_prop)
from src.workflows import pipeline  # noqa: E402
from src.workflows.state import WorkflowState  # noqa: E402

DEFAULT_PAGE_ID = "408d7d3b292381068024e0548308c638"
DEFAULT_STATE = "data/state/workflow_state.json"
NO_LLM_COST_BASIS = "不适用(未调用LLM)；外部API为免费接口，实际金额未知"
HEURISTIC_FALLBACK_ENGINE = "启发式回退"
OPPORTUNITY_SOURCES = ("aihot", "github", "official")


def make_llm(name: str) -> DraftClient:
    if name == "template":
        return TemplateClient()
    if name == "openai-compat":
        from src.llm.openai_compat import OpenAICompatClient
        return OpenAICompatClient()  # 未配置环境变量时抛 LLMNotConfigured
    raise ValueError(f"未知 LLM 引擎：{name}")


def _is_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _serving_label(chat, analyze_mode: str) -> tuple[str, str]:
    """返回实际服务的 (provider, model)；不打印/记录任何密钥。"""
    if chat is None:
        return "heuristic", SCORING_VERSION
    if analyze_mode == "llm":
        return (getattr(chat, "last_provider", "未调用"),
                getattr(chat, "last_model", "未调用"))
    return "glm", getattr(chat, "model", "未知")


def _aggregate_usage(chat) -> dict:
    """汇总 usage_log；任何无法确认的 token/金额都写“未知”，绝不以 0 代替。"""
    usage = getattr(chat, "usage_log", []) or []
    calls = getattr(chat, "calls", 0)
    if not usage:
        return {"calls": calls, "prompt_tokens": UNKNOWN,
                "completion_tokens": UNKNOWN, "tokens": UNKNOWN, "cost": UNKNOWN,
                "cost_basis": UNKNOWN, "list_cost": UNKNOWN,
                "list_cost_basis": UNKNOWN}

    prompts = [u.get("prompt_tokens") for u in usage]
    completions = [u.get("completion_tokens") for u in usage]
    costs = [u.get("cost") for u in usage]
    bases = [u.get("cost_basis") or UNKNOWN for u in usage]
    unique_bases = list(dict.fromkeys(bases))
    cost_basis = (unique_bases[0] if len(unique_bases) == 1
                  else "混合（" + "、".join(unique_bases) + "）")
    if all(_is_number(v) for v in prompts) and all(_is_number(v) for v in completions):
        prompt_tokens = sum(prompts)
        completion_tokens = sum(completions)
        tokens = prompt_tokens + completion_tokens
    else:
        prompt_tokens = completion_tokens = tokens = UNKNOWN
    cost = sum(costs) if all(_is_number(v) for v in costs) else UNKNOWN
    list_costs = [u.get("cost") for u in usage
                  if u.get("provider") == "opencode-go"
                  and _is_number(u.get("cost"))]
    list_cost = sum(list_costs) if list_costs else UNKNOWN
    return {"calls": calls, "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens, "tokens": tokens, "cost": cost,
            "cost_basis": cost_basis, "list_cost": list_cost,
            "list_cost_basis": ("opencode-go套餐额度折算(美元,非实付)"
                                if list_costs else UNKNOWN)}


def _log_collect_cost(args, out: dict, chat, analyze_mode: str) -> dict:
    path = Path(args.state).parent / "cost_log.jsonl"
    if chat is None:
        # 未使用 LLM：金额不适用/未知，绝不以 0 冒充“已确认零成本”。
        stats = {"calls": 0, "prompt_tokens": UNKNOWN,
                 "completion_tokens": UNKNOWN, "tokens": UNKNOWN,
                 "cost": UNKNOWN, "cost_basis": NO_LLM_COST_BASIS,
                 "list_cost": UNKNOWN, "list_cost_basis": UNKNOWN}
        provider, model = "无", "无"
        note = "未调用 LLM；免费接口的实际金额未知"
    else:
        stats = _aggregate_usage(chat)
        provider, model = _serving_label(chat, analyze_mode)
        note = ""
    if stats.get("list_cost") != UNKNOWN:
        note = (note + "；" if note else "") + (
            "OpenCode list_cost 为套餐额度折算，非实付")
    external = out.get("external_calls")
    if not isinstance(external, dict):
        external = {"github": 0, "aihot": 1 if getattr(args, "live", False) else 0,
                    "official": 0, "x": 0, "import": 0}
    record = append_cost(path, {
        "command": f"collect --sources {getattr(args, 'sources', 'aihot,x')} "
                   f"--analyze {analyze_mode}",
        "provider": provider,
        "model": model,
        **stats,
        "external_api_calls": external,
        "note": note,
    })
    print(f"[成本] 实际服务 provider={record['provider']} model={record['model']}；"
          f"调用 {record['calls']} 次，tokens={record['tokens']}，"
          f"金额={record['金额']}；外部请求={external}")
    return record


def audit_event(client, db_ids, event_id, obj, op, ok, detail):
    client.upsert_row(db_ids["08_维护与审计"], "event_id", event_id, {
        "对象": rt(obj), "操作": rt(op),
        "结果": select_prop("success" if ok else "failed"),
        "详情": rt(detail), "occurred_at": date_prop(iso_now()),
    })


def _load_materials_from_state(state) -> list[dict]:
    snap = (state.get("last_collect") or {}).get("snapshot")
    if not snap or not Path(snap).exists():
        raise SystemExit("状态中无素材快照：请先运行 collect")
    return json.loads(Path(snap).read_text(encoding="utf-8"))


# ---------------------------------------------------------------------- collect
def cmd_collect(args) -> int:
    from src.analysis import llm_topics

    state = WorkflowState(args.state)
    if not args.dry_run:
        state.load()
    profile = None
    if args.audience:
        profile = load_profile(args.audience)  # 缺文件/缺字段直接报错，不静默拿错受众
    dry = _DryState(args.state)
    out = pipeline.step_collect(
        state if not args.dry_run else dry,
        aihot_file=args.aihot_file, x_following_file=args.x_following,
        x_posts_file=args.x_posts,
        live=args.live, snapshot_dir=None if args.dry_run else "data/raw",
        sources=args.sources, github_top=args.github_top,
        official_limit=args.official_limit, import_file=args.import_file,
        config_path=args.config)

    analyze_meta = {"engine": f"heuristic:{SCORING_VERSION}"}
    analyze_mode = getattr(args, "analyze", "heuristic")
    chat = None
    use_llm = (analyze_mode in ("glm", "llm") and not args.dry_run)
    if use_llm:
        if analyze_mode == "glm":
            from src.llm.glm import GLMChat
            chat = GLMChat()  # 需 GLM_API_BASE/GLM_API_KEY 环境变量；缺则报错退出
        else:
            from src.llm.provider_chain import ProviderChain
            chat = ProviderChain()  # OpenCode Go → GLM → DeepSeek；缺失的提供方自动跳过
        analysis_materials = pipeline.eligible_materials(out["materials"])
        short = llm_topics.shortlist(analysis_materials, k=args.shortlist,
                                     profile=profile)
        glm_scores = llm_topics.glm_score_posts(chat, short, profile)
        cards, meta = llm_topics.glm_build_cards(chat, short, profile,
                                                 glm_scores=glm_scores)
        provider, model = _serving_label(chat, analyze_mode)
        analyze_meta = {"engine": meta["engine"], "glm_calls": meta["glm_calls"],
                        "shortlist": len(short), "scored": len(glm_scores),
                        "dropped_cards": meta["dropped"],
                        "provider": provider, "model": model}
        if analyze_mode == "llm":
            analyze_meta["engine"] = f"llm:{provider}/{model}"
        details = []
        used = {iid for c in cards for iid in c["关联素材"]}
        for m in analysis_materials:
            if m["item_id"] not in used:
                continue
            gs = glm_scores.get(m["item_id"])
            if gs:
                m["screen_score"] = round(gs["score"])
                m["screen_reasons"] = [f"GLM受众评分 {gs['score']}/10：{gs['reason']}"]
            m["processing_status"] = "candidate"
        ana = {"cards": cards, "details": details}
    else:
        if analyze_mode in ("glm", "llm"):
            print(f"[dry-run] {analyze_mode} 分析不真实调用（省 token），回退启发式展示")
        ana = pipeline.step_analyze(state if not args.dry_run else dry,
                                    out["materials"], top_k=args.top_k,
                                    profile=profile)

    print(f"[S1] 素材 {len(out['materials'])} 条（去重丢弃 {len(out['dropped'])}，"
          f"{'真实拉取' if args.live else '离线回放'}；X 帖子文件 {out['x_posts']} 条；"
          f"X 账号 {len(out['accounts'])} 个（仅文件）")
    print("[S1] 来源计数：" + "，".join(
        f"{name}={count}" for name, count in out["per_source"].items()))
    if out.get("validation_error_counts"):
        print(f"[S1] 校验错误计数：{out['validation_error_counts']}")
    for name, errs in (out.get("source_errors") or {}).items():
        print(f"[S1] ⚠️ {name} 错误 {len(errs)} 条：{str(errs[0])[:180]}")
    print(f"[S2] 分析引擎：{analyze_meta['engine']}；选题卡 {len(ana['cards'])} 张"
          + (f"（{('LLM' if analyze_mode == 'llm' else 'GLM')} 调用 "
             f"{analyze_meta['glm_calls']} 次，候选 {analyze_meta['shortlist']} 条）"
             if 'glm_calls' in analyze_meta else
             f"（评分版本 {SCORING_VERSION}，推荐 "
             f"{sum(1 for c in ana['cards'] if c['推荐级别'] == '推荐')} / "
             f"备选 {sum(1 for c in ana['cards'] if c['推荐级别'] == '备选')} / "
             f"观察 {sum(1 for c in ana['cards'] if c['推荐级别'] == '观察')}）"))
    if use_llm:
        provider, model = _serving_label(chat, analyze_mode)
        print(f"[LLM] 本次实际服务 provider/model：{provider} / {model}")
    if len(ana["cards"]) < 5:
        print(f"⚠️ 选题卡不足 5 张（{len(ana['cards'])}），仍按规则写入并标记 insufficient")
    if (not args.dry_run) or args.live:
        _log_collect_cost(args, out, chat, analyze_mode)

    if args.dry_run:
        print("[dry-run] 未写 Notion、未落状态。前 3 张卡：")
        for c in ana["cards"][:3]:
            print(f"  - {c['topic_id']} [{c['推荐级别']}] {c['拟定标题']}")
        return 0

    client = NotionClient()
    db_ids = ensure_kb(client, args.page_id)
    added = ensure_kb_upgrades(client, db_ids)
    if any(added.values()):
        print(f"[KB] 只增列/只增选项升级：{ {k: v for k, v in added.items() if v} }")
    state.update(db_ids=db_ids, page_root=args.page_id,
                 audience_profile=profile.profile_id if profile else None)
    w = pipeline.step_write_topics(client, db_ids, ana["cards"], out["materials"],
                                   only_referenced=True)
    print(f"[S3] Notion 写入：素材 {w['materials_written']} 条（仅被选题卡引用，池总量 "
          f"{len(out['materials'])}）、选题卡 {w['topics_written']} 张"
          f"（用户选择结果=待用户选择）")

    cards_path = Path(args.state).parent / "cards.json"
    cards_path.parent.mkdir(parents=True, exist_ok=True)
    cards_path.write_text(json.dumps(ana["cards"], ensure_ascii=False, indent=2),
                          encoding="utf-8")
    state.update(cards_path=str(cards_path),
                 gate1={"status": "waiting", "selected": []},
                 last_analyze=analyze_meta)
    state.save()
    audit_event(client, db_ids, f"task05-collect-{iso_now()}", "工作流",
                "collect+analyze+write_topics", True,
                f"引擎{analyze_meta['engine']} 素材{w['materials_written']} "
                f"选题{w['topics_written']} 已停GATE-1")
    print("⏸ GATE-1（第一人工关口）：请到 Notion「03_选题决策」把 1–3 张卡"
          "「用户选择结果」改为 已选定（可另选「用户指定平台」），然后运行："
          "python3 -m src.workflows.run draft")
    return 0


# ------------------------------------------------------------------ audience
def cmd_write_audience(args) -> int:
    """任务05：把受众画像写入 Notion「用户画像」子页面（幂等，已存在不改动）。"""
    profile = load_profile(args.audience)
    client = NotionClient()
    db_ids = ensure_kb(client, args.page_id)
    page, created = ensure_audience_page(client, args.page_id, profile)
    url = f"https://www.notion.so/{page.replace('-', '')}"
    audit_event(client, db_ids, f"task05-audience-{iso_now()}", "用户画像",
                "write_audience_page", True,
                f"{profile.profile_id} {'新建' if created else '已存在未改动'}")
    print(f"{'✅ 已新建' if created else 'ℹ️ 已存在（未改动）'}：用户画像（{profile.profile_id}） → {url}")
    return 0


# ---------------------------------------------------------------------- gate1/draft
def cmd_gate1(args, auto_draft: bool) -> int:
    state = WorkflowState(args.state).load()
    client = NotionClient()
    db_ids = state.get("db_ids") or ensure_kb(client, args.page_id)
    selections = pipeline.step_gate1(client, db_ids)
    if not selections:
        print("⏸ GATE-1：尚未检出任何「已选定」选题（Notion 03_选题决策）。等待用户操作。")
        return 0
    print(f"✅ GATE-1 通过：{len(selections)} 张已选定 →")
    for s in selections:
        print(f"  - {s['topic_id']} {s['拟定标题']} 平台={'+'.join(s['platforms'])}")
    state.update(gate1={"status": "passed", "selected": selections})
    state.save()

    if not auto_draft:
        print("如需生成初稿，运行：python3 -m src.workflows.run draft")
        return 0
    return _do_draft(args, state, client, db_ids, selections)


def _do_draft(args, state, client, db_ids, selections) -> int:
    try:
        materials = _load_materials_from_state(state)
    except SystemExit:
        print("⚠️ 本地无素材快照（如直接沿用已在库选题卡），初稿的事实清单将标注缺失")
        materials = []
    llm = make_llm(args.llm)
    out = pipeline.step_draft(client, db_ids, selections, materials, llm)
    state.update(last_draft=out)
    state.set_stage("drafted")
    state.set_stage("waiting_gate2")
    state.update(gate2={"status": "waiting", "drafts": out["drafts"]})
    state.save()
    audit_event(client, db_ids, f"task03-draft-{iso_now()}", "工作流",
                "draft(初稿V1)", True,
                f"生成 {len(out['drafts'])} 份初稿（引擎={args.llm}），已停GATE-2")
    for d in out["drafts"]:
        print(f"  ✍️ {d['draft_id']} → {d['notion_page']}")
    print("⏸ GATE-2（第二人工关口）：请在 Notion「04_文案与版本」审阅初稿："
          "状态改 已确认 或 修订中（附反馈）。系统无发布能力，绝不自动发布。")
    return 0


def _log_draft_cost(args, chat, drafts: list[dict]) -> dict:
    """draft 成本日志：未知=未知；OpenCode list_cost 标注套餐额度折算非实付。"""
    path = Path(args.state).parent / "cost_log.jsonl"
    if chat is None:
        stats = {"calls": 0, "prompt_tokens": UNKNOWN,
                 "completion_tokens": UNKNOWN, "tokens": UNKNOWN,
                 "cost": UNKNOWN, "cost_basis": NO_LLM_COST_BASIS,
                 "list_cost": UNKNOWN, "list_cost_basis": UNKNOWN}
        provider, model = "无", "无"
    else:
        stats = _aggregate_usage(chat)
        provider = getattr(chat, "last_provider", None) or getattr(
            chat, "model", UNKNOWN)
        model = getattr(chat, "last_model", None) or getattr(
            chat, "model", UNKNOWN)
    note = ("文案生成；list_cost 为套餐额度折算（非实付），未知写「未知」；"
            "未调用发布接口")
    record = append_cost(path, {
        "command": ("draft --topic " +
                    ",".join(getattr(args, "topic", []) or []) +
                    f" --llm {getattr(args, 'llm', 'chain')}"),
        "provider": provider, "model": model,
        **stats,
        "external_api_calls": {},
        "note": note,
    })
    print(f"[成本] provider={record['provider']} model={record['model']}；"
          f"调用 {record['calls']} 次，tokens={record['tokens']}，"
          f"金额={record['金额']}（套餐额度折算非实付；未知=未知）")
    return record


def _expand_topic_args(raw) -> list[str]:
    if not raw:
        return []
    if isinstance(raw, str):
        raw = [raw]
    out: list[str] = []
    for value in raw:
        for topic in re.split(r"[,，\s]+", str(value or "")):
            if topic and topic not in out:
                out.append(topic)
    return out


def _single_draft_platform(card: dict) -> str:
    """RULE-001：一卡一稿；“两者”只写一份通用稿并标注平台=两者。"""
    value = (filled(card.get("用户指定平台")) or filled(card.get("建议平台"))
             or filled(card.get("platform")) or "两者")
    return value if value in ("抖音", "小红书", "两者") else "两者"


def cmd_draft(args, client=None, chat=None) -> int:
    """阶段4：按 --topic 读取 03 全字段 + 05 规则/反馈 → 写 04 → 停 GATE-2。"""
    from src.analysis import preference
    from src.llm.draft_writer import (FALLBACK_MARK, DraftWriter,
                                      build_draft_injection, finalize_draft,
                                      render_markdown)
    from src.notion_store.gates import (read_topic_cards_by_ids,
                                        write_draft_row_pending)

    state = WorkflowState(args.state).load()
    client = client or NotionClient()
    db_ids = state.get("db_ids") or ensure_kb(client, args.page_id)
    ensure_kb_upgrades(client, db_ids)

    topic_ids = _expand_topic_args(getattr(args, "topic", None))
    if topic_ids:
        try:
            cards = read_topic_cards_by_ids(client, db_ids, topic_ids)
        except ValueError as exc:
            print(f"❌ {exc}")
            return 1
        missing = [tid for tid in topic_ids if tid not in
                   {c.get("topic_id") for c in cards}]
        if missing:
            print(f"❌ 03 中找不到 topic_id：{missing}")
        if not cards:
            print("⏸ GATE-1：未找到可写选题卡，未生成任何文案。")
            return 1
    else:
        cards = pipeline.step_gate1(client, db_ids)
        if not cards:
            print("⏸ GATE-1：尚未检出任何「已选定」选题（Notion 03_选题决策）。等待用户操作。")
            return 0

    try:
        materials = _load_materials_from_state(state)
    except SystemExit:
        print("⚠️ 本地无素材快照（如直接沿用已在库选题卡），"
              "初稿事实清单将只使用卡片原始来源。")
        materials = []

    prefs = {"approved": [], "rejected": [], "neutral": [],
             "unpicked": [], "rules": [], "candidate_rules": []}
    if db_ids.get("05_经验与复盘"):
        prefs = preference.load_recent_preferences(client, db_ids)
    mat_by_id = {m["item_id"]: m for m in materials}
    llm_name = getattr(args, "llm", "chain")
    if llm_name == "template":
        # 显式 template 时不使用任何 LLM（即使调用方误传了 chat）。
        chat = None
    if llm_name == "chain" and chat is None:
        from src.llm.provider_chain import ProviderChain
        try:
            chat = ProviderChain(exclude_paid=bool(getattr(args, "no_paid", True)))
        except Exception as exc:  # noqa: BLE001 - 无配置时回退模板
            print(f"⚠️ chain 初始化失败，回退模板：{type(exc).__name__}: {exc}")
            chat = None
    openai_client = None
    if llm_name == "openai-compat" and chat is None:
        from src.llm.openai_compat import OpenAICompatClient
        try:
            openai_client = OpenAICompatClient()
        except Exception as exc:  # noqa: BLE001 - 配置缺失时回退离线模板
            print(f"⚠️ openai-compat 初始化失败，回退模板："
                  f"{type(exc).__name__}: {exc}")

    export_dir = getattr(args, "export_dir", None)
    if export_dir:
        Path(export_dir).mkdir(parents=True, exist_ok=True)

    results: list[dict] = []
    for card in cards:
        topic_id = card.get("topic_id")
        rel_mats = [mat_by_id[iid] for iid in card.get("关联素材") or []
                    if iid in mat_by_id]
        if card.get("关联素材") and len(rel_mats) < len(card["关联素材"]):
            missing_ids = set(card["关联素材"]) - set(mat_by_id)
            print(f"⚠️ {topic_id} 有关联素材不在本地快照：{missing_ids}"
                  "（事实清单仅使用卡片来源）")
        feedbacks = (preference.load_feedback_for_topic(client, db_ids, topic_id)
                     if db_ids.get("05_经验与复盘") else [])
        injection = build_draft_injection(card, prefs, feedbacks)
        inject_path = (Path(args.state).parent /
                       f"draft_injection_{topic_id}.txt")
        inject_path.parent.mkdir(parents=True, exist_ok=True)
        inject_path.write_text(injection, encoding="utf-8")

        platform = _single_draft_platform(card)
        if openai_client is not None:
            target = "抖音" if platform == "两者" else platform
            try:
                raw = openai_client.generate(target, card, rel_mats,
                                             injection_text=injection)
                raw.platform = platform
                draft = finalize_draft(raw, card, rel_mats, platform=platform)
            except Exception as exc:  # noqa: BLE001 - 任何失败都回退模板
                print(f"⚠️ openai-compat 生成失败，回退模板："
                      f"{type(exc).__name__}: {exc}")
                draft = DraftWriter(chat=None, fallback=TemplateClient(),
                                    injection_text=injection).generate(
                                        platform, card, rel_mats)
                draft.engine = f"{draft.engine}（{FALLBACK_MARK}）"
        else:
            writer = DraftWriter(chat=chat, fallback=TemplateClient(),
                                 injection_text=injection)
            draft = writer.generate(platform, card, rel_mats)
        draft.draft_id = f"D-{topic_id}-{platform}-v{draft.version}"
        page_id, created = write_draft_row_pending(
            client, db_ids, draft, topic_id)
        results.append({"draft_id": draft.draft_id, "topic_id": topic_id,
                        "platform": platform, "notion_page": page_id,
                        "created": created, "engine": draft.engine})
        if export_dir:
            path = Path(export_dir) / f"{topic_id}.md"
            path.write_text(render_markdown(draft), encoding="utf-8")
            print(f"  📄 导出：{path}")
        print(f"  ✍️ {draft.draft_id} [{platform}] 生成引擎={draft.engine}"
              f" → {page_id}")

    state.update(last_draft={"drafts": results},
                 gate2={"status": "waiting", "drafts": results})
    try:
        state.set_stage("drafted")
        state.set_stage("waiting_gate2")
    except ValueError:
        state.update(stage="waiting_gate2")
    state.save()

    _log_draft_cost(args, openai_client if openai_client is not None else chat,
                    results)
    print("⏸ GATE-2：等待用户审核，未发布。")
    print("ℹ️ 系统无发布能力；请在 Notion「04_文案与版本」人工审阅，"
          "状态改 已确认 或 修订中（附反馈）。")
    return 0


# ---------------------------------------------------------------------- gate2/status
def cmd_gate2(args) -> int:
    state = WorkflowState(args.state).load()
    client = NotionClient()
    db_ids = state.get("db_ids") or ensure_kb(client, args.page_id)
    out = pipeline.step_gate2(client, db_ids)
    print(f"[GATE-2 观测] 待审 {out['pending']} / 修订中 {out['revising']} / 已确认 {out['confirmed']}")
    for d in out["drafts"]:
        print(f"  - {d['draft_id']} [{d['platform']}] {d['status']}")
    print(f"ℹ️ {out['note']}")
    return 0


def cmd_status(args) -> int:
    state = WorkflowState(args.state).load()
    print(json.dumps(state.data, ensure_ascii=False, indent=2))
    if args.live:
        client = NotionClient()
        db_ids = state.get("db_ids")
        if db_ids:
            sel = pipeline.step_gate1(client, db_ids)
            print(f"[live] GATE-1 已选定 {len(sel)} 张")
            g2 = pipeline.step_gate2(client, db_ids)
            print(f"[live] GATE-2 待审 {g2['pending']} / 修订中 {g2['revising']} / 已确认 {g2['confirmed']}")
    return 0


# ---------------------------------------------------------------------- selfcheck
def cmd_selfcheck(args) -> int:
    """连通性自检：aihot 1 条真实请求 + Notion 权限与结构（幂等）+ 1 条审计。"""
    results = []
    if not args.skip_aihot:
        try:
            items = pipeline.fetch_selected(limit=1)
            results.append(("aihot /api/v1 真实请求(1条)", True,
                            f"返回 {len(items)} 条，示例: {(items[0]['title_or_summary'][:40] + '…') if items else '空'}"))
        except Exception as e:  # noqa: BLE001
            results.append(("aihot /api/v1 真实请求(1条)", False, str(e)[:200]))
    try:
        client = NotionClient()
        db_ids = ensure_kb(client, args.page_id)
        added = ensure_kb_upgrades(client, db_ids)
        audit_event(client, db_ids, f"task03-selfcheck-{iso_now()}", "工作流",
                    "selfcheck", True,
                    f"7库就绪；只增列升级 {added or '无新增'}")
        results.append(("Notion 权限+7库+只增列升级+审计", True,
                        f"db_ids {len(db_ids)} 个；新增列 {added or '无'}"))
    except Exception as e:  # noqa: BLE001
        results.append(("Notion 权限+7库+只增列升级+审计", False, str(e)[:200]))

    ok = all(r[1] for r in results)
    for name, passed, detail in results:
        print(f"{'✅' if passed else '❌'} {name}：{detail}")
    return 0 if ok else 1


# ---------------------------------------------------------------------- github
def cmd_github(args) -> int:
    """只做 GitHub 研究：真实官方 REST → data/state JSON + 终端表格，不写 Notion。"""
    state_dir = Path(args.state).parent
    state_dir.mkdir(parents=True, exist_ok=True)
    try:
        cfg = load_github_config(args.config)
        out = fetch_github(top=args.top, themes=cfg["themes"],
                           since_days=cfg["since_days"],
                           snapshots_path=args.snapshots)
    except Exception as exc:  # noqa: BLE001 - 失败也要留下成本日志与非0退出
        record = append_cost(state_dir / "cost_log.jsonl", {
            "command": f"github --top {args.top}",
            "provider": "无", "model": "无",
            "calls": 0, "prompt_tokens": UNKNOWN, "completion_tokens": UNKNOWN,
            "tokens": UNKNOWN, "cost": UNKNOWN,
            "cost_basis": NO_LLM_COST_BASIS,
            "external_api_calls": {"github": 0, "aihot": 0, "official": 0,
                                   "x": 0, "import": 0},
            "note": f"GitHub 命令失败：{type(exc).__name__}: {exc}",
        })
        print(f"[github] ❌ 失败：{type(exc).__name__}: {exc}")
        print(f"[成本] provider={record['provider']} 金额={record['金额']}；"
              f"成本口径={record['成本口径']}")
        return 1
    stamp = iso_now().replace(":", "").replace("+", "_")
    path = state_dir / f"github_{stamp}.json"
    payload = {"time": iso_now(), "command": f"github --top {args.top}",
               **out}
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                    encoding="utf-8")

    print(f"[github] top={args.top} 真实请求 {out['api_calls']} 次；"
          f"结果 {len(out['items'])} 个 → {path}")
    print(f"{'仓库':<34}{'Stars':>8}  {'最近更新':<12}{'最新版本':<22}"
          f"{'许可证':<12}{'安装难度':<12}适合人群 | 是否值得做视频")
    for it in out["items"]:
        gh = it["extra"]["github"]
        users = "、".join(gh.get("target_users") or [])
        print(f"{gh.get('full_name', ''):<34}{gh.get('stars', 0):>8}  "
              f"{(gh.get('last_update') or '')[:10]:<12}"
              f"{str(gh.get('latest_release', '无'))[:20]:<22}"
              f"{str(gh.get('license', ''))[:10]:<12}"
              f"{str(gh.get('install_difficulty', ''))[:10]:<12}"
              f"{users} | {gh.get('worth_video', '')}")
    for err in out.get("errors") or []:
        print(f"[github] ⚠️ {err}")

    record = append_cost(state_dir / "cost_log.jsonl", {
        "command": f"github --top {args.top}",
        "provider": "无", "model": "无",
        "calls": 0, "prompt_tokens": UNKNOWN, "completion_tokens": UNKNOWN,
        "tokens": UNKNOWN, "cost": UNKNOWN,
        "cost_basis": NO_LLM_COST_BASIS,
        "external_api_calls": {"github": out["api_calls"], "aihot": 0,
                               "official": 0, "x": 0, "import": 0},
        "note": "无 LLM 调用；GitHub 官方 REST 为免费接口，实际金额未知",
    })
    print(f"[成本] provider={record['provider']} 金额={record['金额']}；"
          f"成本口径={record['成本口径']}；GitHub 请求 {out['api_calls']} 次")
    return 0


# ----------------------------------------------------------------- sources-check
def cmd_sources_check(args) -> int:
    """真实小样验证：采集→归一化→校验→去重；可选仅新增写入 01 小样。"""
    sources = ["aihot", "github", "official", "x", "import"]
    if not args.x_file:
        sources.remove("x")
    if not args.import_file:
        sources.remove("import")
    state_dir = Path(args.state).parent
    state_dir.mkdir(parents=True, exist_ok=True)
    out = pipeline.step_collect(
        _DryState(args.state), x_posts_file=args.x_file,
        x_following_file=None, live=True, snapshot_dir=None,
        sources=sources, github_top=args.github_top,
        official_limit=args.official_limit, aihot_limit=args.aihot_limit,
        import_file=args.import_file, config_path=args.config,
        import_errors_path=state_dir / "import_errors.jsonl")

    def source_of(item_id) -> str:
        prefix = str(item_id or "").split(":", 1)[0]
        return {"x_posts": "x"}.get(prefix, prefix)

    deduped = {name: 0 for name in sources}
    for report in out["dropped"]:
        name = source_of(report.get("item_id"))
        if name in deduped:
            deduped[name] += 1
    validation = {name: 0 for name in sources}
    for stype, count in (out.get("validation_error_counts") or {}).items():
        name = {"x_posts": "x"}.get(stype, stype)
        if name in validation:
            validation[name] += count
    fetch_errors = {name: 0 for name in sources}
    for name, errs in (out.get("source_errors") or {}).items():
        if name in fetch_errors:
            fetch_errors[name] += len(errs)
    per_source = {}
    for name in sources:
        per_source[name] = {
            "fetched": out["per_source"].get(name, 0),
            "errors": validation.get(name, 0) + fetch_errors.get(name, 0),
            "deduped": deduped.get(name, 0),
        }

    print("[sources-check] 真实小样：归一化/校验/去重（默认不写 Notion）")
    print(f"{'来源':<12}{'条数':>6}{'错误':>6}{'去重':>6}")
    for name in sources:
        row = per_source[name]
        print(f"{name:<12}{row['fetched']:>6}{row['errors']:>6}{row['deduped']:>6}")
    total_fetched = sum(r["fetched"] for r in per_source.values())
    total_errors = sum(r["errors"] for r in per_source.values())
    total_deduped = sum(r["deduped"] for r in per_source.values())
    print(f"{'合计':<12}{total_fetched:>6}{total_errors:>6}{total_deduped:>6}")
    print(f"[sources-check] 去重后素材 {len(out['materials'])} 条；"
          f"外部请求 {out['external_calls']}")
    for name, errs in (out.get("source_errors") or {}).items():
        print(f"[sources-check] ⚠️ {name} 源错误 {len(errs)} 条：{str(errs[0])[:180]}")

    notion_summary = None
    if getattr(args, "write_notion", False):
        try:
            client = NotionClient()
            db_ids = ensure_kb(client, getattr(args, "page_id", DEFAULT_PAGE_ID))
            ensure_kb_upgrades(client, db_ids)
            notion_summary = pipeline.step_write_raw_materials_if_absent(
                client, db_ids, out["materials"],
                max_rows=getattr(args, "max_rows", 10),
                sources=getattr(args, "notion_sources", "aihot,github,official"))
        except Exception as exc:  # noqa: BLE001 - 写入失败要明确非0
            print(f"[sources-check] ❌ Notion 写入失败："
                  f"{type(exc).__name__}: {exc}")
            return 1
        print("[sources-check] Notion 01 写入摘要：")
        print(json.dumps(notion_summary, ensure_ascii=False))

    stamp = iso_now().replace(":", "").replace("+", "_")
    path = state_dir / f"sources_check_{stamp}.json"
    payload = {
        "time": iso_now(),
        "command": "sources-check",
        "sources": sources,
        "per_source": per_source,
        "totals": {"fetched": total_fetched, "errors": total_errors,
                   "deduped": total_deduped,
                   "materials_after_dedupe": len(out["materials"])},
        "external_api_calls": out["external_calls"],
        "validation_error_counts": out.get("validation_error_counts") or {},
        "source_errors": out.get("source_errors") or {},
        "items": out["materials"],
    }
    if notion_summary is not None:
        payload["notion_write"] = notion_summary
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                    encoding="utf-8")
    print(f"[sources-check] 结果已写入 {path}")

    record = append_cost(state_dir / "cost_log.jsonl", {
        "command": "sources-check",
        "provider": "无", "model": "无",
        "calls": 0, "prompt_tokens": UNKNOWN, "completion_tokens": UNKNOWN,
        "tokens": UNKNOWN, "cost": UNKNOWN,
        "cost_basis": NO_LLM_COST_BASIS,
        "external_api_calls": out["external_calls"],
        "note": "无 LLM 调用；外部 API 为免费接口，实际金额未知",
    })
    print(f"[成本] provider={record['provider']} 金额={record['金额']}；"
          f"成本口径={record['成本口径']}；外部请求={record['external_api_calls']}")
    return 0


# ------------------------------------------------------------------ feedback/rules
def _load_client_state(args, client=None):
    state = WorkflowState(args.state).load()
    client = client or NotionClient()
    db_ids = state.get("db_ids") or ensure_kb(client, args.page_id)
    ensure_kb_upgrades(client, db_ids)
    state.update(db_ids=db_ids, page_root=args.page_id)
    return state, client, db_ids


def cmd_feedback(args, client=None) -> int:
    """记录一条 GATE-1 反馈到 05（手动触发；绝不改 03）。"""
    from src.analysis import preference
    state, client, db_ids = _load_client_state(args, client)
    round_no = getattr(args, "round", None)
    if round_no is None:
        round_no = state.get("round_id") or 1
    feedback = {
        "topic_id": args.topic,
        "原始选题": getattr(args, "title", "") or "",
        "推荐理由": getattr(args, "why", "") or "",
        "用户选择": args.pick,
        "原话反馈": args.say,
        "原因标签": getattr(args, "reason", "") or "",
        "轮次": round_no,
        "下轮参考": getattr(args, "next", "") or "",
        "time": iso_now(),
    }
    out = preference.record_feedback(client, db_ids, feedback)
    state.save()
    print(f"✅ 已写反馈：{out['feedback_id']}（类型=反馈，性质=用户偏好，状态=待验证）")
    if out.get("approved_material_id"):
        print(f"✅ 已写认可素材：{out['approved_material_id']}")
    print("ℹ️ 未修改 03_选题决策 的任何卡状态；如需确认规则请到 Notion 手动操作。")
    return 0


def cmd_feedback_seed(args, client=None) -> int:
    """从本地 JSON 批量写反馈（文件放 data/state，不入库）。"""
    from src.analysis import preference
    path = Path(args.file)
    if not path.exists():
        print(f"❌ 反馈文件不存在：{path}")
        return 1
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict):
        records = (data.get("items") or data.get("feedbacks")
                   or data.get("records"))
        if records is None and "topic_id" in data:
            records = [data]
    else:
        records = data
    if not isinstance(records, list):
        print("❌ 反馈 JSON 应为数组或 {\"items\": [...]}")
        return 1
    _, client, db_ids = _load_client_state(args, client)
    written = []
    for rec in records:
        if not isinstance(rec, dict):
            continue
        written.append(preference.record_feedback(client, db_ids, rec))
    print(f"✅ 批量写入反馈 {len(written)} 条（幂等；已存在不重复）。")
    return 0


def cmd_rules(args, client=None) -> int:
    """候选规则：--propose 生成待确认规则；--confirm 只提示用户手动改状态。"""
    from src.analysis import preference
    if getattr(args, "confirm", None):
        record_id = args.confirm
        print("请在 Notion 中由用户手动把状态改为正在使用"
              f"（record_id={record_id}）。代码不会自动修改状态。")
        return 0
    if not getattr(args, "propose", False):
        print("用法：rules --propose 或 rules --confirm <record_id>")
        return 1
    state, client, db_ids = _load_client_state(args, client)
    if getattr(args, "propose", False):
        chat = None
        if getattr(args, "llm", "template") == "llm":
            from src.llm.provider_chain import ProviderChain
            chat = ProviderChain()
        out = preference.propose_rules(client, db_ids, chat=chat)
        if not out["proposed"]:
            print(f"ℹ️ {out.get('reason')}（轮次：{out.get('rounds')}）")
            return 0
        print(f"✅ 已生成候选规则 {len(out['proposed'])} 条（类型=候选规则，状态=待确认，"
              f"引擎={out['engine']}）：")
        for rid in out["proposed"]:
            print(f"  - {rid}")
        print("⚠️ 候选规则仅供参考，绝不会自动生效；请在 Notion 中人工确认后"
              "手动把状态改为「正在使用」。")
        state.save()
        return 0
    print("用法：rules --propose 或 rules --confirm <record_id>")
    return 1


# --------------------------------------------------------------- topics-round
def _flatten_demand_comments(materials: list[dict]) -> list[dict]:
    flat = []
    for m in materials:
        for idx, comment in enumerate(m.get("comments") or []):
            text = (comment.get("text") if isinstance(comment, dict)
                    else str(comment or "")).strip()
            if not text:
                continue
            flat.append({
                "text": text,
                "source_url": (comment.get("source_url") if isinstance(comment, dict)
                               else "") or m.get("source_url", ""),
                "item_id": m["item_id"],
                "comment_index": idx,
            })
    return flat


def _attach_demand_results(materials: list[dict], classified: list[dict]) -> None:
    by_item = {m["item_id"]: m for m in materials}
    for entry in classified:
        m = by_item.get(entry.get("item_id"))
        idx = entry.get("comment_index")
        if m is None or idx is None:
            continue
        comments = m.get("comments") or []
        if 0 <= idx < len(comments) and isinstance(comments[idx], dict):
            comments[idx]["demand_tags"] = entry["tags"]
        tags = m.setdefault("demand_tags", [])
        for tag in entry["tags"]:
            if tag not in tags:
                tags.append(tag)
        m.setdefault("demand_evidence", []).append({
            "text": entry["text"], "tags": entry["tags"],
            "source_url": entry.get("source_url", ""),
        })


def _write_preference_injection(state_dir: Path, text: str, now: datetime) -> Path:
    state_dir.mkdir(parents=True, exist_ok=True)
    path = state_dir / f"preference_injection_{now.strftime('%Y%m%dT%H%M%S')}.txt"
    path.write_text(text or "（本轮无偏好注入）", encoding="utf-8")
    return path


def _log_topics_round_cost(args, chat, out: dict, *, demand_count: int,
                           card_count: int) -> dict:
    path = Path(args.state).parent / "cost_log.jsonl"
    if chat is None:
        stats = {"calls": 0, "prompt_tokens": UNKNOWN,
                 "completion_tokens": UNKNOWN, "tokens": UNKNOWN,
                 "cost": UNKNOWN, "cost_basis": NO_LLM_COST_BASIS,
                 "list_cost": UNKNOWN, "list_cost_basis": UNKNOWN}
        provider, model = "无", "无"
    else:
        stats = _aggregate_usage(chat)
        provider = getattr(chat, "last_provider", None) or getattr(chat, "model", UNKNOWN)
        model = getattr(chat, "last_model", None) or getattr(chat, "model", UNKNOWN)
    external = out.get("external_calls") if isinstance(out.get("external_calls"), dict) else {}
    note = (f"需求分类 {demand_count} 条；合并选题卡 {card_count} 张；"
            "cost 为接口返回 list_cost 时按套餐额度折算（非实付），未知写「未知」")
    if stats.get("list_cost") != UNKNOWN:
        note += "；OpenCode list_cost 为套餐额度折算，非实付"
    record = append_cost(path, {
        "command": f"topics-round --round-id {getattr(args, 'round_id', '') or ''}",
        "provider": provider, "model": model,
        **stats,
        "external_api_calls": external,
        "note": note,
    })
    print(f"[成本] provider={record['provider']} model={record['model']}；"
          f"调用 {record['calls']} 次，tokens={record['tokens']}，"
          f"金额={record['金额']}；成本口径={record['成本口径']}")
    return record


def _source_group_of(material: dict) -> str:
    source = str(material.get("source_type") or material.get("platform") or "")
    return "x_posts" if source == "x" else source


def _mark_engine(cards: list[dict], engine: str) -> list[dict]:
    for card in cards:
        card["生成引擎"] = engine
    return cards


def _heuristic_round_cards(pool: list[dict], *, now: datetime, profile,
                           max_cards: int, engine: str) -> list[dict]:
    """启发式出卡：需求轨 + 机会型按来源分组，保证来源代表性。"""
    from src.analysis.demand import has_demand_evidence
    from src.analysis.topics import build_topic_cards

    cards: list[dict] = []
    demand_pool = [m for m in pool if has_demand_evidence(m)]
    opp_pool = [m for m in pool if not has_demand_evidence(m)]
    if demand_pool:
        cs, _ = build_topic_cards(demand_pool, top_k=max_cards, now=now,
                                  profile=profile)
        for card in cs:
            card["_intended_type"] = "需求型"
        cards.extend(cs)

    grouped: dict[str, list[dict]] = {}
    for material in opp_pool:
        grouped.setdefault(_source_group_of(material), []).append(material)
    for source in (*OPPORTUNITY_SOURCES, "x_posts"):
        group = grouped.get(source) or []
        if not group:
            continue
        cs, _ = build_topic_cards(group, top_k=min(3, max_cards), now=now,
                                  profile=profile)
        for card in cs:
            card["_intended_type"] = "机会型"
            card["_source_group"] = source
        cards.extend(cs)
    # 其他来源（import 等）也需要机会型兜底
    other = [m for source, group in grouped.items()
             if source not in (*OPPORTUNITY_SOURCES, "x_posts")
             for m in group]
    if other:
        cs, _ = build_topic_cards(other, top_k=min(3, max_cards), now=now,
                                  profile=profile)
        for card in cs:
            card["_intended_type"] = "机会型"
        cards.extend(cs)
    return _mark_engine(cards, engine)


def _postprocess_round_cards(cards: list[dict], materials: list[dict], *,
                             now: datetime, profile, max_cards: int,
                             active_rules: list[str] | None = None) -> list[dict]:
    from src.analysis.demand import (demand_evidence_for_item,
                                     has_demand_evidence, short_evidence_texts)
    from src.analysis.privacy import sanitize_card
    mat_by_id = {m["item_id"]: m for m in materials}
    allowed_opportunity = {"aihot", "github", "official", "x_posts"}
    final: list[dict] = []
    for c in cards:
        mats = [mat_by_id[i] for i in c.get("关联素材") or [] if i in mat_by_id]
        if not mats:
            continue
        ctype = c.get("_intended_type") or c.get("选题类型") or c.get("类型")
        if ctype not in ("需求型", "机会型"):
            ctype = ("需求型" if any(has_demand_evidence(m) for m in mats)
                     else "机会型")
        if ctype == "需求型":
            evidence: list[str] = []
            for m in mats:
                for text in short_evidence_texts(m):
                    if text not in evidence:
                        evidence.append(text)
            if not evidence:
                continue  # 需求型卡必须引用评论原文证据
            c["需求证据"] = ("评论证据（脱敏，≤3条）："
                             + "；".join(f"「{t}」" for t in evidence[:3]))
        else:
            if not any(m.get("source_type") in allowed_opportunity
                       or m.get("platform") in {"aihot", "github", "official", "x"}
                       for m in mats):
                continue  # 机会型卡必须来自 aihot/github/official/X 帖子
        source_urls = [m["source_url"] for m in mats if m.get("source_url")]
        if not source_urls:
            continue
        if ctype == "需求型":
            demand_mats = [m for m in mats if has_demand_evidence(m)]
            demand_ev = []
            for m in demand_mats:
                demand_ev.extend(demand_evidence_for_item(m))
            candidates = demand_mats or mats
            primary = max(
                candidates,
                key=lambda m: score_item_v3(
                    m, now=now, profile=profile, demand=demand_ev,
                    active_rules=active_rules)["adjusted_total"])
            v3 = score_item_v3(primary, now=now, profile=profile,
                               demand=demand_ev, active_rules=active_rules)
        else:
            # 不按 refs 顺序盲取第一条：取引用素材中 v3 最强的一条作为主证据。
            primary = max(
                mats,
                key=lambda m: score_item_v3(
                    m, now=now, profile=profile,
                    active_rules=active_rules)["adjusted_total"])
            v3 = score_item_v3(primary, now=now, profile=profile,
                               active_rules=active_rules)
        c["选题类型"] = ctype
        c["类型"] = ctype
        if not c.get("_source_group"):
            c["_source_group"] = _source_group_of(primary)
        c["评分版本"] = SCORING_VERSION_V3
        c["轨道得分"] = v3["adjusted_track_score"]
        c["ranking_track_score"] = v3["adjusted_track_score"]
        c["ranking_total"] = v3["adjusted_total"]
        c["rule_adjustment"] = v3["rule_adjustment"]
        c["副业证据等级"] = v3["side_hustle_evidence_level"]
        c["传播潜力假设(不计分)"] = v3["propagation_hypothesis"]["text"]
        c["综合评分"] = v3["adjusted_total"]
        # 最终推荐级别只按 v3-dual；LLM 分只作为推荐理由中的文字参考。
        llm_score = c.get("_llm_score")
        c["推荐级别"] = v3["level"]
        base_reason = str(c.get("推荐理由") or "").strip()
        v3_reason = (f"v3 总分 {v3['adjusted_total']}/18（满分 18），轨道分 "
                     f"{v3['adjusted_track_score']}")
        if v3["rule_adjustment"]:
            v3_reason += f"（已应用正在使用规则加减分 {v3['rule_adjustment']:+d}）"
        llm_ref = ""
        if _is_number(llm_score) and f"{llm_score}/10" not in base_reason:
            service = c.get("_llm_service") or "LLM"
            llm_ref = f"LLM 参考分（{service}）：{llm_score}/10（不参与推荐级别）"
        if llm_ref:
            v3_reason += "；" + llm_ref
        c["推荐理由"] = (base_reason + "；" + v3_reason
                         if base_reason else v3_reason)
        material_text = " ".join(
            str(m.get("title_or_summary") or "") for m in mats)
        old_audience = filled(c.get("面向谁"))
        existing_user = filled(c.get("面向用户"))
        audience_input = (existing_user
                          if parse_audience_categories(existing_user)
                          else (old_audience or existing_user or "待验证"))
        c["面向用户"] = audience_user(
            audience_input,
            c.get("拟定标题"), c.get("核心价值"), c.get("为什么适合"),
            material_text)
        c["痛点或机会"] = (filled(c.get("痛点或机会"))
                           or filled(c.get("为什么适合"))
                           or filled(c.get("核心价值")) or "待验证")
        c["原始来源"] = source_urls[0]
        c["为何现在做"] = (filled(c.get("为何现在做"))
                           or filled(c.get("为什么现在")) or "待验证")
        c["国内可用"] = normalize_china_usability(
            filled(c.get("国内可用")) or filled(c.get("国内可用性")))
        if v3.get("overseas_new_opportunity"):
            c["国内可用"] = OVERSEAS_OPPORTUNITY_LABEL
            c["国外新机会"] = OVERSEAS_OPPORTUNITY_LABEL
        c["是否付费"] = (filled(c.get("是否付费"))
                         or paid_label(material_text))
        c["能否实际验证"] = (filled(c.get("能否实际验证"))
                             or ("可验证：有原始出处链接"
                                 if source_urls else "待验证"))
        c["切入角度"] = (filled(c.get("切入角度"))
                         or filled(c.get("切入角度与钩子")) or "待验证")
        c["主要风险"] = (filled(c.get("主要风险"))
                         or filled(c.get("可信度与风险")) or "待验证")
        c["引用帖子"] = "；".join(source_urls)  # X 卡只放 url，不写用户名/正文全文
        # 规则2：国外新工具卡不得出现注册/翻墙/VPN 等开通步骤；只讲能力。
        if v3.get("overseas_new_opportunity"):
            scrub_card_forbidden_steps(c)
        c.setdefault("生成引擎", "启发式")
        c["_source_urls"] = source_urls
        c["_v3"] = v3
        final.append(c)
    merged = merge_dual_tracks(
        final, top_n=max_cards,
        opportunity_sources=OPPORTUNITY_SOURCES)
    for card in merged:
        sanitize_card(card, materials)
    return merged


def _generate_round_cards(args, out, *, chat, profile, now, preference_text,
                          active_rules):
    """采集结果 → 需求分类 → shortlist → LLM/启发式出卡 → v3 后处理。"""
    from src.analysis import demand as demand_engine
    from src.analysis import llm_topics

    materials = pipeline.eligible_materials(out["materials"])
    analyze_mode = getattr(args, "analyze", "llm")
    use_llm = analyze_mode == "llm" and chat is not None
    # 3) 需求分类（LLM 失败回退关键词）；--analyze heuristic 时不得调用 LLM。
    flat = _flatten_demand_comments(materials)
    classified = demand_engine.classify_demands(
        flat, chat=chat if use_llm else None,
        preference_text=preference_text)
    _attach_demand_results(materials, classified)
    aggregates = demand_engine.aggregate_demands(classified)

    # 4) shortlist + 打分/出卡（需求池与机会池分别生成）
    max_cards = max(1, min(10, int(getattr(args, "max_cards", 10))))
    shortlist_k = max(1, int(getattr(args, "shortlist", 40)))
    short = llm_topics.shortlist(
        materials, k=shortlist_k, profile=profile,
        ensure_sources=OPPORTUNITY_SOURCES, per_source_min=3)
    # 需求证据素材优先级：保证需求轨有候选，同时总 shortlist 不突破 --shortlist 上限。
    demand_candidates = [m for m in materials
                         if demand_engine.has_demand_evidence(m)]
    demand_ranked = (llm_topics.shortlist(
        demand_candidates, k=shortlist_k, profile=profile)
        if demand_candidates else [])
    ordered: list[dict] = []
    seen_ids: set[str] = set()

    def _take(material: dict) -> None:
        item_id = material.get("item_id")
        if item_id not in seen_ids:
            ordered.append(material)
            seen_ids.add(item_id)

    for source in OPPORTUNITY_SOURCES:
        for material in [m for m in short if _source_group_of(m) == source][:3]:
            _take(material)
    for material in demand_ranked:
        _take(material)
    for material in short:
        _take(material)
    short = ordered[:shortlist_k]
    source_counts = Counter(_source_group_of(m) for m in short)
    print(f"[S2] shortlist {len(short)} 条（上限 {shortlist_k}），来源："
          + "，".join(f"{k}={v}" for k, v in sorted(source_counts.items())))

    cards: list[dict] = []
    if use_llm:
        try:
            glm_scores = llm_topics.glm_score_posts(
                chat, short, profile, preference_text=preference_text)
        except Exception as exc:  # noqa: BLE001 - 打分失败仍可尝试出卡/启发式
            print(f"⚠️ LLM 打分失败，继续尝试出卡：{type(exc).__name__}: {exc}")
            glm_scores = {}
        demand_pool = [m for m in short
                       if demand_engine.has_demand_evidence(m)]
        opp_pool = [m for m in short
                    if not demand_engine.has_demand_evidence(m)]
        demand_pool.sort(
            key=lambda m: -score_item_v3(
                m, now=now, profile=profile,
                active_rules=active_rules)["ranking_track_score"])
        opp_pool.sort(
            key=lambda m: -score_item_v3(
                m, now=now, profile=profile,
                active_rules=active_rules)["ranking_track_score"])

        if demand_pool:
            try:
                cs, _meta = llm_topics.glm_build_cards(
                    chat, demand_pool, profile, now=now, glm_scores=glm_scores,
                    min_cards=1, max_cards=max_cards,
                    preference_text=preference_text, card_type="需求型")
                for c in cs:
                    c["_intended_type"] = "需求型"
                    c["生成引擎"] = "LLM"
                cards.extend(cs)
            except Exception as exc:  # noqa: BLE001 - 单轨失败不拖垮另一轨
                print(f"⚠️ 需求型 LLM 出卡失败，稍后用启发式补："
                      f"{type(exc).__name__}: {exc}")

        # 机会型按来源分组尝试：保证 aihot/github/official 每类至少 1 张。
        grouped: dict[str, list[dict]] = {}
        for material in opp_pool:
            grouped.setdefault(_source_group_of(material), []).append(material)
        for source in (*OPPORTUNITY_SOURCES, "x_posts"):
            group = grouped.get(source) or []
            if not group:
                continue
            try:
                cs, _meta = llm_topics.glm_build_cards(
                    chat, group, profile, now=now, glm_scores=glm_scores,
                    min_cards=1, max_cards=min(3, max_cards),
                    preference_text=preference_text, card_type="机会型")
                for c in cs:
                    c["_intended_type"] = "机会型"
                    c["_source_group"] = source
                    c["生成引擎"] = "LLM"
                cards.extend(cs)
            except Exception as exc:  # noqa: BLE001 - 单源失败不拖垮其他来源
                print(f"⚠️ {source} 机会型 LLM 出卡失败："
                      f"{type(exc).__name__}: {exc}")

    if not cards:
        if use_llm:
            print("⚠️ LLM 全部失败，回退启发式（卡上标注生成引擎=启发式回退）")
        cards = _heuristic_round_cards(
            short, now=now, profile=profile, max_cards=max_cards,
            engine=HEURISTIC_FALLBACK_ENGINE if use_llm else "启发式")

    merged = _postprocess_round_cards(
        cards, materials, now=now, profile=profile, max_cards=max_cards,
        active_rules=active_rules)
    if not merged:
        if use_llm:
            print("⚠️ LLM 出卡均未通过校验，回退启发式")
        heuristic = _heuristic_round_cards(
            short, now=now, profile=profile, max_cards=max_cards,
            engine=HEURISTIC_FALLBACK_ENGINE if use_llm else "启发式")
        merged = _postprocess_round_cards(
            heuristic, materials, now=now, profile=profile,
            max_cards=max_cards, active_rules=active_rules)
    else:
        # 双轨交替之外再兜底：若某类素材存在但 LLM 没出该类卡，用启发式补一张。
        present = {c.get("选题类型") for c in merged}
        if len(present) == 1:
            missing = "机会型" if "需求型" in present else "需求型"
            pool = [m for m in short
                    if (demand_engine.has_demand_evidence(m)
                        if missing == "需求型"
                        else not demand_engine.has_demand_evidence(m))]
            if pool:
                extra_cards = _heuristic_round_cards(
                    pool, now=now, profile=profile, max_cards=max_cards,
                    engine=HEURISTIC_FALLBACK_ENGINE if use_llm else "启发式")
                extra = _postprocess_round_cards(
                    extra_cards, materials, now=now, profile=profile,
                    max_cards=max_cards, active_rules=active_rules)
                merged = merge_dual_tracks(
                    merged + extra, top_n=max_cards,
                    opportunity_sources=OPPORTUNITY_SOURCES)
    return merged, materials, classified, aggregates


MARKER_SIMULATED = "[模拟样例·请忽略]"


def _round_record_path(args, round_id: str) -> Path:
    safe = str(round_id or "repair").replace("/", "_").replace(os.sep, "_")
    return Path(args.state).parent / f"topics_round_{safe}.json"


def _save_round_record(args, round_id: str, cards: list[dict],
                       materials: list[dict], out: dict) -> Path:
    used_ids = {iid for c in cards for iid in c.get("关联素材") or []}
    by_id = {m["item_id"]: m for m in materials}
    path = _round_record_path(args, round_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "round_id": round_id,
        "saved_at": iso_now(),
        "topic_ids": [c["topic_id"] for c in cards],
        "cards": cards,
        "material_ids": sorted(used_ids),
        "materials": [
            {"item_id": iid,
             "source_type": by_id[iid].get("source_type"),
             "source_url": by_id[iid].get("source_url")}
            for iid in sorted(used_ids) if iid in by_id
        ],
        "sources": out.get("sources") or [],
        "external_calls": out.get("external_calls") or {},
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                    encoding="utf-8")
    print(f"[轮次存档] 已保存 {path}")
    return path


def _load_round_record(args, round_id: str | None) -> dict | None:
    if not round_id:
        return None
    path = _round_record_path(args, round_id)
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _repair_id_list(record: dict | None, args) -> list[str]:
    ids: list[str] = []
    if record:
        ids.extend(str(t) for t in (record.get("topic_ids") or []) if t)
        if not ids:
            ids.extend(str(c.get("topic_id")) for c in
                       (record.get("cards") or []) if c.get("topic_id"))
    raw = getattr(args, "repair_ids", None)
    if raw:
        if isinstance(raw, str):
            raw = re.split(r"[,，\s]+", raw)
        ids.extend(str(t).strip() for t in raw if str(t).strip())
    out: list[str] = []
    seen: set[str] = set()
    for topic_id in ids:
        if topic_id not in seen:
            out.append(topic_id)
            seen.add(topic_id)
    return out


def _prop_text(prop: dict | None) -> str:
    prop = prop or {}
    for key in ("title", "rich_text"):
        if prop.get(key):
            return "".join(x.get("text", {}).get("content", "")
                           for x in prop[key])
    return str(prop.get("url") or prop.get("select", {}).get("name", "") or "")


def _next_topic_sequence(client, db_id: str, day: str) -> int:
    """读取 03 当日已有 topic_id 的最大 V 序号，返回下一个可用序号。"""
    pattern = re.compile(rf"^TX-{re.escape(day)}-V(\d+)$")
    max_index = 0
    for row in client.query_database(db_id):
        props = row.get("properties") or {}
        match = pattern.match(_prop_text(props.get("topic_id")))
        if match:
            max_index = max(max_index, int(match.group(1)))
    return max_index + 1


def _existing_material_sources(client, db_ids, candidate_ids: set[str]) -> dict:
    """从 01 查询候选素材的 source_url（用于 fallback repair 判断 example 域名）。"""
    if client is None or not db_ids or not db_ids.get("01_原始素材"):
        return {}
    found: dict[str, str] = {}
    for row in client.query_database(db_ids["01_原始素材"]):
        props = row.get("properties") or {}
        iid = _prop_text(props.get("item_id"))
        if iid in candidate_ids:
            found[iid] = _prop_text(props.get("source_url"))
    return found


def _round_simulated_sources(record: dict | None, client, db_ids,
                             topic_ids: list[str]) -> tuple[set[str], set[str]]:
    """返回 (需标记的 03 topic_id, 需标记的 01 item_id)。"""
    fake_topics: set[str] = set()
    fake_materials: set[str] = set()
    wanted = set(topic_ids)
    candidate_materials: set[str] = set()
    linked_by_topic: dict[str, list[str]] = {}
    source_by_topic: dict[str, str] = {}

    if record:
        material_info = {m.get("item_id"): m for m in
                         (record.get("materials") or []) if m.get("item_id")}
        candidate_materials.update(
            str(i) for i in (record.get("material_ids") or []) if i)
        for card in record.get("cards") or []:
            tid = card.get("topic_id")
            if tid not in wanted:
                continue
            linked = [str(i) for i in (card.get("关联素材") or [])]
            linked_by_topic[tid] = linked
            candidate_materials.update(linked)
            if card.get("原始来源"):
                source_by_topic[tid] = str(card.get("原始来源"))
            urls = [card.get("原始来源")] + list(card.get("_source_urls") or [])
            if any(is_simulated_source(None, u) for u in urls):
                fake_topics.add(tid)
    else:
        if client and db_ids and db_ids.get("03_选题决策"):
            for row in client.query_database(db_ids["03_选题决策"]):
                props = row.get("properties") or {}
                tid = _prop_text(props.get("topic_id"))
                if tid not in wanted:
                    continue
                linked_text = _prop_text(props.get("关联素材"))
                linked = [x for x in re.split(r"[、,，;；\s]+", linked_text) if x]
                linked_by_topic[tid] = linked
                candidate_materials.update(linked)
                source_by_topic[tid] = _prop_text(props.get("原始来源"))

    existing_sources = _existing_material_sources(
        client, db_ids, candidate_materials)
    for iid in candidate_materials:
        recorded = (record or {}).get("materials") if record else None
        recorded_url = ""
        if recorded:
            recorded_url = next(
                (m.get("source_url") or "" for m in recorded
                 if m.get("item_id") == iid), "")
        if is_simulated_source(iid, recorded_url or existing_sources.get(iid)):
            fake_materials.add(iid)

    for tid, linked in linked_by_topic.items():
        if (is_simulated_source(None, source_by_topic.get(tid))
                or any(iid in fake_materials for iid in linked)):
            fake_topics.add(tid)

    # 旧存档若只有 material_ids 而没有卡内关联明细，无法逐卡映射：
    # 该轮所有 topic_id 都视为受模拟素材影响（仍只限本轮）。
    if record and fake_materials and not any(linked_by_topic.values()):
        fake_topics.update(wanted)
    return fake_topics, fake_materials


def _mark_simulated_rows(client, db_ids, fake_topics: set[str],
                         fake_materials: set[str]) -> dict:
    marked_topics = [tid for tid in sorted(fake_topics)
                     if pipeline.prefix_notion_title(
                         client, db_ids, "03_选题决策", "topic_id", tid,
                         "拟定标题", MARKER_SIMULATED)]
    marked_materials = [iid for iid in sorted(fake_materials)
                        if pipeline.prefix_notion_title(
                            client, db_ids, "01_原始素材", "item_id", iid,
                            "title_or_summary", MARKER_SIMULATED)]
    return {"topics": marked_topics, "materials": marked_materials}


def cmd_topics_round_repair(args, *, client=None, chat=None, profile=None,
                            now: datetime | None = None) -> int:
    """修复指定轮次：只更新该轮自建 topic_id 的 03 行，并标记模拟样例行。"""
    from src.analysis import llm_topics  # noqa: F401 - 保持与 topics-round 同路径
    from src.connectors.x_posts_source import load_x_posts

    now = now or datetime.now(timezone.utc)
    dry = bool(getattr(args, "dry_run", False))
    round_id = getattr(args, "repair_round", None)
    record = _load_round_record(args, round_id)
    topic_ids = _repair_id_list(record, args)
    if not topic_ids:
        print("❌ repair 缺少 topic_id：本地无该轮存档时请显式传 "
              "--repair-ids TX-...[,TX-...]")
        return 1
    print(f"[repair] 轮次 {round_id or '(未命名)'}：只更新 {len(topic_ids)} 个 topic_id "
          f"（来源：{'本地存档' if record else '--repair-ids'}）")

    state = _DryState(args.state) if dry else WorkflowState(args.state)
    if not dry:
        state.load()
    if profile is None and getattr(args, "audience", None):
        profile = load_profile(args.audience)
    analyze_mode = getattr(args, "analyze", "llm")
    if chat is None and analyze_mode == "llm":
        try:
            from src.llm.provider_chain import ProviderChain
            chat = ProviderChain()
        except Exception as exc:  # noqa: BLE001
            print(f"⚠️ LLM 未配置，回退启发式：{type(exc).__name__}: {exc}")
            chat = None
    if profile is None and chat is not None:
        profile = load_profile()

    db_ids = state.get("db_ids")
    if client is not None and db_ids is None:
        db_ids = ensure_kb(client, args.page_id)
    fake_topics, fake_materials = _round_simulated_sources(
        record, client, db_ids, topic_ids)

    x_replies = getattr(args, "x_replies_file", None)
    x_loader = ((lambda p: load_x_posts(p, replies_path=x_replies))
                if x_replies else load_x_posts)
    aihot_file = getattr(args, "aihot_file", None)
    live = bool(getattr(args, "live", True)) and not aihot_file
    out = pipeline.step_collect(
        state, aihot_file=aihot_file,
        x_following_file=getattr(args, "x_following", None),
        x_posts_file=getattr(args, "x_file", None),
        live=live,
        snapshot_dir=None if dry else "data/raw",
        sources=getattr(args, "sources", "aihot,github,official,x"),
        github_top=getattr(args, "github_top", 5),
        official_limit=getattr(args, "official_limit", 3),
        aihot_limit=getattr(args, "aihot_limit", 20),
        import_file=getattr(args, "import_file", None),
        config_path=getattr(args, "config", None),
        x_posts_loader=x_loader)
    merged, materials, classified, aggregates = _generate_round_cards(
        args, out, chat=chat, profile=profile, now=now,
        preference_text="", active_rules=[])
    if not merged:
        print("❌ repair 未生成任何可用新卡（真实素材不足或校验失败）；未更新 Notion。")
        return 1
    if len(merged) < len(topic_ids):
        print(f"⚠️ 新卡 {len(merged)} 张少于待修复 topic_id {len(topic_ids)} 个："
              "只更新有对应新卡的行，其余行保持原样。")
    cards = merged[:len(topic_ids)]
    for card, topic_id in zip(cards, topic_ids):
        card["topic_id"] = topic_id

    if getattr(args, "write_notion", False) and not dry:
        client = client or NotionClient()
        db_ids = db_ids or state.get("db_ids") or ensure_kb(client, args.page_id)
        ensure_kb_upgrades(client, db_ids)
        result = pipeline.update_topics_by_ids(client, db_ids, cards, topic_ids)
        marked = _mark_simulated_rows(client, db_ids, fake_topics,
                                       fake_materials)
        print(f"[repair] 03 更新 {result['updated_count']} 行；跳过 "
              f"{len(result['skipped'])} 行（不存在或缺少来源）")
        print(f"[repair] 模拟样例标记：03 {len(marked['topics'])} 行 / "
              f"01 {len(marked['materials'])} 行（已标记的不重复添加）")
        if result["skipped"]:
            print(f"[repair] 跳过 topic_id：{result['skipped']}")
    else:
        print("[repair] dry-run/未加 --write-notion：仅预览，不写 Notion。")
        for card in cards:
            print(f"  - {card['topic_id']} [{card.get('推荐级别')}] "
                  f"{card.get('拟定标题')}")

    _log_topics_round_cost(args, chat, out,
                           demand_count=len(classified),
                           card_count=len(cards))
    print(f"✅ repair 完成：本轮 {len(cards)} 张；未删除任何行；"
          "“用户选择结果”保持原值。")
    return 0


def cmd_topics_round(args, *, client=None, chat=None, profile=None,
                     now: datetime | None = None) -> int:
    """选题轮：采集→需求分类→偏好注入→LLM/启发式出卡→v3 校验→写 03/02→停 GATE-1。"""
    from src.analysis import demand as demand_engine
    from src.analysis import llm_topics, preference
    from src.analysis.topics import build_topic_cards
    from src.connectors.x_posts_source import load_x_posts

    now = now or datetime.now(timezone.utc)
    round_id = str(getattr(args, "round_id", None) or now.strftime("%Y%m%d%H%M%S"))
    dry = bool(getattr(args, "dry_run", False))
    provided_client = client is not None  # 测试注入的 mock 不触发真实写入护栏
    state = _DryState(args.state) if dry else WorkflowState(args.state)
    if not dry:
        state.load()
    if profile is None and getattr(args, "audience", None):
        profile = load_profile(args.audience)
    analyze_mode = getattr(args, "analyze", "llm")
    # dry-run 只禁止“写”，不禁止读/LLM；默认 llm 必须真正走 provider_chain。
    if chat is None and analyze_mode == "llm":
        try:
            from src.llm.provider_chain import ProviderChain
            chat = ProviderChain()
        except Exception as exc:  # noqa: BLE001 - 未配置时回退启发式
            print(f"⚠️ LLM 未配置，回退启发式：{type(exc).__name__}: {exc}")
            chat = None
    if profile is None and chat is not None:
        profile = load_profile()

    # 1) 偏好：先读 05，供需求分类/打分/出卡注入；全文落本地 state 目录。
    # dry-run 只要有 NOTION_TOKEN + 已知 db_ids，也要只读加载（绝不建库/改列）。
    prefs = {"approved": [], "rejected": [], "rules": [], "candidate_rules": []}
    db_ids = state.get("db_ids")
    if dry and not db_ids and Path(args.state).exists():
        persisted = WorkflowState(args.state).load()
        db_ids = persisted.get("db_ids")
    if client is None and os.environ.get("NOTION_TOKEN") and (not dry or db_ids):
        try:
            client = NotionClient()
        except Exception as exc:  # noqa: BLE001 - 无 token/权限时仍可本地预览
            print(f"⚠️ Notion 只读偏好不可用：{type(exc).__name__}: {exc}")
    if client is not None:
        if db_ids is None and not dry:
            db_ids = ensure_kb(client, args.page_id)
        elif db_ids is None and dry:
            # dry-run 允许只读发现已有库，绝不建库/改列。
            try:
                found = client.child_databases(args.page_id)
                db_ids = ({name: found[name] for name in
                           ("00_X关注账号名单", "01_原始素材", "02_用户需求",
                            "03_选题决策", "04_文案与版本", "05_经验与复盘",
                            "08_维护与审计") if name in found} or None)
            except Exception as exc:  # noqa: BLE001 - 只读发现失败不阻塞预览
                print(f"⚠️ dry-run 只读发现 db_ids 失败：{type(exc).__name__}: {exc}")
        if db_ids and db_ids.get("05_经验与复盘"):
            if not dry:
                ensure_kb_upgrades(client, db_ids)
            state.update(db_ids=db_ids, page_root=args.page_id)
            prefs = preference.load_recent_preferences(client, db_ids)
            print(f"[偏好] 只读加载 05：认可 {len(prefs.get('approved') or [])} 条 / "
                  f"否决 {len(prefs.get('rejected') or [])} 条 / "
                  f"正在使用规则 {len(prefs.get('rules') or [])} 条")
        elif dry:
            print("[偏好] dry-run 未找到 05 db_id：跳过只读加载（不写 Notion）")
    elif dry:
        print("[偏好] dry-run 无 NOTION_TOKEN：跳过 05 只读加载（不写 Notion）")
    preference_text = (preference.build_preference_prompt(prefs)
                       if any(prefs.get(k) for k in
                              ("approved", "rejected", "rules", "candidate_rules"))
                       else "")
    active_rules = [str(r.get("内容") or r.get("原始选题") or "").strip()
                    for r in (prefs.get("rules") or [])
                    if str(r.get("内容") or r.get("原始选题") or "").strip()]
    injection_path = _write_preference_injection(
        Path(args.state).parent, preference_text, now)
    if preference_text:
        print("[偏好] 注入前 800 字：")
        print(preference_text[:800])
    else:
        print("[偏好] 本轮无历史偏好注入")
    print(f"[偏好] 全文已保存：{injection_path}")

    # 2) 采集 + 校验/过滤（x-replies-file 用现有 x_posts_source 加载）
    x_replies = getattr(args, "x_replies_file", None)
    x_loader = ((lambda p: load_x_posts(p, replies_path=x_replies))
                if x_replies else load_x_posts)
    aihot_file = getattr(args, "aihot_file", None)
    live = bool(getattr(args, "live", True)) and not aihot_file
    out = pipeline.step_collect(
        state, aihot_file=aihot_file,
        x_following_file=getattr(args, "x_following", None),
        x_posts_file=getattr(args, "x_file", None),
        live=live,
        snapshot_dir=None if dry else "data/raw",
        sources=getattr(args, "sources", "aihot,github,official,x"),
        github_top=getattr(args, "github_top", 5),
        official_limit=getattr(args, "official_limit", 3),
        aihot_limit=getattr(args, "aihot_limit", 20),
        import_file=getattr(args, "import_file", None),
        config_path=getattr(args, "config", None),
        x_posts_loader=x_loader)
    merged, materials, classified, aggregates = _generate_round_cards(
        args, out, chat=chat, profile=profile, now=now,
        preference_text=preference_text, active_rules=active_rules)
    used_material_ids = {iid for c in merged for iid in c.get("关联素材") or []}
    offenders = [m for m in materials
                 if is_simulated_source(m.get("item_id"), m.get("source_url"))]
    # 真实 CLI 写入路径（client=None）绝不允许 fake/example 素材入 Notion；
    # 测试注入 FakeNotionClient 与 dry-run 不受影响。
    if getattr(args, "write_notion", False) and not dry and not provided_client and offenders:
        print("❌ 拒绝写入 Notion：本轮引用了模拟样例来源（fake/example.com/example.org）。"
              "请去掉 --aihot-file，使用真实 aihot 拉取或真实素材文件。")
        for m in offenders[:5]:
            print(f"  - {m.get('item_id')} ← {m.get('source_url') or '无 URL'}")
        return 1

    # 同日重跑：先查询 03 当日最大 V 序号，接续编号，避免 topic_id 冲突。
    start_index = 1
    if getattr(args, "write_notion", False) and not dry:
        client = client or NotionClient()
        db_ids = state.get("db_ids") or ensure_kb(client, args.page_id)
        ensure_kb_upgrades(client, db_ids)
        start_index = _next_topic_sequence(
            client, db_ids["03_选题决策"], now.strftime("%Y%m%d"))
    for i, c in enumerate(merged, start=start_index):
        c["topic_id"] = f"TX-{now.strftime('%Y%m%d')}-V{i:02d}"
        c["用户选择结果"] = "待用户选择"

    if not dry:
        _save_round_record(args, round_id, merged, materials, out)

    print(f"[topics-round] 采集 {len(materials)} 条（去重丢弃 "
          f"{len(out.get('dropped') or [])}）；评论 {len(classified)} 条；需求类 "
          f"{len(aggregates)} 类；合并选题卡 {len(merged)} 张")
    for c in merged:
        print(f"  - {c['topic_id']} [{c.get('选题类型')}/{c.get('推荐级别')}] "
              f"{c.get('拟定标题')}（生成引擎={c.get('生成引擎')}）")
    level_counts = Counter(c.get("推荐级别") for c in merged)
    print("[评分] 推荐级别：" + "，".join(
        f"{name}={level_counts.get(name, 0)}"
        for name in ("推荐", "备选", "观察")))
    if merged and level_counts.get("推荐", 0) == 0:
        print("[评分说明] 本轮无“推荐”：v3 九维启发式在缺少国内可用性/副业证据/"
              "可复现要素时，多个维度只能记 0/1，分数会系统性偏低；阈值（推荐≥12、"
              "备选≥9、轨道分≥3）未为出推荐而调整；LLM 分只作推荐理由中的文字参考。")

    # 5) 写 Notion（03 create_row_if_absent + 02 只增）→ 停 GATE-1
    if getattr(args, "write_notion", False) and not dry:
        w = pipeline.step_write_topics(
            client, db_ids, merged, materials, only_referenced=True,
            create_only=True)
        if w["topics_written"] == 0:
            print("❌ 03 未写入任何选题卡（0 张）：请检查 topic_id 冲突或字段缺失；"
                  "本次不视为成功。")
            _log_topics_round_cost(args, chat, out,
                                   demand_count=len(classified),
                                   card_count=len(merged))
            return 1
        d = pipeline.step_write_demands(
            client, db_ids, aggregates, round_id=round_id)
        cards_path = Path(args.state).parent / "cards.json"
        cards_path.parent.mkdir(parents=True, exist_ok=True)
        cards_path.write_text(json.dumps(merged, ensure_ascii=False, indent=2),
                              encoding="utf-8")
        state.update(db_ids=db_ids, cards_path=str(cards_path), round_id=round_id,
                     gate1={"status": "waiting", "selected": []},
                     last_analyze={"engine": "v3-dual",
                                   "demand_categories": len(aggregates),
                                   "cards": len(merged)})
        try:
            state.set_stage("waiting_gate1")
        except ValueError:
            state.update(stage="waiting_gate1")
        state.save()
        audit_event(client, db_ids, f"topics-round-{iso_now()}", "工作流",
                    "topics-round", True,
                    f"需求 {len(aggregates)} 类；选题 {w['topics_written']} 张；"
                    f"02 新增 {d['written']} 行；已停 GATE-1")
        print(f"[写入] 03 选题 {w['topics_written']} 张；02 需求 "
              f"{d['rows']} 类（新增 {d['written']} 行，已存在不覆盖）")
    elif dry:
        print("[dry-run] 未写 Notion；以上为本地预览。")

    if not dry:
        state.update(round_id=round_id)
        try:
            state.set_stage("waiting_gate1")
        except ValueError:
            state.update(stage="waiting_gate1")
        state.save()

    _log_topics_round_cost(args, chat, out, demand_count=len(classified),
                           card_count=len(merged))
    print("⏸ GATE-1（第一人工关口）：请在 Notion「03_选题决策」人工选定 1–3 张，"
          "然后运行 feedback / draft。当前停在 GATE-1，未进入文案。")
    return 0


# ---------------------------------------------------------------------- dry-state
class _DryState:
    """dry-run 用的哑状态（不落盘）。"""

    def __init__(self, path):
        self.path = Path(path)
        self.data = {"stage": "init"}

    def set_stage(self, s):
        self.data["stage"] = s

    def update(self, **kw):
        self.data.update(kw)

    def get(self, k, d=None):
        return self.data.get(k, d)

    def save(self):
        pass


def main() -> int:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--page-id", default=DEFAULT_PAGE_ID)
    common.add_argument("--state", default=DEFAULT_STATE)
    ap = argparse.ArgumentParser(prog="workflow", parents=[common])
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("collect", parents=[common], help="S1+S2+S3 → 停在 GATE-1")
    p.add_argument("--aihot-file", default="data/examples/aihot_fake_sample.json")
    p.add_argument("--x-following", default="/workspace/ai-zimeiti/inputs/x_following.json")
    p.add_argument("--x-posts", "--x-file", dest="x_posts", default=None,
                   help="X 帖子文件（本地 JSON，任务05；只读不联网）")
    p.add_argument("--sources", default="aihot,x",
                   help="逗号分隔来源：aihot,github,official,x,import（默认保持 aihot,x）")
    p.add_argument("--github-top", type=int, default=5,
                   help="GitHub 每轮取前 N 个（默认 5）")
    p.add_argument("--official-limit", type=int, default=3,
                   help="每个官方源取最近 N 条（默认 3）")
    p.add_argument("--import-file", default=None,
                   help="国内统一导入 JSON/CSV（本地文件，不联网）")
    p.add_argument("--config", default="config/sources.example.yaml",
                   help="来源配置 YAML（GitHub 主题/官方源 URL）")
    p.add_argument("--live", action="store_true", help="真实拉取 aihot（≤20 条，默认离线回放）")
    p.add_argument("--audience", default="config/audience_profile.yaml",
                   help="受众画像 YAML（评分依据，任务05）")
    p.add_argument("--analyze", default="heuristic", choices=["heuristic", "glm", "llm"],
                   help="分析引擎：heuristic 本地启发式；glm 用户自备 GLM 套餐；"
                        "llm 走 OpenCode Go→GLM→DeepSeek 回退链")
    p.add_argument("--shortlist", type=int, default=60,
                   help="glm 路径的本地预筛候选数（控成本）")
    p.add_argument("--top-k", type=int, default=8)
    p.add_argument("--dry-run", action="store_true")

    p = sub.add_parser("write-audience", parents=[common],
                       help="把受众画像写入 Notion「用户画像」页（幂等）")
    p.add_argument("--audience", default="config/audience_profile.yaml")

    p = sub.add_parser("gate1", parents=[common], help="读取 GATE-1 选择结果")
    p.add_argument("--draft", action="store_true")
    p.add_argument("--llm", default="template")

    p = sub.add_parser("draft", parents=[common],
                       help="按选题卡生成初稿（默认 chain，失败回退模板；停在 GATE-2）")
    p.add_argument("--topic", action="append", default=[],
                   help="topic_id（可重复，也可逗号分隔）；不传则读取 GATE-1 已选定卡")
    p.add_argument("--llm", default="chain",
                   choices=["chain", "template", "openai-compat"],
                   help="chain=OpenCode Go→GLM→DeepSeek；失败回退 template_client")
    p.add_argument("--no-paid", dest="no_paid", action="store_true", default=True,
                   help="默认开启：排除 DeepSeek 等付费提供方")
    p.add_argument("--paid", dest="no_paid", action="store_false",
                   help="允许付费提供方（默认不开启）")
    p.add_argument("--export-dir", default=None,
                   help="同时导出 markdown：<dir>/<topic_id>.md")

    sub.add_parser("gate2", parents=[common], help="读取 GATE-2 各稿状态")
    p = sub.add_parser("status", parents=[common], help="本地状态")
    p.add_argument("--live", action="store_true")

    p = sub.add_parser("selfcheck", parents=[common], help="连通性自检（aihot 1 条 + Notion）")
    p.add_argument("--skip-aihot", action="store_true")

    p = sub.add_parser("github", parents=[common],
                       help="只做 GitHub 研究（真实 REST，输出 JSON+表格，不写 Notion）")
    p.add_argument("--top", type=int, default=5)
    p.add_argument("--config", default="config/sources.example.yaml")
    p.add_argument("--snapshots", default=DEFAULT_SNAPSHOTS)

    p = sub.add_parser("sources-check", parents=[common],
                       help="真实小样：aihot+github+official+x+import → 校验/去重（不写 Notion）")
    p.add_argument("--github-top", type=int, default=5)
    p.add_argument("--official-limit", type=int, default=3)
    p.add_argument("--aihot-limit", type=int, default=3)
    p.add_argument("--x-file", dest="x_file", default=None)
    p.add_argument("--import-file", default=None)
    p.add_argument("--config", default="config/sources.example.yaml")
    p.add_argument("--write-notion", action="store_true",
                   help="仅新增写入 01_原始素材 小样（需 NOTION_TOKEN；已有行不改）")
    p.add_argument("--max-rows", type=int, default=10,
                   help="--write-notion 单次最多新增行数（默认 10）")
    p.add_argument("--notion-sources", default="aihot,github,official",
                   help="--write-notion 允许写入的来源（默认不含 X 与 import）")

    # ---------------------------------------------------------- 阶段3 子命令
    p = sub.add_parser("feedback", parents=[common],
                       help="记录 GATE-1 反馈到 05（不改 03）")
    p.add_argument("--topic", required=True, help="topic_id（如 TX-20261102-V01）")
    p.add_argument("--pick", required=True,
                   choices=["认可", "一般", "否决", "未选"])
    p.add_argument("--say", required=True, help="用户原话反馈")
    p.add_argument("--reason", default="", help="原因标签")
    p.add_argument("--round", type=int, default=None, help="反馈轮次")
    p.add_argument("--title", default="", help="原始选题")
    p.add_argument("--why", default="", help="推荐理由")
    p.add_argument("--next", default="", help="下轮参考")

    p = sub.add_parser("feedback-seed", parents=[common],
                       help="从本地 JSON 批量写反馈（文件放 data/state，不入库）")
    p.add_argument("--file", required=True, help="反馈 JSON（数组或 {items:[...]}）")

    p = sub.add_parser("rules", parents=[common],
                       help="候选规则：--propose 生成待确认规则；--confirm 只提示手动改状态")
    p.add_argument("--propose", action="store_true")
    p.add_argument("--confirm", default=None, help="候选规则 record_id（只提示，不改状态）")
    p.add_argument("--llm", default="template", choices=["template", "llm"])

    p = sub.add_parser("topics-round", parents=[common],
                       help="选题轮：采集→需求分类→偏好注入→出卡→写03/02→停GATE-1")
    p.add_argument("--sources", default="aihot,github,official,x")
    p.add_argument("--x-file", dest="x_file", default=None)
    p.add_argument("--x-replies-file", dest="x_replies_file", default=None,
                   help="X API 原样回复文件（data/includes/replies/meta）")
    p.add_argument("--x-following", dest="x_following", default=None)
    p.add_argument("--aihot-file", dest="aihot_file", default=None,
                   help="显式传入才回放本地 aihot 文件；不传则默认真实拉取")
    p.add_argument("--import-file", default=None)
    p.add_argument("--github-top", type=int, default=5)
    p.add_argument("--aihot-limit", type=int, default=20)
    p.add_argument("--official-limit", type=int, default=3)
    p.add_argument("--max-cards", type=int, default=10)
    p.add_argument("--round-id", default=None)
    p.add_argument("--repair-round", default=None,
                   help="修复指定轮次：读取 data/state/topics_round_<id>.json")
    p.add_argument("--repair-ids", default=None,
                   help="本地无轮次存档时显式传 topic_id 列表（逗号分隔）")
    p.add_argument("--write-notion", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--live", action=argparse.BooleanOptionalAction,
                   default=True, help="默认真实拉取 aihot；--no-live 关闭")
    p.add_argument("--config", default="config/sources.example.yaml")
    p.add_argument("--audience", default="config/audience_profile.yaml")
    p.add_argument("--shortlist", type=int, default=40,
                   help="LLM 预筛候选数（默认 ≤40，控成本；会为 aihot/github/official 保留配额）")
    p.add_argument("--analyze", default="llm", choices=["heuristic", "llm"])

    args = ap.parse_args()
    if args.cmd == "topics-round" and getattr(args, "aihot_file", None):
        # 只有显式 --aihot-file 才走本地回放，避免误把假样例用于真实运行。
        args.live = False
    if args.cmd == "collect":
        return cmd_collect(args)
    if args.cmd == "write-audience":
        return cmd_write_audience(args)
    if args.cmd == "gate1":
        args.llm = getattr(args, "llm", "template")
        return cmd_gate1(args, auto_draft=args.draft)
    if args.cmd == "draft":
        return cmd_draft(args)
    if args.cmd == "gate2":
        return cmd_gate2(args)
    if args.cmd == "status":
        return cmd_status(args)
    if args.cmd == "selfcheck":
        return cmd_selfcheck(args)
    if args.cmd == "github":
        return cmd_github(args)
    if args.cmd == "sources-check":
        return cmd_sources_check(args)
    if args.cmd == "feedback":
        return cmd_feedback(args)
    if args.cmd == "feedback-seed":
        return cmd_feedback_seed(args)
    if args.cmd == "rules":
        return cmd_rules(args)
    if args.cmd == "topics-round":
        if getattr(args, "repair_round", None) or getattr(args, "repair_ids", None):
            return cmd_topics_round_repair(args)
        return cmd_topics_round(args)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
