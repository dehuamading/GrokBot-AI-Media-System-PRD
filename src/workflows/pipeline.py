"""工作流编排器（任务03）：collect → analyze → write_topics → [GATE-1]
→ read gate1 → draft → [GATE-2] → gate2 观测。

每一步都是显式函数，依赖（NotionClient / 连接器 / LLMClient）从外部注入，
单元测试用 FakeNotionClient / 假文件 / 模板引擎，全程离线。
"""

from __future__ import annotations

import json
import re
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
        if live:
            try:
                aihot_items = fetcher(limit=aihot_limit)
            except Exception as exc:  # noqa: BLE001 - 单源失败不拖垮其他来源
                aihot_items = []
                source_errors["aihot"] = [
                    f"aihot 拉取失败：{type(exc).__name__}: {exc}"]
        else:
            aihot_items = loader(aihot_file) if aihot_file else []
        materials.extend(aihot_items)
        per_source["aihot"] = len(aihot_items)
        external["aihot"] = 1 if live else 0

    if "x" in selected:
        x_posts = x_posts_loader(x_posts_file) if x_posts_file else []
        materials.extend(x_posts)
        per_source["x"] = len(x_posts)
        if getattr(x_posts, "errors", None):
            source_errors["x"] = list(x_posts.errors)
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

    if "import" in selected and not import_file:
        print("⚠️ --sources 含 import 但未提供 --import-file：已跳过 import")
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
def eligible_materials(materials: list[dict]) -> list[dict]:
    """校验失败的素材不得进入选题分析（未校验过的素材默认保留）。"""
    return [m for m in materials if m.get("eligible_for_topic") is not False]


def step_analyze(state, materials: list[dict], top_k: int = 8,
                 profile=None) -> dict:
    """S2 分析：去重(已在前置)→分类→评分→选题卡。纯本地（启发式；GLM 路径见 run.py）。"""
    materials = eligible_materials(materials)
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
def _safe_x_summary(it: dict, override=None) -> str:
    """01 中 X 素材只写 LLM/规则概括：≤40 字，且不含 @handle/用户名。"""
    from src.analysis.privacy import (keyword_summary, scrub_identifiers,
                                      x_identifiers)
    text = str(override or "").strip()
    if not text or text in ("待验证", "未知", "待核实"):
        text = keyword_summary(it)
    text = scrub_identifiers(text, x_identifiers([it]))
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > 40:
        text = text[:40].rstrip()
    return text or "X帖子｜AI行业动态"


def _material_notion_properties(it: dict, *, extended: bool = False,
                                summary_override: str | None = None) -> dict:
    from src.notion_store.client import (date_prop, number_prop, rt,
                                         select_prop, url_prop)
    extra = it.get("extra") or {}
    title_or_summary = it["title_or_summary"]
    if it.get("source_type") == "x_posts":
        title_or_summary = _safe_x_summary(it, summary_override)
    props = {
        "source_type": select_prop(it["source_type"]),
        "source_url": url_prop(it["source_url"]),
        "author_or_publisher": rt(it["author_or_publisher"]),
        "published_at": date_prop(it["published_at"]),
        "collected_at": date_prop(it["collected_at"]),
        "title_or_summary": rt(title_or_summary),
        "claims": rt(" | ".join(c for c in it.get("claims") or [] if c)),
        "evidence_status": select_prop(it["evidence_status"]),
        "processing_status": select_prop(it["processing_status"]),
        "screen_score": number_prop(it.get("screen_score")),
        "screen_reasons": rt("; ".join(it.get("screen_reasons") or [])[:1800]),
        "aihot_score": number_prop(extra.get("aihot_score")),
        "category": rt(str(extra.get("category") or "")),
        "aihot_url": url_prop(extra.get("aihot_url")),
    }
    if extended:
        # 仅在 01 已存在这些列时才会写入（调用方按 schema 过滤，不建新列）。
        props.update({
            "platform": select_prop(it.get("platform")),
            "license_status": rt(str(it.get("license_status") or "")),
            "tool_or_method": rt(" | ".join(it.get("tool_or_method") or [])),
            "keywords": rt(" | ".join(it.get("keywords") or [])),
        })
    return props


def _notion_source_types(sources) -> set[str] | None:
    if sources is None:
        return None
    if isinstance(sources, str):
        sources = [s.strip() for s in sources.split(",") if s.strip()]
    mapping = {"x": "x_posts"}
    return {mapping.get(str(s).strip().lower(), str(s).strip().lower())
            for s in sources}


def step_write_raw_materials_if_absent(client, db_ids, materials: list[dict], *,
                                       max_rows: int = 10,
                                       sources=None) -> dict:
    """01 小样写入：只新增缺失行，绝不 update/delete 已存在行。"""
    db_id = db_ids["01_原始素材"]
    before_count = len(client.query_database(db_id))
    schema = set((client.get_database(db_id).get("properties") or {}).keys())
    allowed = _notion_source_types(sources)
    written: list[dict] = []
    skipped: list[str] = []
    for it in materials:
        if len(written) >= max(0, int(max_rows)):
            break
        if allowed is not None and it.get("source_type") not in allowed:
            continue
        props = _material_notion_properties(it, extended=True)
        props = {k: v for k, v in props.items() if k in schema}
        _, created = client.create_row_if_absent(
            db_id, "item_id", it["item_id"], props)
        if created:
            written.append({"item_id": it["item_id"],
                            "title": it.get("title_or_summary") or ""})
        else:
            skipped.append(it["item_id"])
    after_count = len(client.query_database(db_id))
    return {"before_count": before_count, "after_count": after_count,
            "written": written, "skipped_existing": len(skipped),
            "skipped_existing_items": skipped}


def _pending(value) -> str:
    text = str(value or "").strip()
    return text if text and text not in ("未知", "待核实", "待确认") else "待验证"


def step_write_topics(client, db_ids, cards: list[dict],
                      materials: list[dict], page_ids: dict[str, str] | None = None,
                      only_referenced: bool = True,
                      create_only: bool = False) -> dict:
    """S3 写入 Notion：01 更新分数/状态；03 upsert 选题卡（待用户选择）。

    only_referenced=True（任务05）：01 只写被选题卡引用到的素材，
    避免把上千条原始帖子写进 Notion。
    """
    from src.analysis.privacy import sanitize_card
    from src.notion_store.client import (number_prop, rt, select_prop,
                                         url_prop)
    page_ids = page_ids or {}
    if only_referenced:
        used_ids = {iid for c in cards for iid in c["关联素材"]}
        materials = [m for m in materials if m["item_id"] in used_ids]
    # 防线：无论从 collect 还是 topics-round 进入，写 03 前都按引用素材做 X 隐私清洗。
    mat_by_id = {m["item_id"]: m for m in materials}
    for card in cards:
        cited = [mat_by_id[iid] for iid in card.get("关联素材") or []
                 if iid in mat_by_id]
        if cited:
            sanitize_card(card, cited)
    summary_by_item: dict[str, str] = {}
    for card in cards:
        for iid in card.get("关联素材") or []:
            summary_by_item.setdefault(iid, card.get("拟定标题") or "")
    n_mat = 0
    for it in materials:
        client.upsert_row(db_ids["01_原始素材"], "item_id", it["item_id"],
                          _material_notion_properties(
                              it, summary_override=summary_by_item.get(
                                  it["item_id"])))
        n_mat += 1

    db_id = db_ids["03_选题决策"]
    schema = set((client.get_database(db_id).get("properties") or {}).keys())
    n_top = 0
    for c in cards:
        props = _topic_card_properties(c, schema, include_user=True)
        if not props:
            continue
        writer = client.create_row_if_absent if create_only else client.upsert_row
        writer(db_id, "topic_id", c["topic_id"], props)
        n_top += 1
    return {"materials_written": n_mat, "topics_written": n_top,
            "materials_total": len(materials)}


def _topic_card_properties(card: dict, schema: set[str], *,
                           include_user: bool = False) -> dict:
    """组装 03 属性；repair 时 include_user=False，绝不覆盖用户选择结果。"""
    from src.notion_store.client import (number_prop, rt, select_prop,
                                         url_prop)
    # 原始来源必须是素材 source_url；缺失则不出卡（不编造来源）。
    source_url = (card.get("原始来源")
                  or ((card.get("_source_urls") or [None])[0]))
    if not source_url:
        return {}
    linked = "、".join(f"{iid}" for iid in card["关联素材"])
    props = {
        "拟定标题": rt(card["拟定标题"]), "建议平台": select_prop(card["建议平台"]),
        "面向谁": rt(card["面向谁"]), "核心价值": rt(card["核心价值"]),
        "为什么现在": rt(card["为什么现在"]), "关联素材": rt(linked),
        "需求证据": rt(card["需求证据"]), "可信度与风险": rt(card["可信度与风险"]),
        "最强反证": rt(card["最强反证"]), "差异与难度": rt(card["差异与难度"]),
        "推荐级别": select_prop(card["推荐级别"]), "推荐理由": rt(card["推荐理由"]),
        # 任务05 新增列（只增列升级，不存在时自动补）：
        "为什么适合": rt(card.get("为什么适合")),
        "切入角度与钩子": rt(card.get("切入角度与钩子")),
        "国内可用性": rt(card.get("国内可用性")),
        "引用帖子": rt(card.get("引用帖子")),
        "综合评分": number_prop(card.get("综合评分")),
        # 阶段3 新字段（列不存在时跳过该属性）
        "选题类型": rt(_pending(card.get("选题类型") or card.get("类型"))),
        "面向用户": rt(_pending(card.get("面向用户"))),
        "痛点或机会": rt(_pending(card.get("痛点或机会"))),
        "原始来源": url_prop(source_url),
        "为何现在做": rt(_pending(card.get("为何现在做"))),
        "国内可用": rt(_pending(card.get("国内可用"))),
        "是否付费": rt(_pending(card.get("是否付费"))),
        "能否实际验证": rt(_pending(card.get("能否实际验证"))),
        "切入角度": rt(_pending(card.get("切入角度"))),
        "主要风险": rt(_pending(card.get("主要风险"))),
        "评分版本": rt(_pending(card.get("评分版本"))),
        "轨道得分": number_prop(card.get("轨道得分")),
        "副业证据等级": rt(_pending(card.get("副业证据等级"))),
        "传播潜力假设(不计分)": rt(_pending(
            card.get("传播潜力假设(不计分)") or card.get("传播潜力假设"))),
        "生成引擎": rt(card.get("生成引擎") or "启发式"),
    }
    if include_user:
        props["用户选择结果"] = select_prop("待用户选择")
    return {k: v for k, v in props.items() if k in schema}


def _rich_or_title_text(prop: dict | None) -> str:
    prop = prop or {}
    for key in ("rich_text", "title"):
        if prop.get(key):
            return "".join(x.get("text", {}).get("content", "")
                           for x in prop[key])
    return ""


def update_topics_by_ids(client, db_ids, cards: list[dict],
                         allowed_ids: set[str] | list[str]) -> dict:
    """repair 专用：只更新 allowed_ids 中已存在的 03 行，不新建/不删除。"""
    allowed = set(allowed_ids)
    db_id = db_ids["03_选题决策"]
    schema = set((client.get_database(db_id).get("properties") or {}).keys())
    updated, skipped = [], []
    for card in cards:
        topic_id = card.get("topic_id")
        if topic_id not in allowed:
            continue
        props = _topic_card_properties(card, schema, include_user=False)
        if not props:
            skipped.append(topic_id)
            continue
        row_id = client.find_row_by_title(db_id, "topic_id", topic_id)
        if not row_id:
            skipped.append(topic_id)
            continue
        client.update_page(row_id, props)
        updated.append(topic_id)
    return {"updated": updated, "skipped": skipped,
            "updated_count": len(updated)}


def prefix_notion_title(client, db_ids, db_name: str, key_prop: str,
                        key_value: str, value_prop: str, marker: str) -> bool:
    """仅在目标行标题前加一次标记；不改其他字段、不新建行。"""
    from src.notion_store.client import rt
    db_id = db_ids[db_name]
    row_id = client.find_row_by_title(db_id, key_prop, key_value)
    if not row_id:
        return False
    current = ""
    for row in client.query_database(db_id):
        if row.get("id") == row_id:
            current = _rich_or_title_text(row.get("properties", {}).get(value_prop))
            break
    if current.startswith(marker):
        return False
    client.update_page(row_id, {value_prop: rt(marker + current)})
    return True


def step_write_demands(client, db_ids, aggregates, *,
                       round_id: str | None = None,
                       max_evidence: int = 3) -> dict:
    """把需求聚合只增写入 02_用户需求（每类一行，计数=真实评论条数）。"""
    from src.notion_store.client import rt, select_prop, url_prop
    db_id = db_ids.get("02_用户需求")
    if not db_id:
        return {"rows": 0, "written": 0, "categories": []}
    schema = set((client.get_database(db_id).get("properties") or {}).keys())
    stamp = round_id or iso_now().replace(":", "").replace("+", "_")
    written: list[dict] = []
    for rec in aggregates:
        category = rec.get("category")
        count = int(rec.get("count") or 0)
        if not category or count <= 0:
            continue
        evidence = list(rec.get("evidence_short") or rec.get("evidence") or [])
        evidence = evidence[:max(0, max_evidence)]
        short = "；".join(f"「{e.get('text', '')}」" for e in evidence) or "（无）"
        problem_id = f"DEMAND-{stamp}-{category}"
        props = {
            "user_type": select_prop("其他"),
            "problem": rt(f"{category}｜实际评论 {count} 条（仅真实评论计数，不估算）"),
            "evidence_url": url_prop(rec.get("source_urls", [None])[0]
                                     if rec.get("source_urls") else None),
            "sample_limit": rt(f"计数={count}；脱敏短证据（≤{max_evidence}条）：{short}"),
            "status": select_prop("待验证"),
        }
        props = {k: v for k, v in props.items() if k in schema}
        _, created = client.create_row_if_absent(
            db_id, "problem_id", problem_id, props)
        written.append({"problem_id": problem_id, "category": category,
                        "count": count, "created": created})
    return {"rows": len(written),
            "written": sum(1 for w in written if w["created"]),
            "categories": [w["category"] for w in written],
            "items": written}


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
