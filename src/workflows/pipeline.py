"""工作流编排器（任务03）：collect → analyze → write_topics → [GATE-1]
→ read gate1 → draft → [GATE-2] → gate2 观测。

每一步都是显式函数，依赖（NotionClient / 连接器 / LLMClient）从外部注入，
单元测试用 FakeNotionClient / 假文件 / 模板引擎，全程离线。
"""

from __future__ import annotations

import json
from pathlib import Path

from src.analysis.classify import classify_item
from src.analysis.scoring import SCORING_VERSION
from src.analysis.topics import build_topic_cards
from src.connectors.aihot_source import fetch_selected, load_from_file
from src.connectors.github_source import DEFAULT_SNAPSHOTS, fetch_github
from src.connectors.github_source import load_config as load_github_config
from src.connectors.import_source import load_import_file
from src.connectors.official_source import fetch_official
from src.connectors.x_following_source import load_following
from src.connectors.x_posts_source import load_x_posts
from src.llm.base import DraftClient, platforms_of
from src.normalize import iso_now, screen_score, validate_item
from src.notion_store.gates import (read_gate1_selections, read_gate2_status,
                                    validate_gate1, write_draft_row)
from src.validate import dedupe as validate_dedupe
from src.validate import validate_item as validate_source_item

DEFAULT_SOURCES = ["aihot", "x"]
KNOWN_SOURCES = {"aihot", "github", "official", "x", "import"}


# ---------------------------------------------------------------------- S1
def _selected_sources(sources) -> list[str]:
    if sources is None:
        return list(DEFAULT_SOURCES)
    if isinstance(sources, str):
        sources = [s.strip() for s in sources.split(",") if s.strip()]
    selected = []
    for name in sources:
        name = str(name).strip().lower()
        if name and name not in selected:
            selected.append(name)
    unknown = set(selected) - KNOWN_SOURCES
    if unknown:
        raise ValueError(f"未知来源：{sorted(unknown)}（允许 {sorted(KNOWN_SOURCES)}）")
    return selected


def step_collect(state, *, aihot_file: str | Path | None = None,
                 x_following_file: str | Path | None = None,
                 x_posts_file: str | Path | None = None,
                 live: bool = False, snapshot_dir: str | Path = "data/raw",
                 sources=None, github_top: int = 5, official_limit: int = 3,
                 aihot_limit: int = 20,
                 import_file: str | Path | None = None,
                 config_path: str | Path | None = None,
                 github_snapshots_path: str | Path = DEFAULT_SNAPSHOTS,
                 import_errors_path: str | Path = "data/state/import_errors.jsonl",
                 fetcher=fetch_selected, loader=load_from_file,
                 x_posts_loader=load_x_posts, following_loader=load_following,
                 github_fetcher=fetch_github,
                 official_fetcher=fetch_official,
                 import_loader=load_import_file) -> dict:
    """S1 数据抓取（默认来源保持 aihot+x，行为不变）。

    --sources 可增选 github/official/import；所有采集仍由手动命令触发。
    """
    selected = _selected_sources(sources)
    materials: list[dict] = []
    per_source: dict[str, int] = {name: 0 for name in selected}
    source_errors: dict[str, list] = {}
    external = {"github": 0, "aihot": 0, "official": 0, "x": 0, "import": 0}
    x_posts: list[dict] = []
    accounts: list[dict] = []

    if "aihot" in selected:
        aihot_items = (fetcher(limit=aihot_limit) if live
                       else (loader(aihot_file) if aihot_file else []))
        materials.extend(aihot_items)
        per_source["aihot"] = len(aihot_items)
        external["aihot"] = 1 if live else 0

    if "x" in selected:
        x_posts = x_posts_loader(x_posts_file) if x_posts_file else []
        materials.extend(x_posts)
        per_source["x"] = len(x_posts)
        accounts = following_loader(x_following_file) if x_following_file else []

    if "github" in selected:
        gh_config = load_github_config(config_path)
        try:
            gh = github_fetcher(top=github_top, themes=gh_config["themes"],
                                since_days=gh_config["since_days"],
                                snapshots_path=github_snapshots_path)
        except Exception as exc:  # noqa: BLE001 - 单源失败不拖垮其他来源
            gh = {"items": [], "api_calls": 0, "errors": [
                f"GitHub 连接器异常：{type(exc).__name__}: {exc}"]}
        materials.extend(gh.get("items") or [])
        per_source["github"] = len(gh.get("items") or [])
        external["github"] = gh.get("api_calls", 0)
        if gh.get("errors"):
            source_errors["github"] = list(gh["errors"])

    if "official" in selected:
        try:
            off = official_fetcher(config_path=config_path,
                                   default_limit=official_limit)
        except Exception as exc:  # noqa: BLE001
            off = {"items": [], "api_calls": 0, "errors": [
                {"source": "官方源", "error": f"{type(exc).__name__}: {exc}"}]}
        materials.extend(off.get("items") or [])
        per_source["official"] = len(off.get("items") or [])
        external["official"] = off.get("api_calls", 0)
        if off.get("errors"):
            source_errors["official"] = list(off["errors"])

    if "import" in selected and import_file:
        imported = import_loader(import_file, errors_path=import_errors_path)
        materials.extend(imported.get("items") or [])
        per_source["import"] = len(imported.get("items") or [])
        if imported.get("errors"):
            source_errors["import"] = list(imported["errors"])

    validation_error_counts: dict[str, int] = {}
    for it in materials:
        if it.get("source_type") in {"aihot", "x_posts"}:
            errs = validate_item(it)
            if errs:
                raise ValueError(f"素材校验失败 {it['item_id']}: {errs}")
        errs = validate_source_item(it)
        if errs:
            stype = str(it.get("source_type") or "unknown")
            validation_error_counts[stype] = validation_error_counts.get(stype, 0) + 1

    materials, dropped = validate_dedupe(materials)

    snapshot = None
    if snapshot_dir:
        snap_dir = Path(snapshot_dir) / f"run_{iso_now().replace(':', '').replace('+', '_')}"
        snap_dir.mkdir(parents=True, exist_ok=True)
        snapshot = str(snap_dir / "materials.json")
        Path(snapshot).write_text(json.dumps(materials, ensure_ascii=False, indent=2),
                                  encoding="utf-8")

    state.update(last_collect={"materials": len(materials), "dropped": len(dropped),
                               "accounts": len(accounts), "live": live,
                               "x_posts": len(x_posts), "per_source": per_source,
                               "snapshot": snapshot, "run_id": iso_now()})
    state.set_stage("collected")
    state.save()
    return {"materials": materials, "accounts": accounts, "dropped": dropped,
            "snapshot": snapshot, "x_posts": len(x_posts),
            "per_source": per_source, "validation_error_counts": validation_error_counts,
            "source_errors": source_errors, "external_calls": external,
            "sources": selected}


# ---------------------------------------------------------------------- S2
def step_analyze(state, materials: list[dict], top_k: int = 8,
                 profile=None) -> dict:
    """S2 分析：去重(已在前置)→分类→评分→选题卡。纯本地（启发式；GLM 路径见 run.py）。"""
    cards, details = build_topic_cards(materials, top_k=top_k, profile=profile)
    for d in details:
        d["item"]["content_type"] = d["content_type"]
        sc = d["score"]
        d["item"]["screen_score"] = min(10, round(sc["total"] * 10 / 14))
        d["item"]["screen_reasons"] = [f"{x['name']}{x['grade']}:{x['reason']}"
                                       for x in sc["dimensions"]]
        d["item"]["processing_status"] = "screened"
    used = {iid for c in cards for iid in c["关联素材"]}
    for d in details:
        if d["item"]["item_id"] in used:
            d["item"]["processing_status"] = "candidate"

    state.update(last_analyze={"cards": len(cards), "scoring_version": SCORING_VERSION,
                               "insufficient": len(cards) < 5})
    state.set_stage("waiting_gate1")
    state.save()
    return {"cards": cards, "details": details}


# ---------------------------------------------------------------------- S3
def step_write_topics(client, db_ids, cards: list[dict],
                      materials: list[dict], page_ids: dict[str, str] | None = None,
                      only_referenced: bool = True) -> dict:
    """S3 写入 Notion：01 更新分数/状态；03 upsert 选题卡（待用户选择）。

    only_referenced=True（任务05）：01 只写被选题卡引用到的素材，
    避免把上千条原始帖子写进 Notion。
    """
    from src.notion_store.client import (date_prop, number_prop, rt,
                                         select_prop, url_prop)
    page_ids = page_ids or {}
    if only_referenced:
        used_ids = {iid for c in cards for iid in c["关联素材"]}
        materials = [m for m in materials if m["item_id"] in used_ids]
    n_mat = 0
    for it in materials:
        extra = it.get("extra") or {}
        client.upsert_row(db_ids["01_原始素材"], "item_id", it["item_id"], {
            "source_type": select_prop(it["source_type"]),
            "source_url": url_prop(it["source_url"]),
            "author_or_publisher": rt(it["author_or_publisher"]),
            "published_at": date_prop(it["published_at"]),
            "collected_at": date_prop(it["collected_at"]),
            "title_or_summary": rt(it["title_or_summary"]),
            "claims": rt(" | ".join(c for c in it.get("claims") or [] if c)),
            "evidence_status": select_prop(it["evidence_status"]),
            "processing_status": select_prop(it["processing_status"]),
            "screen_score": number_prop(it.get("screen_score")),
            "screen_reasons": rt("; ".join(it.get("screen_reasons") or [])[:1800]),
            "aihot_score": number_prop(extra.get("aihot_score")),
            "category": rt(str(extra.get("category") or "")),
            "aihot_url": url_prop(extra.get("aihot_url")),
        })
        n_mat += 1

    n_top = 0
    for c in cards:
        linked = "、".join(f"{iid}" for iid in c["关联素材"])
        client.upsert_row(db_ids["03_选题决策"], "topic_id", c["topic_id"], {
            "拟定标题": rt(c["拟定标题"]), "建议平台": select_prop(c["建议平台"]),
            "面向谁": rt(c["面向谁"]), "核心价值": rt(c["核心价值"]),
            "为什么现在": rt(c["为什么现在"]), "关联素材": rt(linked),
            "需求证据": rt(c["需求证据"]), "可信度与风险": rt(c["可信度与风险"]),
            "最强反证": rt(c["最强反证"]), "差异与难度": rt(c["差异与难度"]),
            "推荐级别": select_prop(c["推荐级别"]), "推荐理由": rt(c["推荐理由"]),
            "用户选择结果": select_prop("待用户选择"),
            # 任务05 新增列（只增列升级，不存在时自动补）：
            "为什么适合": rt(c.get("为什么适合")),
            "切入角度与钩子": rt(c.get("切入角度与钩子")),
            "国内可用性": rt(c.get("国内可用性")),
            "引用帖子": rt(c.get("引用帖子")),
            "综合评分": number_prop(c.get("综合评分")),
        })
        n_top += 1
    return {"materials_written": n_mat, "topics_written": n_top,
            "materials_total": len(materials)}


# ---------------------------------------------------------------------- S4
def step_gate1(client, db_ids) -> list[dict]:
    """S4 读取 GATE-1 审核结果（Notion 实时查询，唯一事实源）。"""
    selections = read_gate1_selections(client, db_ids)
    if selections:
        validate_gate1(selections)
    return selections


# ---------------------------------------------------------------------- S5
def step_draft(client, db_ids, selections: list[dict], materials: list[dict],
               llm: DraftClient) -> dict:
    """S5 写文案：每题×每平台恰好 1 份初稿 → Notion 04（状态 V1，待审）。

    selections 为 read_gate1_selections 返回的完整选题卡（Notion 事实源）；
    materials 为素材池快照，按 关联素材 的 item_id 匹配事实。
    红线：本步骤之后没有任何自动动作；DraftClient 只产文本，无发布能力。
    """
    mat_by_id = {m["item_id"]: m for m in materials}
    drafts_meta = []
    for card in selections:
        rel_mats = [mat_by_id[i] for i in card.get("关联素材") or [] if i in mat_by_id]
        if (card.get("关联素材") and len(rel_mats) < len(card["关联素材"])):
            missing = set(card["关联素材"]) - set(mat_by_id)
            print(f"⚠️ {card['topic_id']} 有关联素材不在本地快照：{missing}（初稿事实清单会相应缺失）")
        for platform in card.get("platforms") or platforms_of(card):
            draft = llm.generate(platform, card, rel_mats)
            draft.draft_id = f"D-{card['topic_id']}-{platform}-v{draft.version}"
            page_id, created = write_draft_row(client, db_ids, draft, card["topic_id"])
            drafts_meta.append({"draft_id": draft.draft_id, "topic_id": card["topic_id"],
                                "platform": platform, "notion_page": page_id,
                                "created": created, "engine": draft.engine})
    return {"drafts": drafts_meta,
            "note": "已停在 GATE-2：请在 Notion 04_文案与版本 审阅（已确认/修订中）；"
                    "系统无发布能力，绝不自动发布"}


# ---------------------------------------------------------------------- S6
def step_gate2(client, db_ids) -> dict:
    """S6 GATE-2 观测（只读）。修订循环与发布属 P4/P5，且发布永远人工。"""
    return read_gate2_status(client, db_ids)
