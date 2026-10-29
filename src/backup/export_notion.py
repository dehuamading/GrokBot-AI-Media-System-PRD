"""备份导出（PRD 10.1）：把 Notion 各库导出为 JSON/CSV 到 data/exports/。

只导出业务数据（素材/选题/经验/审计），不含任何凭据。
公开仓库仅提交脱敏样本；完整导出保留在本地/私有位置（见 docs/restore-runbook.md）。
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.notion_store.client import NotionClient

EXPORT_FIELDS = {
    "01_原始素材": ["item_id", "source_type", "source_url", "author_or_publisher",
                  "published_at", "collected_at", "title_or_summary", "evidence_status",
                  "processing_status", "screen_score"],
    "03_选题决策": ["topic_id", "拟定标题", "建议平台", "推荐级别", "用户选择结果"],
    "05_经验与复盘": ["record_id", "类型", "状态", "性质", "version"],
    "08_维护与审计": ["event_id", "对象", "操作", "结果", "occurred_at"],
}


def cell(page: dict, prop: str):
    p = page.get("properties", {}).get(prop, {})
    t = p.get("type")
    if t == "title":
        return "".join(x["plain_text"] for x in p.get("title", []))
    if t == "rich_text":
        return "".join(x["plain_text"] for x in p.get("rich_text", []))
    if t == "url":
        return p.get("url") or ""
    if t == "select":
        return (p.get("select") or {}).get("name", "")
    if t == "number":
        return p.get("number")
    if t == "date":
        return (p.get("date") or {}).get("start", "")
    return ""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--state", default="data/state/p1_state.json")
    ap.add_argument("--out", default="data/exports")
    args = ap.parse_args()

    state = json.loads(Path(args.state).read_text(encoding="utf-8"))
    client = NotionClient()
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    summary = {}
    stamp = (state.get("last_collect") or {}).get("run_id") or "manual"
    for name, fields in EXPORT_FIELDS.items():
        db_id = state["db_ids"].get(name)
        if not db_id:
            continue
        rows = client.query_database(db_id)
        records = [{f: cell(r, f) for f in fields} for r in rows]
        (out_dir / f"{name}_{stamp}.json").write_text(
            json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
        with open(out_dir / f"{name}_{stamp}.csv", "w", newline="", encoding="utf-8-sig") as fh:
            w = csv.DictWriter(fh, fieldnames=fields)
            w.writeheader()
            w.writerows(records)
        summary[name] = len(records)

    print("导出完成:", json.dumps(summary, ensure_ascii=False), f"→ {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
