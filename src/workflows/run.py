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
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.analysis.audience import load_profile  # noqa: E402
from src.analysis.scoring import (SCORING_VERSION, SCORING_VERSION_V3,  # noqa: E402
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
NO_LLM_COST_BASIS = "无LLM调用；外部API为免费接口，实际金额未知"


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
                "cost_basis": UNKNOWN}

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
    return {"calls": calls, "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens, "tokens": tokens, "cost": cost,
            "cost_basis": cost_basis}


def _log_collect_cost(args, out: dict, chat, analyze_mode: str) -> dict:
    path = Path(args.state).parent / "cost_log.jsonl"
    if chat is None:
        # 未使用 LLM：provider=无，金额 0（免费接口），不是“未知”冒充 0。
        stats = {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0,
                 "tokens": 0, "cost": 0,
                 "cost_basis": "免费接口（未使用LLM）"}
        provider, model = "无", "无"
        note = "免费接口（AIHot/GitHub/RSS/Releases）；未使用 LLM"
    else:
        stats = _aggregate_usage(chat)
        provider, model = _serving_label(chat, analyze_mode)
        note = ""
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
        stats = {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0,
                 "tokens": 0, "cost": 0,
                 "cost_basis": "免费接口（未使用LLM）"}
        provider, model = "无", "无"
    else:
        stats = _aggregate_usage(chat)
        provider = getattr(chat, "last_provider", None) or getattr(chat, "model", UNKNOWN)
        model = getattr(chat, "last_model", None) or getattr(chat, "model", UNKNOWN)
    external = out.get("external_calls") if isinstance(out.get("external_calls"), dict) else {}
    record = append_cost(path, {
        "command": f"topics-round --round-id {getattr(args, 'round_id', '') or ''}",
        "provider": provider, "model": model,
        **stats,
        "external_api_calls": external,
        "note": (f"需求分类 {demand_count} 条；合并选题卡 {card_count} 张；"
                 "cost 为接口返回 list_cost 时按套餐额度折算（非实付），未知写「未知」"),
    })
    print(f"[成本] provider={record['provider']} model={record['model']}；"
          f"调用 {record['calls']} 次，tokens={record['tokens']}，"
          f"金额={record['金额']}；成本口径={record['成本口径']}")
    return record


def _postprocess_round_cards(cards: list[dict], materials: list[dict], *,
                             now: datetime, profile, max_cards: int,
                             active_rules: list[str] | None = None) -> list[dict]:
    from src.analysis.demand import (demand_evidence_for_item,
                                     has_demand_evidence, short_evidence_texts)
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
            primary = demand_mats[0] if demand_mats else mats[0]
            demand_ev = []
            for m in demand_mats:
                demand_ev.extend(demand_evidence_for_item(m))
            v3 = score_item_v3(primary, now=now, profile=profile,
                               demand=demand_ev, active_rules=active_rules)
        else:
            primary = mats[0]
            v3 = score_item_v3(primary, now=now, profile=profile,
                               active_rules=active_rules)
        c["选题类型"] = ctype
        c["类型"] = ctype
        c["评分版本"] = SCORING_VERSION_V3
        c["轨道得分"] = v3["adjusted_track_score"]
        c["ranking_track_score"] = v3["adjusted_track_score"]
        c["ranking_total"] = v3["adjusted_total"]
        c["rule_adjustment"] = v3["rule_adjustment"]
        c["副业证据等级"] = v3["side_hustle_evidence_level"]
        c["传播潜力假设(不计分)"] = v3["propagation_hypothesis"]["text"]
        c["综合评分"] = v3["adjusted_total"]
        c["推荐级别"] = v3["level"]
        base_reason = str(c.get("推荐理由") or "").strip()
        v3_reason = (f"v3 总分 {v3['adjusted_total']}/18，轨道分 "
                     f"{v3['adjusted_track_score']}")
        if v3["rule_adjustment"]:
            v3_reason += f"（已应用正在使用规则加减分 {v3['rule_adjustment']:+d}）"
        c["推荐理由"] = (base_reason + "；" + v3_reason
                         if base_reason else v3_reason)
        c["面向用户"] = c.get("面向用户") or c.get("面向谁") or "待验证"
        c["痛点或机会"] = c.get("痛点或机会") or c.get("核心价值") or "待验证"
        c["原始来源"] = source_urls[0]
        c["为何现在做"] = c.get("为何现在做") or c.get("为什么现在") or "待验证"
        c["国内可用"] = c.get("国内可用") or c.get("国内可用性") or "待验证"
        c["是否付费"] = c.get("是否付费") or "待验证"
        c["能否实际验证"] = c.get("能否实际验证") or "待验证"
        c["切入角度"] = c.get("切入角度") or c.get("切入角度与钩子") or "待验证"
        c["主要风险"] = c.get("主要风险") or c.get("可信度与风险") or "待验证"
        c["引用帖子"] = "；".join(source_urls)  # X 卡只放 url，不写用户名/正文全文
        # X 来源卡：隐去作者名/用户名（引用帖子字段仍只放 url）
        if any(m.get("platform") == "x" or m.get("source_type") == "x_posts"
               for m in mats):
            from src.analysis.demand import sanitize_evidence
            names: set[str] = set()
            for m in mats:
                if m.get("platform") != "x" and m.get("source_type") != "x_posts":
                    continue
                author = str(m.get("author_or_publisher") or "").strip()
                if author:
                    names.add(author)
                username = str((m.get("extra") or {}).get("author_username") or "")
                if username:
                    names.update({username, f"@{username}"})
            for key in ("拟定标题", "面向用户", "痛点或机会", "为何现在做",
                        "国内可用", "是否付费", "能否实际验证", "切入角度",
                        "主要风险", "推荐理由", "核心价值"):
                value = c.get(key)
                if not isinstance(value, str):
                    continue
                for name in names:
                    if name:
                        value = value.replace(name, "（已隐去）")
                if key == "拟定标题":
                    value = sanitize_evidence(value, limit=60)
                c[key] = value
            # 若标题直接照搬帖子正文（常见于启发式路径），换成需求概括，避免全文入库。
            raw_texts = [str(m.get("title_or_summary") or "").strip()
                         for m in mats
                         if (m.get("platform") == "x"
                             or m.get("source_type") == "x_posts")
                         and len(str(m.get("title_or_summary") or "").strip()) >= 20]
            title = str(c.get("拟定标题") or "")
            if any(raw and (raw in title or title in raw) for raw in raw_texts):
                tags: list[str] = []
                for m in mats:
                    for tag in m.get("demand_tags") or []:
                        if tag not in tags:
                            tags.append(tag)
                c["拟定标题"] = ("X 用户需求：" + "、".join(tags[:2])
                                 if tags else "X 热点观察（标题已概括）")
            # 其他字段若照搬帖子正文，同样替换为概括（评论证据短摘录不受影响）。
            for key in ("核心价值", "痛点或机会", "切入角度", "推荐理由"):
                value = str(c.get(key) or "")
                if any(raw and (raw in value or (len(value) >= 20 and value in raw))
                       for raw in raw_texts):
                    c[key] = "基于 X 用户需求的概括（帖子正文未入库）"
        c["_source_urls"] = source_urls
        c["_v3"] = v3
        final.append(c)
    return merge_dual_tracks(final, top_n=max_cards)


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
    state = _DryState(args.state) if dry else WorkflowState(args.state)
    if not dry:
        state.load()
    if profile is None and getattr(args, "audience", None):
        profile = load_profile(args.audience)
    if chat is None and not dry and getattr(args, "analyze", "llm") == "llm":
        try:
            from src.llm.provider_chain import ProviderChain
            chat = ProviderChain()
        except Exception as exc:  # noqa: BLE001 - 未配置时回退启发式
            print(f"⚠️ LLM 未配置，回退启发式：{type(exc).__name__}: {exc}")
    if profile is None and chat is not None:
        profile = load_profile()

    # 1) 偏好：先读 05，供需求分类/打分/出卡注入；全文落本地 state 目录。
    prefs = {"approved": [], "rejected": [], "rules": [], "candidate_rules": []}
    if client is not None:
        db_ids = state.get("db_ids") or ensure_kb(client, args.page_id)
        ensure_kb_upgrades(client, db_ids)
        state.update(db_ids=db_ids, page_root=args.page_id)
        prefs = preference.load_recent_preferences(client, db_ids)
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
    out = pipeline.step_collect(
        state, aihot_file=getattr(args, "aihot_file", None),
        x_following_file=getattr(args, "x_following", None),
        x_posts_file=getattr(args, "x_file", None),
        live=getattr(args, "live", False),
        snapshot_dir=None if dry else "data/raw",
        sources=getattr(args, "sources", "aihot,github,official,x"),
        github_top=getattr(args, "github_top", 5),
        official_limit=getattr(args, "official_limit", 3),
        aihot_limit=getattr(args, "aihot_limit", 20),
        import_file=getattr(args, "import_file", None),
        config_path=getattr(args, "config", None),
        x_posts_loader=x_loader)
    materials = pipeline.eligible_materials(out["materials"])

    # 3) 需求分类（LLM 失败回退关键词）
    flat = _flatten_demand_comments(materials)
    classified = demand_engine.classify_demands(
        flat, chat=chat, preference_text=preference_text)
    _attach_demand_results(materials, classified)
    aggregates = demand_engine.aggregate_demands(classified)

    # 4) shortlist + 打分/出卡（需求池与机会池分别生成）
    max_cards = max(1, min(10, int(getattr(args, "max_cards", 10))))
    short = llm_topics.shortlist(
        materials, k=int(getattr(args, "shortlist", 60)), profile=profile)
    seen = {m["item_id"] for m in short}
    for m in materials:
        if m["item_id"] not in seen and demand_engine.has_demand_evidence(m):
            short.append(m)
            seen.add(m["item_id"])

    cards: list[dict] = []
    if chat is not None:
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
        for pool, ctype in ((demand_pool, "需求型"), (opp_pool, "机会型")):
            if not pool:
                continue
            try:
                cs, _meta = llm_topics.glm_build_cards(
                    chat, pool, profile, now=now, glm_scores=glm_scores,
                    min_cards=1, max_cards=max_cards,
                    preference_text=preference_text, card_type=ctype)
            except Exception as exc:  # noqa: BLE001 - 单轨失败不拖垮另一轨
                print(f"⚠️ {ctype} LLM 出卡失败，稍后用启发式补："
                      f"{type(exc).__name__}: {exc}")
                continue
            for c in cs:
                c["_intended_type"] = ctype
            cards.extend(cs)
    if not cards:
        heuristic, _details = build_topic_cards(
            short, top_k=max_cards, now=now, profile=profile)
        cards.extend(heuristic)

    merged = _postprocess_round_cards(
        cards, materials, now=now, profile=profile, max_cards=max_cards,
        active_rules=active_rules)
    if not merged:
        heuristic, _details = build_topic_cards(
            short, top_k=max_cards, now=now, profile=profile)
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
                extra_cards, _details = build_topic_cards(
                    pool, top_k=max_cards, now=now, profile=profile)
                extra = _postprocess_round_cards(
                    extra_cards, materials, now=now, profile=profile,
                    max_cards=max_cards, active_rules=active_rules)
                merged = merge_dual_tracks(merged + extra, top_n=max_cards)
    for i, c in enumerate(merged, start=1):
        c["topic_id"] = f"TX-{now.strftime('%Y%m%d')}-V{i:02d}"
        c["用户选择结果"] = "待用户选择"

    print(f"[topics-round] 采集 {len(materials)} 条（去重丢弃 "
          f"{len(out.get('dropped') or [])}）；评论 {len(flat)} 条；需求类 "
          f"{len(aggregates)} 类；合并选题卡 {len(merged)} 张")
    for c in merged:
        print(f"  - {c['topic_id']} [{c.get('选题类型')}/{c.get('推荐级别')}] "
              f"{c.get('拟定标题')}")

    # 5) 写 Notion（03 create_row_if_absent + 02 只增）→ 停 GATE-1
    if getattr(args, "write_notion", False) and not dry:
        client = client or NotionClient()
        db_ids = state.get("db_ids") or ensure_kb(client, args.page_id)
        ensure_kb_upgrades(client, db_ids)
        w = pipeline.step_write_topics(
            client, db_ids, merged, materials, only_referenced=True,
            create_only=True)
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

    p = sub.add_parser("draft", parents=[common], help="GATE-1 通过后生成初稿（停在 GATE-2）")
    p.add_argument("--llm", default="template", choices=["template", "openai-compat"])

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
    p.add_argument("--pick", required=True, choices=["认可", "一般", "否决"])
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
    p.add_argument("--aihot-file", dest="aihot_file",
                   default="data/examples/aihot_fake_sample.json")
    p.add_argument("--import-file", default=None)
    p.add_argument("--github-top", type=int, default=5)
    p.add_argument("--aihot-limit", type=int, default=20)
    p.add_argument("--official-limit", type=int, default=3)
    p.add_argument("--max-cards", type=int, default=10)
    p.add_argument("--round-id", default=None)
    p.add_argument("--write-notion", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--live", action="store_true")
    p.add_argument("--config", default="config/sources.example.yaml")
    p.add_argument("--audience", default="config/audience_profile.yaml")
    p.add_argument("--shortlist", type=int, default=60)
    p.add_argument("--analyze", default="llm", choices=["heuristic", "llm"])

    args = ap.parse_args()
    if args.cmd == "collect":
        return cmd_collect(args)
    if args.cmd == "write-audience":
        return cmd_write_audience(args)
    if args.cmd == "gate1":
        args.llm = getattr(args, "llm", "template")
        return cmd_gate1(args, auto_draft=args.draft)
    if args.cmd == "draft":
        return cmd_gate1(args, auto_draft=True)
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
        return cmd_topics_round(args)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
