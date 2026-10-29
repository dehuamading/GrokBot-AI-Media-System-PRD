"""测试用 Fake Notion 客户端：内存实现与真实客户端同接口（仅本仓库用到的方法）。

不发起任何网络请求；帮助断言「无发布路径」等红线。
"""

from __future__ import annotations


class FakeNotionClient:
    def __init__(self):
        self.databases: dict[str, dict] = {}   # name -> {"id","properties","rows":[]}
        self.pages: dict[str, dict] = {}       # page_id -> {"parent_db","properties","children"}
        self.calls: list[str] = []
        self._n = 0

    # ------------------------------------------------------------------ helpers
    def _new_id(self) -> str:
        self._n += 1
        return f"fake-id-{self._n:06d}"

    @staticmethod
    def plain(prop: dict) -> str:
        t = prop.get("type")
        if t == "title":
            return "".join(x["text"]["content"] for x in prop.get("title", []))
        if t == "rich_text":
            return "".join(x["text"]["content"] for x in prop.get("rich_text", []))
        if t == "select":
            return (prop.get("select") or {}).get("name", "")
        if t == "number":
            return prop.get("number")
        if t == "url":
            return prop.get("url") or ""
        if t == "date":
            return (prop.get("date") or {}).get("start", "") if prop.get("date") else ""
        return ""

    @staticmethod
    def _norm_prop(prop: dict) -> dict:
        """把写入格式的属性补上 type 字段，模拟真实 API 返回形态。"""
        for t in ("title", "rich_text", "select", "number", "url", "date", "checkbox"):
            if t in prop:
                return {"type": t, t: prop[t]}
        return prop

    # ------------------------------------------------------------------ pages/dbs
    def get_page(self, page_id):
        self.calls.append("get_page")
        return {"id": page_id, "url": f"https://www.notion.so/{page_id}"}

    def child_databases(self, page_id):
        self.calls.append("child_databases")
        return {name: db["id"] for name, db in self.databases.items()}

    def create_database(self, parent_page_id, title, properties, icon=None):
        self.calls.append("create_database")
        if title in self.databases:
            raise ValueError(f"库已存在：{title}")
        db_id = self._new_id()
        self.databases[title] = {"id": db_id, "properties": dict(properties), "rows": []}
        return {"id": db_id}

    def get_database(self, db_id):
        for db in self.databases.values():
            if db["id"] == db_id:
                return {"id": db_id, "properties": db["properties"]}
        raise KeyError(db_id)

    def add_database_columns(self, db_id, props):
        self.calls.append("add_database_columns")
        for db in self.databases.values():
            if db["id"] == db_id:
                missing = {k: v for k, v in props.items() if k not in db["properties"]}
                db["properties"].update(missing)
                return sorted(missing.keys())
        raise KeyError(db_id)

    def create_page(self, parent, properties, children=None):
        self.calls.append("create_page")
        pid = self._new_id()
        self.pages[pid] = {"parent_db": parent.get("database_id"),
                           "properties": {k: self._norm_prop(v)
                                          for k, v in properties.items()},
                           "children": list(children or [])}
        db = self._db(parent.get("database_id"))
        db["rows"].append(pid)
        return {"id": pid}

    def update_page(self, page_id, properties):
        self.calls.append("update_page")
        self.pages[page_id]["properties"].update(
            {k: self._norm_prop(v) for k, v in properties.items()})
        return {"id": page_id}

    def append_blocks(self, page_id, children):
        self.calls.append("append_blocks")
        self.pages[page_id]["children"].extend(children)
        return {"appended": len(children)}

    def _db(self, db_id):
        for db in self.databases.values():
            if db["id"] == db_id:
                return db
        raise KeyError(db_id)

    # ------------------------------------------------------------------ query
    def query_database(self, db_id, filter_=None, page_size=100):
        self.calls.append("query_database")
        db = self._db(db_id)
        rows = [{"id": pid, "properties": self.pages[pid]["properties"]}
                for pid in db["rows"]]
        if filter_:
            prop = filter_["property"]
            if "select" in filter_:
                want = filter_["select"]["equals"]
                rows = [r for r in rows
                        if (r["properties"].get(prop, {}).get("select") or {}).get("name") == want]
        return rows

    def find_row_by_title(self, db_id, prop, value):
        for r in self.query_database(db_id):
            if self.plain(r["properties"].get(prop, {})) == value:
                return r["id"]
        return None

    def upsert_row(self, db_id, title_prop, title_value, props):
        self.calls.append("upsert_row")
        existing = self.find_row_by_title(db_id, title_prop, title_value)
        if existing:
            self.update_page(existing, props)
            return existing, False
        page = self.create_page({"type": "database_id", "database_id": db_id},
                                {title_prop: {"title": [{"text": {"content": title_value}}]}})
        rest = {k: v for k, v in props.items() if k != title_prop}
        if rest:
            self.update_page(page["id"], rest)
        return page["id"], True

    # ------------------------------------------------------------------ helpers for tests
    def row(self, db_name, title_prop, title_value) -> dict:
        db = self.databases[db_name]
        for pid in db["rows"]:
            p = self.pages[pid]["properties"]
            if self.plain(p.get(title_prop, {})) == title_value:
                return self.pages[pid]
        raise KeyError(f"{db_name} 中无 {title_value}")

    def set_select(self, db_name, title_prop, title_value, prop, value):
        page = self.row(db_name, title_prop, title_value)
        page["properties"][prop] = {"type": "select", "select": {"name": value}}
