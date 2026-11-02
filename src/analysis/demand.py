"""需求引擎（阶段3）：评论 → 10 类用户需求 + 原文证据。

设计约束：
- 10 类固定标签，可多标签；无法归类时标「其他」；
- LLM 只负责分类与一句话摘要，失败/解析失败一律回退本地关键词（透明可解释）；
- 每条结果标注 engine=llm:<model> 或 keyword，便于审计；
- 聚合计数只等于真实评论条数，绝不估算/放大；
- 证据文本脱敏（@用户名 / URL / 手机号 / 邮箱），不携带评论作者；
- 证据只用于本地分析与选题卡「需求证据」的短摘录（≤60 字/条，≤3 条）。

本模块纯本地（除调用方注入的 chat 外），无网络请求。
"""

from __future__ import annotations

import re
from typing import Any

DEMAND_CATEGORIES: list[str] = [
    "安装部署",
    "注册付款",
    "国内使用",
    "免费替代",
    "AI编程",
    "AI内容创作",
    "AI副业赚钱",
    "工作效率",
    "工具比较",
    "故障排查",
]
OTHER_CATEGORY = "其他"
ALL_CATEGORIES = DEMAND_CATEGORIES + [OTHER_CATEGORY]

# 关键词规则：与旧 import_source stub 对齐并扩充；仅用于 LLM 不可用时的回退。
KEYWORD_RULES: dict[str, list[str]] = {
    "安装部署": ["安装", "部署", "配置", "环境", "docker", "pip install",
                 "npm install", "怎么装", "装上", "本地运行", "下载"],
    "注册付款": ["注册", "登录", "付款", "支付", "订阅", "充值", "账号",
                 "信用卡", "外币卡", "扣费", "收费", "价格", "会员"],
    "国内使用": ["国内", "中国", "能用吗", "能不能用", "访问", "网络", "梯子",
                 "翻墙", "魔法", "被墙", "科学上网", "境外"],
    "免费替代": ["免费", "白嫖", "替代", "平替", "开源", "破解", "试用",
                 "不要钱", "省钱", "免费额度"],
    "AI编程": ["编程", "代码", "cursor", "claude code", "vibe coding",
               "开发", "小程序", "网站", "应用", "api", "写代码", "零基础做"],
    "AI内容创作": ["短视频", "视频", "剪辑", "配音", "数字人", "写作",
                   "图片", "绘画", "文案", "自媒体", "起号", "封面", "脚本"],
    "AI副业赚钱": ["副业", "赚钱", "变现", "接单", "创业", "月入", "收入",
                   "怎么卖", "带货", "私域"],
    "工作效率": ["办公", "excel", "ppt", "表格", "文档", "效率", "自动化",
                 "工作流", "会议", "笔记", "pdf"],
    "工具比较": ["对比", "比较", "哪个好", "哪个更", "vs", "区别", "选择",
                 "测评", "评测", "横评"],
    "故障排查": ["报错", "失败", "崩溃", "卡住", "不工作", "无法", "bug",
                 "解决", "闪退", "打不开", "连不上", "超时"],
}

# 脱敏规则：顺序重要（邮箱先于 @用户名，避免残留域名）。
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}")
_URL_RE = re.compile(r"(?:https?://|www\.)\S+", re.I)
_AT_RE = re.compile(r"@[^\s，。；、,;:：()（）\[\]【】]+")
_MOBILE_RE = re.compile(
    r"(?<!\d)(?:\+?86[- ]?)?1[3-9]\d(?:[ -]?\d){8}(?!\d)")
_TEL_RE = re.compile(r"(?<!\d)\d{3,4}-\d{7,8}(?!\d)")


def sanitize_evidence(text: str, limit: int = 60) -> str:
    """去掉 @用户名、URL、手机号/邮箱，压缩空白并截断到 limit 字。"""
    cleaned = _EMAIL_RE.sub(" ", str(text or ""))
    cleaned = _URL_RE.sub(" ", cleaned)
    cleaned = _AT_RE.sub(" ", cleaned)
    cleaned = _MOBILE_RE.sub(" ", cleaned)
    cleaned = _TEL_RE.sub(" ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned[:max(0, limit)]


def keyword_demand_tags(text: str) -> list[str]:
    """关键词回退：返回命中的全部标签；全不命中 → [「其他」]。"""
    lowered = str(text or "").lower()
    tags: list[str] = []
    for category in DEMAND_CATEGORIES:
        if any(k.lower() in lowered for k in KEYWORD_RULES[category]):
            tags.append(category)
    return tags or [OTHER_CATEGORY]


# 兼容旧 import_source 的命名（同一套标签）。
classify_comment_demand = keyword_demand_tags


def _comment_parts(comment: Any) -> tuple[str, str, str]:
    """把字符串/dict 评论统一成 (text, source_url, item_id)。"""
    if isinstance(comment, str):
        return comment, "", ""
    if not isinstance(comment, dict):
        return str(comment or ""), "", ""
    text = str(comment.get("text") or comment.get("comment") or "").strip()
    source_url = str(comment.get("source_url") or comment.get("url") or "").strip()
    item_id = str(comment.get("item_id") or "").strip()
    return text, source_url, item_id


def _model_name(chat) -> str:
    model = getattr(chat, "model", "") or "unknown"
    return str(model)


def _chat_json(chat, system: str, user: str) -> dict:
    """兼容 chat_json 与 chat(system, user) 两种客户端签名。"""
    if hasattr(chat, "chat_json"):
        try:
            return chat.chat_json(system, user, temperature=0.1, max_tokens=4000)
        except TypeError:
            return chat.chat_json(system, user)
    content = chat.chat(system, user, temperature=0.1, max_tokens=4000)
    from src.llm.glm import parse_json_loose
    return parse_json_loose(content)


def _result_list(data: Any) -> list[dict]:
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if isinstance(data, dict):
        for key in ("results", "items", "data", "demands"):
            if isinstance(data.get(key), list):
                return [x for x in data[key] if isinstance(x, dict)]
        if "i" in data or "tags" in data:
            return [data]
    return []


DEMAND_SYSTEM = (
    "你是中文 AI 自媒体需求分类分析师。把每条评论归入以下 10 类需求（可多标签）："
    + "、".join(DEMAND_CATEGORIES)
    + "；无法归类时用「其他」。同时给出一句话问题摘要。"
    "严格只输出 JSON：{\"results\":[{\"i\":1,\"tags\":[\"国内使用\"],"
    "\"问题摘要\":\"一句话\"}]}。不要输出 JSON 以外的文字。"
)


def _flatten_materials_if_needed(comments: list) -> list:
    """兼容直接传入素材列表（含 comments[]）的调用方式。"""
    if any(isinstance(c, dict) and isinstance(c.get("comments"), list)
           for c in comments):
        flat = []
        for m in comments:
            if not isinstance(m, dict):
                continue
            for idx, comment in enumerate(m.get("comments") or []):
                text, url, _ = _comment_parts(comment)
                if not text:
                    continue
                flat.append({
                    "text": text,
                    "source_url": url or m.get("source_url", ""),
                    "item_id": m.get("item_id", ""),
                    "comment_index": idx,
                })
        return flat
    return comments


def classify_demands(comments: list, chat=None, preference_text: str = "",
                     batch_size: int = 40) -> list[dict]:
    """批量分类评论需求；每批 ≤40 条，LLM 失败/解析失败逐批回退关键词。

    返回与输入等长的列表，每项：
    {i, index, text, tags[], summary, 问题摘要, engine, source_url, item_id}
    """
    comments = _flatten_materials_if_needed(comments)
    batch_size = max(1, min(40, int(batch_size or 40)))
    out: list[dict] = []
    for start in range(0, len(comments), batch_size):
        chunk = comments[start:start + batch_size]
        texts = [_comment_parts(c) for c in chunk]
        llm_engine = f"llm:{_model_name(chat)}" if chat is not None else "keyword"
        results: dict[int, dict] = {}

        if chat is not None and any(t for t, _, _ in texts):
            lines = [f"#{i} 评论：{t[:300]}" for i, (t, _, _) in enumerate(texts, 1)]
            system = DEMAND_SYSTEM
            if preference_text:
                system += f"\n\n【用户偏好参考（仅参考）】\n{preference_text}"
            user = (f"请分类以下评论（共 {len(lines)} 条）：\n" + "\n".join(lines))
            try:
                data = _chat_json(chat, system, user)
                for r in _result_list(data):
                    try:
                        n = int(r.get("i", r.get("index")))
                    except (TypeError, ValueError):
                        continue
                    if not 1 <= n <= len(chunk):
                        continue
                    raw_tags = r.get("tags") or []
                    if isinstance(raw_tags, str):
                        raw_tags = re.split(r"[,，、|]", raw_tags)
                    tags = [str(t).strip() for t in raw_tags
                            if str(t).strip() in ALL_CATEGORIES]
                    # 去重且保序
                    tags = list(dict.fromkeys(tags))
                    if not tags:
                        tags = [OTHER_CATEGORY]
                    summary = str(r.get("问题摘要") or r.get("summary") or "").strip()
                    results[n] = {"tags": tags, "summary": summary[:120]}
            except Exception:  # noqa: BLE001 - LLM 失败一律回退关键词
                results = {}

        for i, (text, source_url, item_id) in enumerate(texts, 1):
            if i in results:
                tags = results[i]["tags"]
                summary = results[i]["summary"]
                engine = llm_engine
            else:
                tags = keyword_demand_tags(text)
                summary = text[:120]
                engine = "keyword"
            out.append({
                "i": start + i,
                "index": start + i - 1,
                "text": text,
                "tags": tags,
                "summary": summary,
                "问题摘要": summary,
                "engine": engine,
                "source_url": source_url,
                "item_id": item_id,
            })
    return out


class DemandAggregation(list):
    """按类聚合结果（list），额外支持按类名索引，便于调用方使用。"""

    def __getitem__(self, key):
        if isinstance(key, str):
            for rec in self:
                if rec.get("category") == key:
                    return rec
            raise KeyError(key)
        return super().__getitem__(key)

    def by_category(self) -> dict[str, dict]:
        return {rec["category"]: rec for rec in self}

    def get(self, key, default=None):
        try:
            return self[key]
        except KeyError:
            return default

    def keys(self):
        return [rec["category"] for rec in self]

    def values(self):
        return list(self)

    def items(self):
        return [(rec["category"], rec) for rec in self]

    def __contains__(self, key):
        if isinstance(key, str):
            return any(rec.get("category") == key for rec in self)
        return super().__contains__(key)


def aggregate_demands(items: list[dict]) -> DemandAggregation:
    """按 10 类聚合，count=真实评论条数；每类最多 5 条脱敏证据。

    输入可来自 classify_demands，也可直接是带 text 的原始评论（自动关键词回退）。
    """
    buckets: dict[str, dict] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or item.get("comment") or "")
        tags = item.get("tags")
        if isinstance(tags, str):
            tags = re.split(r"[,，、|]", tags)
        if isinstance(tags, (tuple, set)):
            tags = list(tags)
        if not isinstance(tags, list) or not tags:
            tags = keyword_demand_tags(text)
        tags = [str(t) for t in dict.fromkeys(tags) if str(t) in ALL_CATEGORIES]
        if not tags:
            tags = [OTHER_CATEGORY]
        source_url = str(item.get("source_url") or item.get("url") or "")
        for tag in tags:
            rec = buckets.setdefault(tag, {
                "category": tag,
                "count": 0,
                "evidence": [],
                "evidence_short": [],
                "source_urls": [],
            })
            rec["count"] += 1
            if source_url and source_url not in rec["source_urls"]:
                rec["source_urls"].append(source_url)
            cleaned = sanitize_evidence(text)
            if cleaned and len(rec["evidence"]) < 5:
                if all(e["text"] != cleaned for e in rec["evidence"]):
                    rec["evidence"].append({"text": cleaned,
                                            "source_url": source_url})
            if len(rec["evidence_short"]) < 3:
                if cleaned and all(e["text"] != cleaned for e in rec["evidence_short"]):
                    rec["evidence_short"].append({"text": cleaned,
                                                  "source_url": source_url})

    order = {name: i for i, name in enumerate(ALL_CATEGORIES)}
    result = DemandAggregation(sorted(
        buckets.values(), key=lambda r: (-r["count"], order.get(r["category"], 99))))
    return result


def demand_evidence_for_item(item: dict) -> list[dict]:
    """提取单条素材的评论需求证据（带标签、原文与 source_url）。"""
    out: list[dict] = []
    for comment in item.get("comments") or []:
        if isinstance(comment, str):
            text = comment
            tags = keyword_demand_tags(text)
            source_url = str(item.get("source_url") or "")
        elif isinstance(comment, dict):
            text = str(comment.get("text") or "")
            tags = comment.get("demand_tags")
            if isinstance(tags, (tuple, set)):
                tags = list(tags)
            if not isinstance(tags, list) or not tags:
                tags = keyword_demand_tags(text)
            source_url = str(comment.get("source_url")
                             or item.get("source_url") or "")
        else:
            continue
        tags = [str(t) for t in tags if str(t) in ALL_CATEGORIES]
        if not tags:
            continue
        out.append({"text": text, "tags": tags, "source_url": source_url})
    # 兼容调用方已把分类结果放在 item["demand_evidence"] 的情况。
    if not out and isinstance(item.get("demand_evidence"), list):
        for ev in item["demand_evidence"]:
            if not isinstance(ev, dict):
                continue
            tags = [str(t) for t in (ev.get("tags") or [])
                    if str(t) in ALL_CATEGORIES]
            if tags:
                out.append({
                    "text": str(ev.get("text") or ""),
                    "tags": tags,
                    "source_url": str(ev.get("source_url") or item.get("source_url") or ""),
                })
    return out


def has_demand_evidence(item: dict) -> bool:
    """是否有可用的评论需求证据（至少一个非「其他」标签）。"""
    return any(set(e["tags"]) - {OTHER_CATEGORY}
               for e in demand_evidence_for_item(item))


def short_evidence_texts(item: dict, limit: int = 3) -> list[str]:
    """给选题卡用的短证据摘录（≤60 字/条，≤limit 条，已脱敏）。"""
    texts: list[str] = []
    for ev in demand_evidence_for_item(item):
        cleaned = sanitize_evidence(ev["text"])
        if cleaned and cleaned not in texts:
            texts.append(cleaned)
        if len(texts) >= limit:
            break
    return texts
