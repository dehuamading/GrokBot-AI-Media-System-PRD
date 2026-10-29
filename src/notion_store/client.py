"""Notion 适配层（PRD 9.1）：官方 REST API 客户端。

- token 只从环境变量 NOTION_TOKEN 读取，绝不打印、绝不写文件/日志。
- 429 按 Retry-After 退避，5xx 指数退避，最多 4 次；4xx 立即失败（权限/参数问题不重试）。
- 写入接口均为幂等 upsert（先按唯一键查询再创建/更新），满足 PRD 9.3 重跑不重复入库。
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

NOTION_VERSION = "2022-06-28"
BASE = "https://api.notion.com/v1"


class NotionError(RuntimeError):
    pass


class NotionClient:
    def __init__(self, token: str | None = None, throttle: float = 0.35):
        token = token or os.environ.get("NOTION_TOKEN", "")
        if not token:
            raise NotionError(
                "缺少 NOTION_TOKEN 环境变量（运行前从 /workspace/.secrets/notion_token 注入，"
                "禁止在命令行/文档中出现明文）"
            )
        self._headers = {
            "Authorization": f"Bearer {token}",
            "Notion-Version": NOTION_VERSION,
            "Content-Type": "application/json",
        }
        self._throttle = throttle  # ≈2.9 req/s，低于 3 req/s 限速
        self._last_ts = 0.0

    # ------------------------------------------------------------------
    def _request(self, method: str, path: str, body: dict | None = None,
                 params: dict | None = None) -> dict:
        url = f"{BASE}{path}"
        for attempt in range(5):
            wait = self._throttle - (time.monotonic() - self._last_ts)
            if wait > 0:
                time.sleep(wait)
            self._last_ts = time.monotonic()
            resp = requests.request(method, url, headers=self._headers,
                                    json=body, params=params, timeout=30)
            if resp.status_code == 429:
                delay = float(resp.headers.get("Retry-After", 2 * (attempt + 1)))
                time.sleep(delay)
                continue
            if resp.status_code >= 500:
                time.sleep(2 ** attempt)
                continue
            if resp.status_code >= 400:
                # 不打印 headers（含 token），只打印状态码与响应体
                raise NotionError(f"Notion API {resp.status_code}: {resp.text[:400]}")
            return resp.json()
        raise NotionError(f"Notion API 重试耗尽: {method} {path}")

    # ------------------------------------------------------------------
    def get_page(self, page_id: str) -> dict:
        return self._request("GET", f"/pages/{page_id}")

    def create_database(self, parent_page_id: str, title: str,
                        properties: dict, icon: str | None = None) -> dict:
        body = {
            "parent": {"type": "page_id", "page_id": parent_page_id},
            "title": [{"type": "text", "text": {"content": title}}],
            "is_inline": False,
            "properties": properties,
        }
        if icon:
            body["icon"] = {"type": "emoji", "emoji": icon}
        return self._request("POST", "/databases", body)

    def create_page(self, parent: dict, properties: dict,
                    children: list[dict] | None = None) -> dict:
        body = {"parent": parent, "properties": properties}
        if children:
            body["children"] = children
        return self._request("POST", "/pages", body)

    def update_page(self, page_id: str, properties: dict) -> dict:
        return self._request("PATCH", f"/pages/{page_id}",
                             {"properties": properties})

    def query_database(self, db_id: str, filter_: dict | None = None,
                       page_size: int = 100) -> list[dict]:
        out, cursor = [], None
        while True:
            body = {"page_size": page_size}
            if filter_:
                body["filter"] = filter_
            if cursor:
                body["start_cursor"] = cursor
            data = self._request("POST", f"/databases/{db_id}/query", body)
            out.extend(data.get("results") or [])
            if not data.get("has_more"):
                return out
            cursor = data.get("next_cursor")

    def child_databases(self, page_id: str) -> dict[str, str]:
        """列某页面下的子数据库：{标题: block_id}（幂等建库的依据）。"""
        return self._children(page_id, "child_database", "title")

    def child_pages(self, page_id: str) -> dict[str, str]:
        """列某页面下的子页面：{标题: page_id}。"""
        return self._children(page_id, "child_page", "title")

    def _children(self, page_id: str, block_type: str, field: str) -> dict[str, str]:
        out, cursor = {}, None
        while True:
            params = {"page_size": 100}
            if cursor:
                params["start_cursor"] = cursor
            data = self._request("GET", f"/blocks/{page_id}/children", params=params)
            for blk in data.get("results") or []:
                if blk.get("type") == block_type:
                    out[blk[block_type].get(field, "")] = blk["id"]
            if not data.get("has_more"):
                return out
            cursor = data.get("next_cursor")

    # ------------------------------------------------------------------
    def find_row_by_title(self, db_id: str, prop: str, value: str) -> str | None:
        rows = self.query_database(
            db_id, {"property": prop, "title": {"equals": value}}
        )
        return rows[0]["id"] if rows else None

    def upsert_row(self, db_id: str, title_prop: str, title_value: str,
                   props: dict) -> tuple[str, bool]:
        """按 title 唯一键 upsert 一行，返回 (page_id, 是否新建)。"""
        existing = self.find_row_by_title(db_id, title_prop, title_value)
        if existing:
            self.update_page(existing, props)
            return existing, False
        page = self.create_page({"type": "database_id", "database_id": db_id},
                                {title_prop: {"title": [{"text": {"content": title_value}}]}})
        # 创建后再补齐其余属性（title 已在创建时写入）
        rest = {k: v for k, v in props.items() if k != title_prop}
        if rest:
            self.update_page(page["id"], rest)
        return page["id"], True

    def append_blocks(self, page_id: str, children: list[dict]) -> dict:
        """向页面追加内容块（初稿正文写入用）。分批每 100 块。"""
        for i in range(0, len(children), 100):
            self._request("PATCH", f"/blocks/{page_id}/children",
                          {"children": children[i:i + 100]})
        return {"appended": len(children)}

    def get_database(self, db_id: str) -> dict:
        return self._request("GET", f"/databases/{db_id}")

    def add_database_columns(self, db_id: str, props: dict) -> list[str]:
        """只增列升级（幂等）：props 中仅新增库中不存在的属性，绝不删除/修改既有列。

        对齐红线「不删除既有资料」；返回实际新增的属性名列表。
        """
        existing = set(self.get_database(db_id).get("properties", {}).keys())
        missing = {k: v for k, v in props.items() if k not in existing}
        if missing:
            self._request("PATCH", f"/databases/{db_id}", {"properties": missing})
        return sorted(missing.keys())


# ---------------------------------------------------------------------------
# 属性值构造助手（Notion property value 格式）
# ---------------------------------------------------------------------------


def rt(value: str | None) -> dict:
    v = (value or "")[:1900]  # Notion rich_text 单块上限 2000 字符
    return {"rich_text": [{"text": {"content": v}}]}


def url_prop(value: str | None) -> dict:
    return {"url": value or None}


def date_prop(value: str | None) -> dict:
    return {"date": {"start": value} if value else None}


def number_prop(value) -> dict:
    return {"number": value}


def select_prop(option: str | None) -> dict:
    return {"select": {"name": option} if option else None}
