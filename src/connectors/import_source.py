"""国内统一导入接口（阶段2-B3）：本地 JSON/CSV → 统一素材。

边界（任务书）：本模块只读本地文件，不访问任何网络；不登录平台、不绕过限制。
飞书只保留接口类与字段映射（见 docs/import-schema.md），本阶段无联网代码。
"""

from __future__ import annotations

import csv
import hashlib
import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from src.normalize import iso_now, make_item_id, normalize_url

PLATFORMS = {"douyin", "bilibili", "xiaohongshu", "other"}
AUTHORIZATION_STATUSES = {"公开", "已授权", "待确认"}
REQUIRED_FIELDS = ["platform", "original_url", "title", "published_at",
                   "authorization_status"]
DEFAULT_ERRORS_PATH = "data/state/import_errors.jsonl"

# 评论需求分类 stub（第3阶段将替换为 analysis/demand.py 的正式实现）
DEMAND_CATEGORIES: list[tuple[str, list[str]]] = [
    ("安装部署", ["安装", "部署", "配置", "环境", "docker", "pip install",
                  "npm install", "怎么装", "装上"]),
    ("注册付款", ["注册", "登录", "付款", "支付", "订阅", "充值", "账号",
                  "信用卡", "外币卡", "扣费", "收费"]),
    ("国内使用", ["国内", "中国", "能用吗", "访问", "网络", "梯子", "翻墙",
                  "魔法", "被墙"]),
    ("免费替代", ["免费", "白嫖", "替代", "平替", "开源", "破解", "试用",
                  "不要钱"]),
    ("AI编程", ["编程", "代码", "cursor", "claude code", "vibe coding",
                "开发", "小程序", "网站", "api", "写代码"]),
    ("AI内容创作", ["短视频", "视频", "剪辑", "配音", "数字人", "写作",
                    "图片", "绘画", "文案", "自媒体", "起号"]),
    ("AI副业赚钱", ["副业", "赚钱", "变现", "接单", "创业", "月入", "收入",
                    "怎么卖"]),
    ("工作效率", ["办公", "excel", "ppt", "表格", "文档", "效率", "自动化",
                  "工作流"]),
    ("工具比较", ["对比", "比较", "哪个好", "哪个更", "vs", "区别", "选择"]),
    ("故障排查", ["报错", "失败", "崩溃", "卡住", "不工作", "无法", "bug",
                  "解决", "闪退"]),
]


def classify_comment_demand(text: str) -> list[str]:
    """评论需求分类钩子（关键词 stub，透明可解释；第3阶段替换）。"""
    lowered = (text or "").lower()
    tags: list[str] = []
    for category, keywords in DEMAND_CATEGORIES:
        if any(k.lower() in lowered for k in keywords):
            tags.append(category)
    return tags


class FeishuAdapter:
    """飞书多维表格适配接口占位：本阶段不联网、不读取真实凭据。"""

    name = "飞书多维表格"

    def fetch_records(self, *args, **kwargs):
        raise NotImplementedError(
            "飞书适配仅保留接口与字段映射；本阶段不联网，请导出 JSON/CSV 后使用导入接口")

    def map_record(self, record: dict, *args, **kwargs):
        raise NotImplementedError(
            "飞书字段映射见 docs/import-schema.md；当前阶段未实现联网读取")

    def to_import_items(self, records, *args, **kwargs):
        raise NotImplementedError("飞书联网读取未实现；本阶段只支持本地 JSON/CSV")


def _split_multi(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    return [v.strip() for v in re.split(r"[|\n]", str(value)) if v.strip()]


def _parse_metrics(value) -> dict:
    if isinstance(value, dict):
        return value
    if value in (None, ""):
        return {}
    if isinstance(value, str):
        parsed = json.loads(value)
        if not isinstance(parsed, dict):
            raise ValueError("metrics 应为对象/JSON 对象")
        return parsed
    raise ValueError("metrics 应为对象/JSON 对象")


def _parse_comments(value) -> list[dict]:
    comments: list[dict] = []
    if value in (None, ""):
        return comments
    raw_list = value if isinstance(value, list) else _split_multi(value)
    for raw in raw_list:
        if isinstance(raw, dict):
            text = str(raw.get("text") or "").strip()
            created_at = raw.get("created_at") or ""
            metrics = raw.get("metrics") if isinstance(raw.get("metrics"), dict) else {}
        else:
            text = str(raw).strip()
            created_at = ""
            metrics = {}
        if not text:
            continue
        comments.append({
            "text": text,
            "created_at": created_at,
            "metrics": metrics,
            "demand_tags": classify_comment_demand(text),
        })
    return comments


def _canonical_url(url: str) -> str:
    """去跟踪参数/尾斜杠，并忽略 http/https 差异。"""
    normalized = normalize_url(url)
    parts = urlsplit(normalized)
    return urlunsplit(("", parts.netloc.lower(), parts.path, parts.query, ""))


def _title_hash(title: str) -> str:
    clean = re.sub(r"\s+", "", (title or "").lower())
    return hashlib.sha1(clean.encode("utf-8")).hexdigest()


def _validate_record(record: dict) -> list[str]:
    errors: list[str] = []
    for field in REQUIRED_FIELDS:
        if record.get(field) in (None, ""):
            errors.append(f"缺少必填字段: {field}")
    platform = str(record.get("platform") or "").strip().lower()
    if platform and platform not in PLATFORMS:
        errors.append(f"非法 platform: {platform}（允许 {sorted(PLATFORMS)}）")
    auth = record.get("authorization_status")
    if auth and auth not in AUTHORIZATION_STATUSES:
        errors.append(f"非法 authorization_status: {auth}（允许 {sorted(AUTHORIZATION_STATUSES)}）")
    try:
        _parse_metrics(record.get("metrics"))
    except (ValueError, json.JSONDecodeError) as exc:
        errors.append(f"metrics 解析失败: {exc}")
    return errors


def _build_item(record: dict, *, batch_id: str, source_file: str, row: int) -> dict:
    platform = str(record["platform"]).strip().lower()
    url = str(record["original_url"]).strip()
    title = str(record["title"]).strip()
    auth = str(record["authorization_status"]).strip()
    comments = _parse_comments(record.get("comments"))
    keywords = _split_multi(record.get("keywords"))
    metrics = _parse_metrics(record.get("metrics"))
    source = str(record.get("source") or "国内导入").strip()
    collected_at = record.get("collected_at") or iso_now()
    license_status = {"公开": "公开", "已授权": "授权", "待确认": "待确认"}.get(
        auth, "待确认")
    digest = hashlib.sha1(
        f"{platform}|{_canonical_url(url)}|{_title_hash(title)}".encode("utf-8")
    ).hexdigest()[:16]

    return {
        "item_id": make_item_id("import", digest),
        "source_type": "import",
        "source_url": url,
        "author_or_publisher": source,
        "published_at": record.get("published_at"),
        "collected_at": collected_at,
        "title_or_summary": title,
        "raw_reference": f"国内导入：{source_file} 第 {row} 行（批次 {batch_id}）",
        "claims": [title],
        "evidence_status": "unverified",  # 公开评论=用户自述，未交叉核验
        "target_user_hypotheses": [],
        "user_problem_evidence": [c["text"] for c in comments][:5],
        "tool_or_method": keywords,
        "duplicate_of": None,
        "processing_status": "new",
        "platform": platform,
        "origin_agent": "import",
        "keywords": keywords,
        "comments": comments,
        "metrics": metrics,
        "license_status": license_status,
        "authorization_status": auth,
        "errors": [],
        "extra": {
            "import": {
                "batch_id": batch_id,
                "source_file": source_file,
                "row": row,
                "rows": [row],
                "source": source,
                "authorization_status": auth,
            }
        },
    }


def _dedupe_items(items: list[dict]) -> tuple[list[dict], int]:
    kept: list[dict] = []
    index: dict[tuple[str, str], dict] = {}
    duplicates = 0
    for item in items:
        key = (_canonical_url(item["source_url"]), _title_hash(item["title_or_summary"]))
        existing = index.get(key)
        if existing is None:
            index[key] = item
            kept.append(item)
            continue
        duplicates += 1
        seen_comments = {(c["text"], c.get("created_at")) for c in existing["comments"]}
        for comment in item["comments"]:
            if (comment["text"], comment.get("created_at")) not in seen_comments:
                existing["comments"].append(comment)
        existing["user_problem_evidence"] = [
            c["text"] for c in existing["comments"]][:5]
        for kw in item["keywords"]:
            if kw not in existing["keywords"]:
                existing["keywords"].append(kw)
        for metric, value in item["metrics"].items():
            old = existing["metrics"].get(metric)
            if isinstance(value, (int, float)) and (
                    not isinstance(old, (int, float)) or value > old):
                existing["metrics"][metric] = value
            elif metric not in existing["metrics"]:
                existing["metrics"][metric] = value
        imp = existing["extra"]["import"]
        imp["rows"].append(item["extra"]["import"]["row"])
        imp["duplicate_rows"] = imp.get("duplicate_rows", 0) + 1
    return kept, duplicates


def _write_errors(errors: list[dict], path: str | Path) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as f:
        for err in errors:
            f.write(json.dumps(err, ensure_ascii=False) + "\n")


def load_import_file(path: str | Path, *,
                     errors_path: str | Path = DEFAULT_ERRORS_PATH,
                     batch_id: str | None = None) -> dict:
    """读取本地 JSON/CSV；坏行写入错误日志但不中断其余行。"""
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"导入文件不存在：{p}")
    suffix = p.suffix.lower()
    if suffix not in (".json", ".csv"):
        raise ValueError("导入文件只支持 .json 或 .csv")

    batch = batch_id or (datetime.now(timezone.utc).strftime("IMP-%Y%m%d%H%M%S-")
                         + uuid.uuid4().hex[:6])
    source_file = str(p)
    errors: list[dict] = []
    raw_records: list[tuple[int, dict]] = []

    if suffix == ".json":
        data = json.loads(p.read_text(encoding="utf-8"))
        records = data.get("items") if isinstance(data, dict) else data
        if not isinstance(records, list):
            raise ValueError("导入 JSON 应为数组或 {\"items\": [...]}")
        raw_records = [(i, rec) for i, rec in enumerate(records, start=1)]
    else:
        with p.open("r", encoding="utf-8-sig", newline="") as f:
            rows = list(csv.DictReader(f))
        raw_records = [(i, dict(row)) for i, row in enumerate(rows, start=2)]

    built: list[dict] = []
    for row, record in raw_records:
        if not isinstance(record, dict):
            msg = "记录不是对象"
            errors.append({"time": iso_now(), "import_batch": batch,
                           "source_file": source_file, "row": row, "error": msg})
            continue
        if not any(str(v or "").strip() for v in record.values()):
            continue  # CSV 空行
        problems = _validate_record(record)
        if problems:
            errors.append({"time": iso_now(), "import_batch": batch,
                           "source_file": source_file, "row": row,
                           "error": "; ".join(problems)})
            continue
        built.append(_build_item(record, batch_id=batch,
                                 source_file=source_file, row=row))

    items, duplicates = _dedupe_items(built)
    if errors:
        _write_errors(errors, errors_path)
    return {
        "items": items,
        "errors": errors,
        "batch_id": batch,
        "source_file": source_file,
        "total_rows": len(raw_records),
        "dropped_duplicates": duplicates,
    }
