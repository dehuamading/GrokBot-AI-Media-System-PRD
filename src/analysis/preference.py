"""偏好学习（阶段3）：复用 05_经验与复盘，不新建库。

闭环：
- record_feedback：每轮 GATE-1 后写一条「反馈」；用户认可时另写一条「认可素材」；
- load_recent_preferences：读最近 n 条认可/否决（含原话）与「正在使用」规则；
- build_preference_prompt：拼成 LLM 提示块（候选规则仅供参考，不得当硬规则）；
- propose_rules：每 3 轮反馈生成 ≤10 条候选规则，状态=待确认，绝不自动启用。

红线：绝不修改 03 选题卡状态；绝不删除记录；绝不自动改代码/配置；
候选规则必须由用户在 Notion 手动改为「正在使用」后才生效。
"""

from __future__ import annotations

from datetime import datetime, timezone

from src.normalize import iso_now
from src.notion_store.client import (date_prop, number_prop, rt, select_prop)

VALID_PICKS = ("认可", "一般", "否决")
STATUS_PENDING = "待验证"
STATUS_CANDIDATE = "待确认"
STATUS_ACTIVE = "正在使用"


def _text(page: dict, prop: str) -> str:
    p = (page.get("properties") or {}).get(prop) or {}
    t = p.get("type")

    def join(parts):
        return "".join((x.get("plain_text") or (x.get("text") or {}).get("content") or "")
                       for x in parts or [])

    if t == "title":
        return join(p.get("title"))
    if t == "rich_text":
        return join(p.get("rich_text"))
    if t == "select":
        return (p.get("select") or {}).get("name", "") or ""
    if t == "number":
        return p.get("number")
    if t == "date":
        return (p.get("date") or {}).get("start", "") if p.get("date") else ""
    return ""


def _int_or(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _join(value) -> str:
    if isinstance(value, (list, tuple)):
        return "、".join(str(x).strip() for x in value if str(x).strip())
    return str(value or "").strip()


def _feedback_time(fb: dict) -> str:
    return str(fb.get("反馈时间") or fb.get("time") or fb.get("updated_at") or "")


def record_feedback(client, db_ids: dict, feedback: dict) -> dict:
    """写一条反馈到 05；用户选择=认可时另写一条认可素材。幂等、只增、不改 03。"""
    topic_id = str(feedback.get("topic_id") or "").strip()
    if not topic_id:
        raise ValueError("feedback 缺少 topic_id")
    pick = str(feedback.get("用户选择") or feedback.get("pick") or "一般").strip()
    if pick not in VALID_PICKS:
        raise ValueError(f"用户选择须为 {VALID_PICKS}，当前：{pick}")
    round_no = _int_or(feedback.get("轮次", feedback.get("round", 1)), 1)
    raw_when = feedback.get("time") or feedback.get("反馈时间") or iso_now()
    when = (raw_when.isoformat() if isinstance(raw_when, datetime)
            else str(raw_when))
    title = str(feedback.get("原始选题") or "").strip()
    why = str(feedback.get("推荐理由") or "").strip()
    say = str(feedback.get("原话反馈") or feedback.get("say") or "").strip()
    reasons = _join(feedback.get("原因标签") or feedback.get("reason"))
    next_ref = str(feedback.get("下轮参考") or feedback.get("next") or "").strip()
    source = str(feedback.get("原始来源") or feedback.get("来源") or "").strip()

    record_id = f"FB-{round_no}-{topic_id}"
    content = (f"原始选题：{title or topic_id}；推荐理由：{why or '未填写'}；"
               f"用户选择：{pick}；原话：{say or '未填写'}")
    props = {
        "topic_id": rt(topic_id),
        "原始选题": rt(title or topic_id),
        "推荐理由": rt(why),
        "用户选择": select_prop(pick),
        "原话反馈": rt(say),
        "原因标签": rt(reasons),
        "轮次": number_prop(round_no),
        "反馈时间": date_prop(when),
        "下轮参考": rt(next_ref),
        "类型": select_prop("反馈"),
        "性质": select_prop("用户偏好"),
        "状态": select_prop(STATUS_PENDING),
        "适用范围": rt("选题偏好"),
        "内容": rt(content),
        "依据": rt(f"{topic_id}" + (f"；{source}" if source else "")),
        "version": number_prop(round_no),
        "updated_at": date_prop(when),
    }
    page_id, created = client.create_row_if_absent(
        db_ids["05_经验与复盘"], "record_id", record_id, props)

    result = {
        "feedback_id": record_id,
        "feedback_page": page_id,
        "feedback_created": created,
        "approved_material_id": None,
        "approved_material_page": None,
        "approved_material_created": False,
    }
    if pick == "认可":
        am_id = f"AM-{topic_id}"
        am_content = (f"一句话要点：{title or topic_id}；"
                      f"我为什么认可：{why or say or '用户认可该方向'}；"
                      f"来源：{source or topic_id}")
        am_props = {
            "topic_id": rt(topic_id),
            "原始选题": rt(title or topic_id),
            "推荐理由": rt(why),
            "用户选择": select_prop("认可"),
            "原话反馈": rt(say),
            "原因标签": rt(reasons),
            "轮次": number_prop(round_no),
            "反馈时间": date_prop(when),
            "下轮参考": rt(next_ref),
            "类型": select_prop("认可素材"),
            "性质": select_prop("用户偏好"),
            "状态": select_prop(STATUS_PENDING),
            "适用范围": rt("选题偏好"),
            "内容": rt(am_content),
            "依据": rt(f"{topic_id}" + (f"；{source}" if source else "")),
            "version": number_prop(round_no),
            "updated_at": date_prop(when),
        }
        am_page, am_created = client.create_row_if_absent(
            db_ids["05_经验与复盘"], "record_id", am_id, am_props)
        result.update({
            "approved_material_id": am_id,
            "approved_material_page": am_page,
            "approved_material_created": am_created,
        })
    return result


def _row_to_feedback(row: dict) -> dict:
    """按真实 05 schema 读取一行；字段名与 Notion 列一一对应。

    真实列：类型/用户选择/原话反馈/原始选题/推荐理由/下轮参考/反馈时间/
    状态/record_id（record_id 是 title 属性）。
    """
    return {
        "record_id": _text(row, "record_id"),
        "topic_id": _text(row, "topic_id"),
        "原始选题": _text(row, "原始选题") or _text(row, "内容"),
        "推荐理由": _text(row, "推荐理由"),
        "用户选择": _text(row, "用户选择"),
        "原话反馈": _text(row, "原话反馈"),
        "原因标签": _text(row, "原因标签"),
        "轮次": _int_or(_text(row, "轮次"), 0),
        "下轮参考": _text(row, "下轮参考"),
        "反馈时间": _text(row, "反馈时间") or _text(row, "updated_at"),
        "类型": _text(row, "类型"),
        "状态": _text(row, "状态"),
        "内容": _text(row, "内容"),
    }


def load_recent_preferences(client, db_ids: dict, n_pos: int = 5,
                            n_neg: int = 5) -> dict:
    """读 05：最近 n_pos 条认可、n_neg 条否决（含原话）与正在使用规则。"""
    rows = client.query_database(db_ids["05_经验与复盘"])
    feedbacks, rules, candidates = [], [], []
    for row in rows:
        rec = _row_to_feedback(row)
        typ = rec["类型"]
        pick = rec["用户选择"]
        status = rec["状态"]
        if typ in ("反馈", "复盘") and pick in ("认可", "否决"):
            feedbacks.append(rec)
        if status == STATUS_ACTIVE and typ in ("规则", "候选规则"):
            rules.append(rec)
        if typ == "候选规则" and status == STATUS_CANDIDATE:
            candidates.append(rec)

    # 反馈时间倒序；同一时间用 record_id 稳定排序，避免 Notion 返回顺序影响结果。
    feedbacks.sort(key=lambda f: (_feedback_time(f), f.get("record_id") or ""),
                   reverse=True)
    approved = [f for f in feedbacks if f["用户选择"] == "认可"][:max(0, n_pos)]
    rejected = [f for f in feedbacks if f["用户选择"] == "否决"][:max(0, n_neg)]
    rules.sort(key=_feedback_time, reverse=True)
    candidates.sort(key=_feedback_time, reverse=True)
    return {
        "approved": approved,
        "rejected": rejected,
        "rules": rules,
        "candidate_rules": candidates,
    }


def _fmt_feedback(items: list[dict]) -> list[str]:
    if not items:
        return ["（无）"]
    out = []
    for f in items:
        title = f.get("原始选题") or f.get("topic_id") or "未命名选题"
        reason = f.get("原话反馈") or f.get("原因标签") or "未填写原话"
        out.append(f"- 选题：{title}；原话理由：{reason}")
    return out


def build_preference_prompt(prefs: dict) -> str:
    """把偏好整理为提示块；候选规则只作参考，明确不得作为硬规则。"""
    approved = _fmt_feedback(prefs.get("approved") or prefs.get("positive") or [])
    rejected = _fmt_feedback(prefs.get("rejected") or prefs.get("negative") or [])
    rules = [f"- {r.get('内容') or r.get('原始选题')}"
             for r in (prefs.get("rules") or prefs.get("active_rules") or [])]
    candidates = [f"- {r.get('内容') or r.get('原始选题')}"
                  for r in (prefs.get("candidate_rules") or prefs.get("candidates") or [])]
    lines = [
        "【近期认可示例】", *approved,
        "【近期否决示例】", *rejected,
        "【正在使用规则】", *(rules or ["（无）"]),
        "【候选规则(待确认)】仅供参考不得作为硬规则", *(candidates or ["（无）"]),
    ]
    return "\n".join(lines)


def _feedback_rounds(client, db_ids: dict) -> list[int]:
    rounds = set()
    for row in client.query_database(db_ids["05_经验与复盘"]):
        rec = _row_to_feedback(row)
        if rec["类型"] in ("反馈", "复盘") and rec["轮次"] >= 1:
            rounds.add(rec["轮次"])
    return sorted(rounds)


def _template_rules(feedbacks: list[dict]) -> list[str]:
    rules: list[str] = []
    for f in sorted(feedbacks, key=lambda x: x.get("轮次") or 0):
        pick = f.get("用户选择")
        if pick not in ("认可", "否决"):
            continue
        label = f.get("原因标签") or f.get("推荐理由") or f.get("原始选题")
        label = (label or "未标注方向").split("；")[0][:40]
        round_no = f.get("轮次") or "?"
        if pick == "认可":
            rule = f"优先选择「{label}」类选题（第 {round_no} 轮用户认可）"
        else:
            rule = f"避免「{label}」类选题（第 {round_no} 轮用户否决）"
        if rule not in rules:
            rules.append(rule)
        if len(rules) >= 10:
            break
    if not rules:
        rules.append("保持当前选题标准：先验证国内可用性与真实用户需求")
    return rules[:10]


def _llm_rules(chat, prefs: dict) -> list[str]:
    system = ("你是中文 AI 自媒体选题教练。根据用户近期认可/否决与原话理由，"
              "归纳不超过 10 条候选选题规则。只输出 JSON："
              '{"rules":["规则1","规则2"]}。不要输出 JSON 以外文字。')
    user = build_preference_prompt(prefs)
    # 与 ProviderChain 的真实签名保持一致：chat_json(system, user, **kw)
    # （底层 chat(system, user, temperature=…, max_tokens=…)）。
    if hasattr(chat, "chat_json"):
        data = chat.chat_json(system, user, temperature=0.2, max_tokens=2000)
    else:
        from src.llm.glm import parse_json_loose
        data = parse_json_loose(chat.chat(system, user, temperature=0.2,
                                          max_tokens=2000))
    raw = data.get("rules") if isinstance(data, dict) else data
    if not isinstance(raw, list):
        return []
    return [str(x).strip() for x in raw if str(x).strip()][:10]


def propose_rules(client, db_ids: dict, chat=None, now: datetime | None = None) -> dict:
    """反馈轮次数为 3 的倍数（≥3）时生成 ≤10 条候选规则，状态=待确认。"""
    now = now or datetime.now(timezone.utc)
    rounds = _feedback_rounds(client, db_ids)
    if len(rounds) < 3 or len(rounds) % 3 != 0:
        return {"proposed": [], "rounds": rounds, "engine": "none",
                "reason": "反馈轮次数未达到 3 的倍数（≥3），本轮不生成候选规则"}

    prefs = load_recent_preferences(client, db_ids, n_pos=10, n_neg=10)
    engine = "template"
    rules: list[str] = []
    if chat is not None:
        try:
            rules = _llm_rules(chat, prefs)
            engine = f"llm:{getattr(chat, 'model', 'unknown')}"
        except Exception:  # noqa: BLE001 - LLM 失败用模板归纳
            rules = []
            engine = "template(fallback)"
    if not rules:
        feedbacks = prefs["approved"] + prefs["rejected"]
        rules = _template_rules(feedbacks)
        if engine.startswith("llm:"):
            engine = "template(fallback)"

    proposed: list[str] = []
    created: list[str] = []
    existing: list[str] = []
    for i, rule in enumerate(rules[:10], start=1):
        # 记录轮次，避免同日/同轮重跑时复用同一 record_id 而静默“假成功”。
        record_id = f"R-{rounds[-1]}-{now.strftime('%Y%m%d')}-{i:02d}"
        props = {
            "类型": select_prop("候选规则"),
            "性质": select_prop("AI推断假设"),
            "状态": select_prop(STATUS_CANDIDATE),
            "适用范围": rt("选题偏好"),
            "内容": rt(rule),
            "依据": rt(f"第 {rounds[-1]} 轮反馈归纳；须用户确认后才生效"),
            "轮次": number_prop(rounds[-1]),
            "updated_at": date_prop(now.isoformat()),
            "version": number_prop(rounds[-1]),
        }
        _, was_created = client.create_row_if_absent(
            db_ids["05_经验与复盘"], "record_id", record_id, props)
        proposed.append(record_id)
        (created if was_created else existing).append(record_id)
    return {"proposed": proposed, "created": created, "existing": existing,
            "rounds": rounds, "engine": engine,
            "reason": "候选规则已写入 05，状态=待确认；需用户手动确认"}
