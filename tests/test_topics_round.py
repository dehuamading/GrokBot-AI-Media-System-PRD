"""topics-round 端到端测试（全部 mock：mock LLM / FakeNotion / 本地脱敏样例）。

场景：新技术选题、X 用户需求、AI 副业机会、用户否决/认可、国内数据导入（脱敏模拟）。
断言：停在 GATE-1、≤10 卡、两类都有、需求卡引用评论证据、02 只增且计数=实际条数、
X 卡引用帖子只放 url、偏好注入落本地文件、成本日志标明套餐折算口径。
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.analysis.preference import record_feedback
from src.notion_store.bootstrap import ensure_kb, ensure_kb_upgrades
from src.workflows import run
from tests.fakes import FakeNotionClient

REPO = Path(__file__).resolve().parents[1]
IMPORT_FILE = REPO / "tests" / "fixtures" / "import_sample_masked.json"
COST_BASIS = "opencode-go套餐额度折算(美元,非实付)"


class FakeRoundChat:
    """mock provider_chain：需求分类 / 打分 / 出卡均按 system 关键字分支。"""

    model = "mock-round"
    last_provider = "mock"
    last_model = "mock-round"

    def __init__(self):
        self.calls = 0
        self.seen: list[tuple[str, str]] = []
        self.usage_log = [{
            "provider": "mock", "model": "mock-round",
            "prompt_tokens": 100, "completion_tokens": 50,
            "cost": 0.0, "cost_basis": COST_BASIS,
        }]

    def chat_json(self, system, user, **kw):  # noqa: ARG002
        self.calls += 1
        self.seen.append((system, user))
        if "需求分类" in system:
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
                if "副业" in line or "接单" in line:
                    tags.append("AI副业赚钱")
                if not tags:
                    tags.append("其他")
                results.append({"i": i, "tags": tags, "问题摘要": "mock 需求摘要"})
            return {"results": results}
        if "选题卡" in system:
            return {"cards": [
                {"title": "候选卡一", "why_fit": "mock 适合", "hook": "mock 钩子",
                 "refs": [1], "china": "国内可用", "risk": "mock 风险",
                 "counter": "mock 反证", "difficulty": "低", "platform": "两者",
                 "score": 8},
                {"title": "候选卡二", "why_fit": "mock 适合二", "hook": "mock 钩子二",
                 "refs": [2], "china": "待验证", "risk": "mock 风险二",
                 "counter": "mock 反证二", "difficulty": "中", "platform": "抖音",
                 "score": 7},
            ]}
        lines = [ln for ln in user.splitlines() if ln.startswith("#")]
        return {"results": [{"n": i, "score": 9, "reason": "mock 高相关"}
                            for i in range(1, len(lines) + 1)]}


def _write_fixtures(tmp_path):
    aihot = {
        "items": [{
            "id": "mock-newtech-1",
            "title": "OpenAI 发布新模型：零基础教程，国内可用，免费开源",
            "summary": "新能力降低门槛，手把手步骤可复现",
            "publishedAt": "2026-11-01T09:00:00.000Z",
            "source": {"name": "OpenAI 官方博客"},
            "links": {"aihot": "https://aihot.news/items/mock-newtech-1",
                      "original": "https://example.com/newtech"},
            "score": 9, "category": "ai-models",
        }],
    }
    aihot_path = tmp_path / "aihot.json"
    aihot_path.write_text(json.dumps(aihot, ensure_ascii=False), encoding="utf-8")

    x_posts = {"posts": [
        {
            "id": "mock-demand-post",
            "url": "https://x.com/mock_user/status/1",
            "author_username": "mock_user",
            "author_name": "示例需求用户",
            "created_at": "2026-11-01T08:00:00.000Z",
            "text": "有没有国内能用的免费替代？安装老是报错，求教程",
            "lang": "zh", "likes": 30, "reposts": 2, "replies": 1,
            "quotes": 0, "impressions": 900,
        },
        {
            "id": "mock-side-post",
            "url": "https://x.com/mock_side/status/2",
            "author_username": "mock_side",
            "author_name": "示例副业用户",
            "created_at": "2026-11-01T07:00:00.000Z",
            "text": "AI 副业机会：零基础用 AI 做小程序接单，个人经验分享",
            "lang": "zh", "likes": 20, "reposts": 1, "replies": 0,
            "quotes": 0, "impressions": 500,
        },
    ]}
    x_path = tmp_path / "x_posts.json"
    x_path.write_text(json.dumps(x_posts, ensure_ascii=False), encoding="utf-8")

    replies = {
        "data": [],
        "includes": {"replies": [{
            "id": "reply-1",
            "conversation_id": "mock-demand-post",
            "text": "国内能用吗？需要梯子吗？@alice https://example.com/x "
                    "手机13800138000 邮箱a@b.com",
            "created_at": "2026-11-01T08:30:00.000Z",
            "public_metrics": {"like_count": 1},
        }]},
        "meta": {},
    }
    replies_path = tmp_path / "x_replies.json"
    replies_path.write_text(json.dumps(replies, ensure_ascii=False), encoding="utf-8")
    return aihot_path, x_path, replies_path


def _seed_preferences(client, db_ids):
    record_feedback(client, db_ids, {
        "topic_id": "TX-OLD-1", "原始选题": "国内 AI 工具教程",
        "推荐理由": "受众痛点明确", "用户选择": "认可",
        "原话反馈": "这个方向对，继续做", "原因标签": "国内可用",
        "轮次": 1, "下轮参考": "多做教程", "time": "2026-11-01T12:00:00+00:00",
    })
    record_feedback(client, db_ids, {
        "topic_id": "TX-OLD-2", "原始选题": "空投撸毛",
        "推荐理由": "互动高", "用户选择": "否决",
        "原话反馈": "和我的受众无关", "原因标签": "受众无关",
        "轮次": 2, "下轮参考": "避免空投", "time": "2026-11-01T13:00:00+00:00",
    })


def test_topics_round_end_to_end_mock_stops_at_gate1(tmp_path, capsys):
    aihot_path, x_path, replies_path = _write_fixtures(tmp_path)
    client = FakeNotionClient()
    db_ids = ensure_kb(client, "root")
    ensure_kb_upgrades(client, db_ids)
    _seed_preferences(client, db_ids)
    chat = FakeRoundChat()

    args = SimpleNamespace(
        state=str(tmp_path / "state.json"), page_id="root",
        sources="aihot,x,import", x_file=str(x_path),
        x_replies_file=str(replies_path), aihot_file=str(aihot_path),
        import_file=str(IMPORT_FILE), github_top=0, aihot_limit=20,
        official_limit=3, max_cards=10, round_id="ROUND-1",
        write_notion=True, dry_run=False, live=False,
        config=str(REPO / "config" / "sources.example.yaml"),
        audience=str(REPO / "config" / "audience_profile.yaml"),
        shortlist=60, analyze="llm", x_following=None,
    )
    assert run.cmd_topics_round(args, client=client, chat=chat) == 0
    out = capsys.readouterr().out
    assert "GATE-1" in out and "未进入文案" in out

    # 停在 GATE-1：不生成任何文案
    assert client.databases["04_文案与版本"]["rows"] == []

    rows = client.query_database(db_ids["03_选题决策"])
    assert 1 <= len(rows) <= 10
    types = {FakeNotionClient.plain(r["properties"]["选题类型"]) for r in rows}
    assert types == {"需求型", "机会型"}

    for r in rows:
        props = r["properties"]
        assert FakeNotionClient.plain(props["topic_id"]).startswith("TX-")
        assert re.search(r"-V\d{2}$", FakeNotionClient.plain(props["topic_id"]))
        assert FakeNotionClient.plain(props["用户选择结果"]) == "待用户选择"
        assert FakeNotionClient.plain(props["评分版本"]) == "v3-dual"
        assert isinstance(FakeNotionClient.plain(props["轨道得分"]), int)
        assert FakeNotionClient.plain(props["副业证据等级"])
        assert "假设" in FakeNotionClient.plain(props["传播潜力假设(不计分)"])
        assert FakeNotionClient.plain(props["原始来源"]).startswith("http")

    # 需求型卡必须引用脱敏后的评论证据；X 卡引用帖子只放 url（不写作者名）
    demand_rows = [r for r in rows if FakeNotionClient.plain(
        r["properties"]["选题类型"]) == "需求型"]
    assert demand_rows
    for r in demand_rows:
        ev = FakeNotionClient.plain(r["properties"]["需求证据"])
        assert "评论证据" in ev
        assert "@alice" not in ev and "13800138000" not in ev
        assert "a@b.com" not in ev and "https://example.com/x" not in ev
        cited = FakeNotionClient.plain(r["properties"]["引用帖子"])
        assert cited.startswith("http") and "示例需求用户" not in cited

    # 新技术机会（aihot）与 AI 副业机会（X）均有机会型卡来源
    cited_all = " ".join(FakeNotionClient.plain(r["properties"]["引用帖子"])
                         for r in rows)
    assert "example.com/newtech" in cited_all
    assert "x.com/mock_side/status/2" in cited_all

    # 02_用户需求：只增、每类一行、计数=实际评论条数、证据脱敏
    demand_rows_02 = client.query_database(db_ids["02_用户需求"])
    assert demand_rows_02
    for r in demand_rows_02:
        props = r["properties"]
        problem = FakeNotionClient.plain(props["problem"])
        sample = FakeNotionClient.plain(props["sample_limit"])
        assert "实际评论" in problem and "条" in problem
        assert "计数=" in sample
        assert "@alice" not in sample and "13800138000" not in sample
    total_counts = sum(int(re.search(r"实际评论 (\d+) 条", FakeNotionClient.plain(
        r["properties"]["problem"])).group(1)) for r in demand_rows_02)
    # 实际参与分类的评论：X 1 条 + 导入 5 条 = 6 条
    assert total_counts == 6

    # 偏好注入：打印 + 全文落本地 state 目录
    injection_files = list(tmp_path.glob("preference_injection_*.txt"))
    assert len(injection_files) == 1
    injection = injection_files[0].read_text(encoding="utf-8")
    assert "近期认可示例" in injection and "近期否决示例" in injection
    assert "候选规则" in injection and "不得作为硬规则" in injection
    assert any("近期认可示例" in user or "近期认可示例" in system
               for system, user in chat.seen)

    # 成本日志：套餐额度折算口径 + 未知不冒充 0
    log = (tmp_path / "cost_log.jsonl").read_text(encoding="utf-8").splitlines()
    record = json.loads(log[-1])
    assert record["command"].startswith("topics-round")
    assert record["cost_basis"] == COST_BASIS
    assert "套餐额度折算" in record["成本口径"]
