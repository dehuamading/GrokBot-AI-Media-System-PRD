"""官方源连接器测试（mock requests）：RSS/Atom/GitHub Releases/单源失败隔离。"""

from __future__ import annotations

import sys
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.connectors import official_source
from src.connectors.official_source import fetch_official


class FakeResponse:
    def __init__(self, status=200, payload=None, text=""):
        self.status_code = status
        self._payload = payload
        self.text = text

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")


RSS = """<?xml version="1.0"?>
<rss version="2.0"><channel>
<item><title>示例新闻一（假数据）</title><link>https://example.com/news/1?utm_source=rss</link>
<pubDate>Tue, 28 Oct 2026 10:00:00 GMT</pubDate><description>摘要一</description></item>
<item><title>示例新闻二（假数据）</title><link>https://example.com/news/2</link>
<pubDate>Mon, 27 Oct 2026 10:00:00 GMT</pubDate><description>摘要二</description></item>
<item><title>示例新闻三（假数据）</title><link>https://example.com/news/3</link>
<pubDate>Sun, 26 Oct 2026 10:00:00 GMT</pubDate><description>摘要三</description></item>
<item><title>示例新闻四（假数据）</title><link>https://example.com/news/4</link>
<pubDate>Sat, 25 Oct 2026 10:00:00 GMT</pubDate><description>摘要四</description></item>
</channel></rss>"""

ATOM = """<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
<entry><title>Atom 示例（假数据）</title>
<link rel="alternate" href="https://example.com/atom/1"/>
<published>2026-10-28T10:00:00Z</published><summary>Atom 摘要</summary></entry>
<entry><title>Atom 示例二（假数据）</title>
<link href="https://example.com/atom/2"/>
<updated>2026-10-27T10:00:00Z</updated><content>Atom 内容</content></entry>
</feed>"""

RELEASES = [
    {"tag_name": "v2.0.0", "name": "v2.0.0", "html_url":
     "https://github.com/example/rel/releases/tag/v2.0.0",
     "published_at": "2026-10-28T10:00:00Z", "body": "release notes"},
    {"tag_name": "v1.0.0", "name": "v1.0.0", "html_url":
     "https://github.com/example/rel/releases/tag/v1.0.0",
     "published_at": "2026-09-01T10:00:00Z", "body": "old notes"},
]


def _fake_get(text=RSS, fail_urls=()):
    def fake(url, params=None, headers=None, timeout=None, **kw):
        if any(f in url for f in fail_urls):
            raise RuntimeError("network boom")
        if url.endswith(".xml") or "rss" in url or "atom" in url:
            return FakeResponse(text=text)
        if "/releases" in url:
            return FakeResponse(payload=RELEASES)
        return FakeResponse(status=404, payload={})
    return fake


def test_rss_parsing_and_limit(monkeypatch):
    monkeypatch.setattr(official_source.requests, "get", _fake_get())
    sources = [{"id": "demo_rss", "name": "示例官方博客",
                "type": "rss", "url": "https://example.com/news.xml", "limit": 3}]
    out = fetch_official(sources)
    assert out["api_calls"] == 1
    assert len(out["items"]) == 3
    it = out["items"][0]
    assert it["platform"] == "official"
    assert it["source_type"] == "official"
    assert it["origin_agent"] == "pi"
    assert it["author_or_publisher"] == "示例官方博客"
    assert it["source_url"] == "https://example.com/news/1?utm_source=rss"
    assert it["published_at"].startswith("2026-10-28")
    assert it["license_status"] == "公开"


def test_atom_parsing(monkeypatch):
    monkeypatch.setattr(official_source.requests, "get", _fake_get(text=ATOM))
    sources = [{"id": "demo_atom", "name": "示例 Atom",
                "type": "rss", "url": "https://example.com/atom.xml", "limit": 2}]
    out = fetch_official(sources)
    assert len(out["items"]) == 2
    assert out["items"][0]["source_url"] == "https://example.com/atom/1"
    assert out["items"][0]["title_or_summary"].startswith("Atom 示例")


def test_github_releases_parsing(monkeypatch):
    monkeypatch.setattr(official_source.requests, "get", _fake_get())
    sources = [{"id": "demo_rel", "name": "示例 Releases",
                "type": "github_releases", "owner": "example", "repo": "rel",
                "limit": 2}]
    out = fetch_official(sources)
    assert out["api_calls"] == 1
    assert len(out["items"]) == 2
    it = out["items"][0]
    assert it["extra"]["official"]["tag"] == "v2.0.0"
    assert it["source_url"].endswith("/v2.0.0")
    assert it["author_or_publisher"] == "示例 Releases"


def test_one_source_failure_does_not_break_others(monkeypatch):
    monkeypatch.setattr(official_source.requests, "get",
                        _fake_get(fail_urls=("bad.example.com",)))
    sources = [
        {"id": "bad", "name": "坏源", "type": "rss",
         "url": "https://bad.example.com/rss", "limit": 3},
        {"id": "good", "name": "好源", "type": "rss",
         "url": "https://example.com/news.xml", "limit": 2},
    ]
    out = fetch_official(sources)
    assert len(out["items"]) == 2
    assert out["per_source"]["bad"]["items"] == 0
    assert out["per_source"]["good"]["items"] == 2
    assert out["errors"] and "坏源" in out["errors"][0]["source"]


RSS_ZH = """<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0"><channel>
<item><title>中文标题：免费本地运行教程</title><link>https://example.com/zh/1</link>
<pubDate>Tue, 28 Oct 2026 10:00:00 GMT</pubDate><description>中文摘要内容</description></item>
</channel></rss>"""

RSS_EMPTY = """<?xml version="1.0"?>
<rss version="2.0"><channel><title>空源</title></channel></rss>"""


class CharsetlessResponse:
    """模拟 requests 在响应头无 charset 时默认 ISO-8859-1 的行为。"""

    def __init__(self, content: bytes):
        self.content = content
        self.status_code = 200
        self.encoding = None
        self.apparent_encoding = "utf-8"

    @property
    def text(self):
        return self.content.decode(self.encoding or "ISO-8859-1")

    def raise_for_status(self):
        return None


def test_rss_without_charset_decodes_chinese_as_utf8(monkeypatch):
    def fake_get(url, params=None, headers=None, timeout=None, **kw):
        return CharsetlessResponse(RSS_ZH.encode("utf-8"))

    monkeypatch.setattr(official_source.requests, "get", fake_get)
    sources = [{"id": "zh", "name": "中文源", "type": "rss",
                "url": "https://example.com/zh.xml", "limit": 1}]
    out = fetch_official(sources)
    assert out["items"][0]["title_or_summary"].startswith("中文标题")


def test_rss_zero_items_records_error(monkeypatch):
    monkeypatch.setattr(official_source.requests, "get",
                        _fake_get(text=RSS_EMPTY))
    sources = [{"id": "empty", "name": "空源", "type": "rss",
                "url": "https://example.com/empty.xml", "limit": 3}]
    out = fetch_official(sources)
    assert out["items"] == []
    assert any("解析到0条" in e["error"] for e in out["errors"])
