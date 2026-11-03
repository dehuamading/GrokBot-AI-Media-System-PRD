"""人工关口（任务03核心）：Notion 是唯一事实源。

- GATE-1（选题）：用户在 03_选题决策 里把卡片「用户选择结果」改为 已选定（1–3 张），
  可另设「用户指定平台」。程序只认实时查询结果，本地状态不能替代。
- GATE-2（终稿）：用户在 04_文案与版本 里把状态改为 已确认（或 修订中 触发修订，
  修订循环属 P4，未完成）。
- 本模块无任何发布能力：读取 + 幂等写入 Notion 04 行/正文块，到此为止。
"""

from __future__ import annotations

import re

from src.normalize import iso_now
from src.notion_store.client import (NotionClient, date_prop, number_prop,
                                     rt, select_prop)


def _cell(page: dict, prop: str) -> str:
    """提取属性纯文本（兼容真实 API 的 plain_text 与写入时的 text.content 两种形态）。"""
    p = (page.get("properties") or {}).get(prop) or {}
    t = p.get("type")

    def txt(parts):
        return "".join((x.get("plain_text") or (x.get("text") or {}).get("content") or "")
                       for x in parts or [])

    if t == "title":
        return txt(p.get("title"))
    if t == "rich_text":
        return txt(p.get("rich_text"))
    if t == "select":
        return (p.get("select") or {}).get("name", "")
    if t == "number":
        value = p.get("number")
        return "" if value is None else value
    if t == "url":
        return p.get("url") or ""
    if t == "date":
        return (p.get("date") or {}).get("start", "") if p.get("date") else ""
    return ""


def _card_from_row(r: dict) -> dict:
    """从 03 行还原全部字段（Notion 是唯一事实源，不丢字段）。"""
    props = r.get("properties") or {}
    all_fields = {name: _cell(r, name) for name in props}
    topic_id = str(all_fields.get("topic_id") or _cell(r, "topic_id"))
    platform = (_cell(r, "用户指定平台") or _cell(r, "建议平台")
                or all_fields.get("建议平台") or "两者")
    platforms = ["抖音", "小红书"] if platform == "两者" else [platform]
    linked_text = str(all_fields.get("关联素材") or _cell(r, "关联素材"))
    linked = [x for x in linked_text.replace(" ", "").split("、") if x]
    source = (all_fields.get("原始来源") or _cell(r, "原始来源")
              or all_fields.get("引用帖子") or "")
    # 与 draft_writer._split_urls 一致：中文逗号/顿号/分号/空白均可分隔。
    sources = [s for s in re.split(r"[，,、；;\s]+", str(source)) if s]
    card = dict(all_fields)
    card.update({
        "topic_id": topic_id, "row_id": r["id"], "platform": platform,
        "platforms": platforms, "关联素材": linked,
        "原始来源": source, "_source_urls": sources,
    })
    return card


# ---------------------------------------------------------------------- GATE-1
def read_gate1_selections(client: NotionClient, db_ids: dict[str, str]) -> list[dict]:
    """读取用户在 Notion 中已选定的选题（Notion 是唯一事实源）。

    返回完整选题卡（从 03 行属性还原全部 PRD 5.4 字段）+ platforms。
    这样任务02遗留的已在库选题卡同样可被选用，不依赖本地文件。
    """
    rows = client.query_database(
        db_ids["03_选题决策"],
        {"property": "用户选择结果", "select": {"equals": "已选定"}})
    return [_card_from_row(r) for r in rows]


def read_topic_cards_by_ids(client: NotionClient, db_ids: dict[str, str],
                            topic_ids: list[str], *,
                            require_selected: bool = True) -> list[dict]:
    """按 topic_id 读取 03 完整卡片（draft --topic 显式指定时使用）。

    GATE-1 语义：默认只允许「用户选择结果=已选定」的卡进入写稿；
    显式指定未选定的卡必须拒绝并提示，而不是静默写 04。
    """
    wanted = {str(t).strip() for t in topic_ids if str(t).strip()}
    order = [str(t).strip() for t in topic_ids if str(t).strip()]
    found: dict[str, dict] = {}
    for row in client.query_database(db_ids["03_选题决策"]):
        card = _card_from_row(row)
        if card.get("topic_id") in wanted:
            found[card["topic_id"]] = card
    cards = [found[tid] for tid in order if tid in found]
    if require_selected:
        unselected = [c.get("topic_id") for c in cards
                      if c.get("用户选择结果") != "已选定"]
        if unselected:
            raise ValueError(
                f"GATE-1：以下选题卡未选定，拒绝写稿：{unselected}；"
                "请先在 03_选题决策 将「用户选择结果」改为 已选定。")
    return cards


def validate_gate1(selections: list[dict]) -> None:
    """PRD 5.4：用户须选择 1—3 个（或更多？PRD 原文『1—3 个或更多』→ 上限放宽到 5）。"""
    if not 1 <= len(selections) <= 5:
        raise ValueError(
            f"GATE-1 校验失败：已选定 {len(selections)} 张，须为 1—5 张"
            "（PRD 5.4：1—3 个或更多；超过 5 张请分批）")


# ---------------------------------------------------------------------- 文案写入
def write_draft_row(client: NotionClient, db_ids: dict[str, str], draft,
                    topic_id: str) -> tuple[str, bool]:
    """把 Draft 幂等写入 04_文案与版本（行属性 + 正文块）。返回 (page_id, 是否新建)。"""
    page_id, created = client.upsert_row(
        db_ids["04_文案与版本"], "draft_id", draft.draft_id, {
            "topic_id": rt(topic_id),
            "platform": select_prop(draft.platform),
            "version": number_prop(draft.version),
            "status": select_prop("V1"),
            "生成引擎": rt(draft.engine),
            "feedback_summary": rt(""),
            "change_summary": rt("初稿 V1（待用户审阅：改状态=修订中 或 已确认）"),
            "updated_at": date_prop(iso_now()),
        })
    # 正文块（幂等由 draft_id 行级 upsert 保证；同稿重写则追加标注）
    blocks = _draft_blocks(draft)
    client.append_blocks(page_id, blocks)
    return page_id, created


def write_draft_row_pending(client: NotionClient, db_ids: dict[str, str], draft,
                            topic_id: str) -> tuple[str, bool]:
    """阶段4：create_row_if_absent 写 04，状态=待审核（绝不改 03/绝不发布）。"""
    page_id, created = client.create_row_if_absent(
        db_ids["04_文案与版本"], "draft_id", draft.draft_id, {
            "topic_id": rt(topic_id),
            "platform": select_prop(draft.platform),
            "version": number_prop(draft.version),
            "status": select_prop("待审核"),
            "生成引擎": rt(draft.engine),
            "feedback_summary": rt(""),
            "change_summary": rt("初稿（待用户审核；系统无发布能力）"),
            "updated_at": date_prop(iso_now()),
        })
    if created:
        client.append_blocks(page_id, _draft_blocks(draft))
    return page_id, created


def _draft_blocks(draft) -> list[dict]:
    def para(text: str) -> dict:
        return {"type": "paragraph",
                "paragraph": {"rich_text": [{"text": {"content": text[:1900]}}]}}

    def h2(text: str) -> dict:
        return {"type": "heading_2",
                "heading_2": {"rich_text": [{"text": {"content": text}}]}}

    titles = list(getattr(draft, "titles", None) or [draft.title])
    openings = list(getattr(draft, "openings", None) or [])
    demos = list(getattr(draft, "demo_suggestions", None) or [])
    blocks = [h2(f"初稿 · {draft.platform} · {draft.engine}"),
              h2("标题候选（3 个）")]
    blocks += [para(f"{i}. {t}") for i, t in enumerate(titles, 1)]
    if openings:
        blocks.append(h2("开头候选（2 个）"))
        blocks += [para(f"{i}. {o}") for i, o in enumerate(openings, 1)]
    blocks += [h2("完整口播稿"), para(draft.body)]
    if demos:
        blocks.append(h2("操作演示建议"))
        blocks += [para(f"- {d}") for d in demos]
    blocks.append(h2("事实核验清单"))
    blocks += [para(f"- {c}") for c in draft.fact_checklist] or [para("（无）")]
    blocks.append(h2("原始来源链接"))
    blocks += [para(f"- {s}") for s in draft.sources] or [para("（无）")]
    blocks.append(h2("未证实内容提醒"))
    blocks += [para(f"- {w}") for w in draft.unverified_warnings] or [para("（无）")]
    blocks.append(para("⚠️ 本稿为系统生成的待审初稿：未发布、不会自动发布；"
                       "请审阅后在属性中改 状态=已确认 或 修订中（附反馈）。"))
    return blocks


# ---------------------------------------------------------------------- GATE-2
def read_gate2_status(client: NotionClient, db_ids: dict[str, str]) -> dict:
    """读取 04 库各稿状态汇总（GATE-2 观测，只读，无后续自动动作）。"""
    rows = client.query_database(db_ids["04_文案与版本"])
    drafts = [{"draft_id": _cell(r, "draft_id"), "topic_id": _cell(r, "topic_id"),
               "platform": _cell(r, "platform"),
               "status": _cell(r, "status")} for r in rows]
    confirmed = sum(1 for d in drafts if d["status"] == "已确认")
    revising = sum(1 for d in drafts if d["status"] == "修订中")
    pending = sum(1 for d in drafts if d["status"] in ("V1", "待生成", "待审核"))
    return {"drafts": drafts, "confirmed": confirmed, "revising": revising,
            "pending": pending,
            "note": "GATE-2 只读观测：已确认≠发布（系统无发布能力）；"
                    "修订中→修订循环属 P4（未完成）"}
