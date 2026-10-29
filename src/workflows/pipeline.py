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
from src.connectors.x_following_source import load_following
from src.llm.base import DraftClient, platforms_of
from src.normalize import dedupe, iso_now, screen_score, validate_item
from src.notion_store.gates import (read_gate1_selections, read_gate2_status,
                                    validate_gate1, write_draft_row)


# ---------------------------------------------------------------------- S1
def step_collect(state, *, aihot_file: str | Path | None, x_following_file: str | Path,
                 live: bool = False, snapshot_dir: str | Path = "data/raw",
                 fetcher=fetch_selected, loader=load_from_file) -> dict:
    """S1 数据抓取：aihot（默认离线回放；--live 才真实请求，≤20 条）+ X 名单文件。"""
    materials = fetcher(limit=20) if live else loader(aihot_file)
    accounts = load_following(x_following_file)
    for it in materials:
        errs = validate_item(it)
        if errs:
            raise ValueError(f"素材校验失败 {it['item_id']}: {errs}")
    materials, dropped = dedupe(materials)

    snapshot = None
    if snapshot_dir:
        snap_dir = Path(snapshot_dir) / f"run_{iso_now().replace(':', '').replace('+', '_')}"
        snap_dir.mkdir(parents=True, exist_ok=True)
        snapshot = str(snap_dir / "materials.json")
        Path(snapshot).write_text(json.dumps(materials, ensure_ascii=False, indent=2),
                                  encoding="utf-8")

    state.update(last_collect={"materials": len(materials), "dropped": len(dropped),
                               "accounts": len(accounts), "live": live,
                               "snapshot": snapshot, "run_id": iso_now()})
    state.set_stage("collected")
    state.save()
    return {"materials": materials, "accounts": accounts, "dropped": dropped,
            "snapshot": snapshot}


# ---------------------------------------------------------------------- S2
def step_analyze(state, materials: list[dict], top_k: int = 8) -> dict:
    """S2 分析：去重(已在前置)→分类→评分→选题卡。纯本地。"""
    cards, details = build_topic_cards(materials, top_k=top_k)
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
                      materials: list[dict], page_ids: dict[str, str] | None = None) -> dict:
    """S3 写入 Notion：01 更新分数/状态；03 upsert 选题卡（待用户选择）。"""
    from src.notion_store.client import (date_prop, number_prop, rt,
                                         select_prop, url_prop)
    page_ids = page_ids or {}
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
        })
        n_top += 1
    return {"materials_written": n_mat, "topics_written": n_top}


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
