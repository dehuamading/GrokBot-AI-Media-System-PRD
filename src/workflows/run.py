"""任务03 工作流 CLI（手动触发，无任何定时调度）。

子命令：
  collect   S1抓取+S2分析+S3写选题卡 → 停在 GATE-1（默认离线回放假数据文件）
  gate1     S4 读取用户在 Notion 的选题选择结果（只读）；--draft 时直接续写初稿
  draft     S4+S5：读取 GATE-1 → 已选定则生成初稿写 Notion 04 → 停在 GATE-2
  gate2     S6 读取 04 库各稿状态（只读观测；无发布动作）
  status    打印本地状态；--live 附带 Notion 关口实况
  selfcheck 连通性自检：aihot 1 条真实请求 + Notion 权限/结构检查 + 1 条审计

通用：--dry-run 全程离线不写 Notion 不落状态；NOTION_TOKEN 只从环境变量读。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.analysis.audience import load_profile  # noqa: E402
from src.analysis.scoring import SCORING_VERSION  # noqa: E402
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


def make_llm(name: str) -> DraftClient:
    if name == "template":
        return TemplateClient()
    if name == "openai-compat":
        from src.llm.openai_compat import OpenAICompatClient
        return OpenAICompatClient()  # 未配置环境变量时抛 LLMNotConfigured
    raise ValueError(f"未知 LLM 引擎：{name}")


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
        live=args.live, snapshot_dir=None if args.dry_run else "data/raw")

    analyze_meta = {"engine": f"heuristic:{SCORING_VERSION}"}
    use_glm = (getattr(args, "analyze", "heuristic") == "glm" and not args.dry_run)
    if use_glm:
        from src.llm.glm import GLMChat
        chat = GLMChat()  # 需 GLM_API_BASE/GLM_API_KEY 环境变量；缺则报错退出
        short = llm_topics.shortlist(out["materials"], k=args.shortlist, profile=profile)
        glm_scores = llm_topics.glm_score_posts(chat, short, profile)
        cards, meta = llm_topics.glm_build_cards(chat, short, profile,
                                                 glm_scores=glm_scores)
        analyze_meta = {"engine": meta["engine"], "glm_calls": meta["glm_calls"],
                        "shortlist": len(short), "scored": len(glm_scores),
                        "dropped_cards": meta["dropped"]}
        details = []
        used = {iid for c in cards for iid in c["关联素材"]}
        for m in out["materials"]:
            if m["item_id"] not in used:
                continue
            gs = glm_scores.get(m["item_id"])
            if gs:
                m["screen_score"] = round(gs["score"])
                m["screen_reasons"] = [f"GLM受众评分 {gs['score']}/10：{gs['reason']}"]
            m["processing_status"] = "candidate"
        ana = {"cards": cards, "details": details}
    else:
        if getattr(args, "analyze", "heuristic") == "glm":
            print("[dry-run] glm 分析不真实调用（省 token），回退启发式展示")
        ana = pipeline.step_analyze(state if not args.dry_run else dry,
                                    out["materials"], top_k=args.top_k,
                                    profile=profile)

    print(f"[S1] 素材 {len(out['materials'])} 条（去重丢弃 {len(out['dropped'])}，"
          f"{'真实拉取' if args.live else '离线回放'}；X 帖子文件 {out['x_posts']} 条；"
          f"X 账号 {len(out['accounts'])} 个（仅文件）")
    print(f"[S2] 分析引擎：{analyze_meta['engine']}；选题卡 {len(ana['cards'])} 张"
          + (f"（GLM 调用 {analyze_meta['glm_calls']} 次，候选 {analyze_meta['shortlist']} 条）"
             if 'glm_calls' in analyze_meta else
             f"（评分版本 {SCORING_VERSION}，推荐 "
             f"{sum(1 for c in ana['cards'] if c['推荐级别'] == '推荐')} / "
             f"备选 {sum(1 for c in ana['cards'] if c['推荐级别'] == '备选')} / "
             f"观察 {sum(1 for c in ana['cards'] if c['推荐级别'] == '观察')}）"))
    if len(ana["cards"]) < 5:
        print(f"⚠️ 选题卡不足 5 张（{len(ana['cards'])}），仍按规则写入并标记 insufficient")

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
    p.add_argument("--x-posts", default=None,
                   help="X 帖子文件（本地 JSON，任务05；只读不联网）")
    p.add_argument("--live", action="store_true", help="真实拉取 aihot（≤20 条，默认离线回放）")
    p.add_argument("--audience", default="config/audience_profile.yaml",
                   help="受众画像 YAML（评分依据，任务05）")
    p.add_argument("--analyze", default="heuristic", choices=["heuristic", "glm"],
                   help="分析引擎：heuristic 本地启发式；glm 用户自备 GLM 套餐")
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
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
