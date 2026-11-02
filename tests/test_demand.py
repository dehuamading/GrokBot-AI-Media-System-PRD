"""需求引擎测试（全部 mock，无真实 LLM / 网络 / Notion 调用）。

覆盖：LLM 正常分类、LLM 失败回退关键词、计数只等于真实评论条数、
证据脱敏（@用户名/URL/手机号/邮箱）与长度/条数上限。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.analysis.demand import (ALL_CATEGORIES, DEMAND_CATEGORIES,
                                 aggregate_demands, classify_demands,
                                 sanitize_evidence)


class FakeDemandChat:
    """mock LLM：按评论行返回固定标签，不联网。"""

    model = "mock-demand"

    def __init__(self, fail: bool = False):
        self.fail = fail
        self.calls = 0

    def chat_json(self, system, user, **kw):  # noqa: ARG002
        self.calls += 1
        if self.fail:
            raise RuntimeError("mock LLM failure")
        lines = [ln for ln in user.splitlines() if ln.startswith("#")]
        results = []
        for i, line in enumerate(lines, start=1):
            tags = []
            if "国内" in line or "梯子" in line:
                tags.append("国内使用")
            if "免费" in line or "替代" in line:
                tags.append("免费替代")
            if "安装" in line or "报错" in line:
                tags.append("安装部署")
            if not tags:
                tags.append("其他")
            results.append({"i": i, "tags": tags, "问题摘要": "mock 摘要"})
        return {"results": results}


def test_demand_categories_are_ten_plus_other():
    assert len(DEMAND_CATEGORIES) == 10
    assert "其他" in ALL_CATEGORIES
    assert {"安装部署", "注册付款", "国内使用", "免费替代", "AI编程",
            "AI内容创作", "AI副业赚钱", "工作效率", "工具比较",
            "故障排查"} == set(DEMAND_CATEGORIES)


def test_classify_demands_llm_normal_marks_engine_and_summary():
    comments = [
        {"text": "国内能用吗？有没有免费替代", "source_url": "https://example.com/a"},
        {"text": "安装一直报错怎么办", "source_url": "https://example.com/b"},
    ]
    out = classify_demands(comments, chat=FakeDemandChat())
    assert len(out) == 2
    assert out[0]["engine"] == "llm:mock-demand"
    assert "国内使用" in out[0]["tags"] and "免费替代" in out[0]["tags"]
    assert out[1]["tags"] == ["安装部署"]
    assert out[0]["summary"] == "mock 摘要"
    assert out[0]["source_url"] == "https://example.com/a"


def test_classify_demands_llm_failure_falls_back_to_keywords():
    comments = [{"text": "国内能用吗？安装报错，想找免费替代"}]
    out = classify_demands(comments, chat=FakeDemandChat(fail=True))
    assert out[0]["engine"] == "keyword"
    assert {"国内使用", "免费替代"} <= set(out[0]["tags"])
    assert "安装部署" in out[0]["tags"] or "故障排查" in out[0]["tags"]


def test_classify_demands_batches_at_most_40_comments():
    chat = FakeDemandChat()
    comments = [{"text": f"评论 {i} 安装报错"} for i in range(85)]
    out = classify_demands(comments, chat=chat)
    assert len(out) == 85
    assert chat.calls == 3  # 40 + 40 + 5


def test_aggregate_count_equals_real_comment_count():
    items = [
        {"text": "安装失败", "tags": ["安装部署"], "engine": "keyword",
         "source_url": "https://example.com/1"},
        {"text": "怎么安装", "tags": ["安装部署", "故障排查"], "engine": "keyword",
         "source_url": "https://example.com/2"},
        {"text": "安装要多久", "tags": ["安装部署"], "engine": "keyword",
         "source_url": "https://example.com/3"},
        {"text": "国内能用吗", "tags": ["国内使用"], "engine": "keyword",
         "source_url": "https://example.com/4"},
    ]
    agg = aggregate_demands(items)
    by = {a["category"]: a for a in agg}
    assert by["安装部署"]["count"] == 3
    assert by["故障排查"]["count"] == 1
    assert by["国内使用"]["count"] == 1
    # 不估算、不放大：每类 count 等于带该标签的真实评论条数
    assert sum(1 for i in items if "安装部署" in i["tags"]) == by["安装部署"]["count"]


def test_evidence_is_sanitized_short_and_limited():
    text = ("@alice 看 https://x.com/a?b=1 手机 13800138000 "
            "邮箱 alice@example.com 安装一直报错，求国内能用的免费替代方案")
    items = [{"text": text, "tags": ["安装部署"], "engine": "keyword",
              "source_url": "https://example.com/source"} for _ in range(7)]
    agg = aggregate_demands(items)
    rec = next(a for a in agg if a["category"] == "安装部署")
    assert rec["count"] == 7
    assert len(rec["evidence"]) <= 5
    assert len(rec["evidence_short"]) <= 3
    for ev in rec["evidence"]:
        assert len(ev["text"]) <= 60
        assert ev["source_url"] == "https://example.com/source"
        assert "@alice" not in ev["text"]
        assert "https://" not in ev["text"]
        assert "13800138000" not in ev["text"]
        assert "alice@example.com" not in ev["text"]
    cleaned = sanitize_evidence(text)
    assert len(cleaned) <= 60
    assert "安装" in cleaned


def test_aggregate_handles_unclassified_raw_comments_with_keyword_fallback():
    agg = aggregate_demands([{"text": "想靠 AI 副业赚钱，怎么接单",
                              "source_url": "https://example.com/z"}])
    assert any(a["category"] == "AI副业赚钱" for a in agg)
