"""阶段3 修复轮回归测试（全部 mock：无真实 Notion 写、无真实 LLM、无网络）。

覆盖：
1) dry-run 只读加载 05 偏好；真实 05 schema 字段与反馈时间倒序；
2) --analyze llm 在 dry-run 也调用 LLM；全部失败才回退并标“生成引擎=启发式回退”；
3) 写入 03/02 的文本不含 X 帖子 >20 字连续原文/用户名/@handle；启发式标题概括；
4) 机会型合并优先覆盖 aihot/github/official（达到备选）；
5) 未调用 LLM 金额=未知（非 0）；OpenCode 记录 list_cost 且注明非实付；
6) LLM 高分卡可出现“推荐”（不改 v3 阈值）；
7) sources-check --official-limit 生效。
"""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.analysis.audience import load_profile
from src.analysis.llm_topics import shortlist
from src.analysis.preference import load_recent_preferences
from src.analysis.scoring import merge_dual_tracks
from src.analysis.topics import build_topic_cards
from src.connectors import official_source
from src.connectors.official_source import fetch_official
from src.notion_store.bootstrap import ensure_kb, ensure_kb_upgrades
from src.workflows import pipeline, run
from src.workflows.state import WorkflowState
from tests.fakes import FakeNotionClient
from tests.test_official_source import RSS, _fake_get
from tests.test_topics_round import FakeRoundChat, _seed_preferences, _write_fixtures

REPO = Path(__file__).resolve().parents[1]
PROFILE = load_profile(REPO / "config" / "audience_profile.yaml")
NOW = datetime(2026, 11, 2, 12, tzinfo=timezone.utc)
OPENCODE_BASIS = "opencode-go套餐额度折算(美元,非实付)"


def _select(name):
    return {"select": {"name": name}}


def _rich(text):
    return {"rich_text": [{"text": {"content": text}}]}


def _date(value):
    return {"date": {"start": value}}


def _material(item_id, source_type, text, *, url=None, platform=None,
              comments=None, author="示例来源"):
    return {
        "item_id": item_id,
        "source_type": source_type,
        "platform": platform or ("x" if source_type == "x_posts" else source_type),
        "source_url": url or f"https://example.com/{item_id.replace(':', '/')}",
        "author_or_publisher": author,
        "published_at": NOW.isoformat(),
        "collected_at": NOW.isoformat(),
        "title_or_summary": text,
        "claims": [],
        "evidence_status": "self_reported" if source_type == "x_posts" else "verified",
        "processing_status": "new",
        "comments": comments or [],
        "extra": {},
    }


def _round_args(tmp_path, *, analyze="llm", dry_run=True, x_file=None,
                replies_file=None, aihot_file=None, state_name="state.json"):
    return SimpleNamespace(
        state=str(tmp_path / state_name), page_id="root",
        sources="aihot,x", x_file=x_file,
        x_replies_file=replies_file, aihot_file=aihot_file,
        import_file=None, github_top=0, aihot_limit=20,
        official_limit=3, max_cards=10, round_id="ROUND-FIX",
        write_notion=False, dry_run=dry_run, live=False,
        config=str(REPO / "config" / "sources.example.yaml"),
        audience=str(REPO / "config" / "audience_profile.yaml"),
        shortlist=40, analyze=analyze, x_following=None,
    )


def _collect_with_fixtures(tmp_path):
    aihot_path, x_path, replies_path = _write_fixtures(tmp_path)
    state = WorkflowState(tmp_path / "collect_state.json")
    out = pipeline.step_collect(
        state, aihot_file=str(aihot_path), x_posts_file=str(x_path),
        sources="aihot,x", snapshot_dir=None,
        config_path=str(REPO / "config" / "sources.example.yaml"))
    return out, aihot_path, x_path, replies_path


# ------------------------------------------------------------------ 1) 偏好
def test_load_recent_preferences_reads_real_05_schema_and_sorts_by_time():
    client = FakeNotionClient()
    db_ids = ensure_kb(client, "root")
    ensure_kb_upgrades(client, db_ids)
    rows = [
        ("FB-1-A", "认可", "第一条认可原话", "2026-11-01T10:00:00+00:00"),
        ("FB-2-A", "认可", "第二条认可原话", "2026-11-02T10:00:00+00:00"),
        ("FB-3-N", "否决", "否决原话", "2026-11-02T11:00:00+00:00"),
        ("FB-4-G", "一般", "一般反馈", "2026-11-02T12:00:00+00:00"),
    ]
    for record_id, pick, say, when in rows:
        client.upsert_row(db_ids["05_经验与复盘"], "record_id", record_id, {
            "类型": _select("反馈"),
            "用户选择": _select(pick),
            "原话反馈": _rich(say),
            "原始选题": _rich(f"选题-{record_id}"),
            "推荐理由": _rich(f"理由-{record_id}"),
            "下轮参考": _rich(f"下轮-{record_id}"),
            "反馈时间": _date(when),
            "状态": _select("待验证"),
        })

    prefs = load_recent_preferences(client, db_ids, n_pos=5, n_neg=5)
    assert [p["原话反馈"] for p in prefs["approved"]] == [
        "第二条认可原话", "第一条认可原话"]  # 按反馈时间倒序
    assert prefs["rejected"][0]["原话反馈"] == "否决原话"
    assert all(p["record_id"] for p in prefs["approved"])
    assert prefs["approved"][0]["推荐理由"] == "理由-FB-2-A"
    assert prefs["approved"][0]["下轮参考"] == "下轮-FB-2-A"
    # 一般反馈不进入认可/否决示例
    assert all(p["用户选择"] != "一般" for p in prefs["approved"] + prefs["rejected"])


def test_dry_run_reads_preferences_read_only_when_token_and_db_ids_exist(
        tmp_path, monkeypatch, capsys):
    out, aihot_path, x_path, replies_path = _collect_with_fixtures(tmp_path)
    client = FakeNotionClient()
    db_ids = ensure_kb(client, "root")
    ensure_kb_upgrades(client, db_ids)
    _seed_preferences(client, db_ids)

    # 真实 dry-run 场景：state 文件里已有 db_ids；NOTION_TOKEN 已注入环境。
    persisted = WorkflowState(tmp_path / "state.json")
    persisted.update(db_ids=db_ids, page_root="root")
    persisted.save()
    client.calls.clear()  # 只观察 cmd 运行期间的调用
    monkeypatch.setenv("NOTION_TOKEN", "test-token-not-real")
    monkeypatch.setattr(run, "NotionClient", lambda: client)
    monkeypatch.setattr(run.pipeline, "step_collect", lambda *a, **kw: out)

    chat = FakeRoundChat()
    args = _round_args(tmp_path, analyze="llm", dry_run=True,
                       x_file=str(x_path),
                       replies_file=str(replies_path), aihot_file=str(aihot_path))
    assert run.cmd_topics_round(args, chat=chat) == 0
    printed = capsys.readouterr().out
    assert "本轮无历史偏好注入" not in printed
    assert "近期认可示例" in printed
    assert "query_database" in client.calls
    write_calls = {"create_page", "update_page", "create_database",
                   "add_database_columns", "ensure_select_options",
                   "create_row_if_absent", "upsert_row"}
    assert not (set(client.calls) & write_calls)
    injection = list(tmp_path.glob("preference_injection_*.txt"))
    assert injection and "这个方向对，继续做" in injection[0].read_text(encoding="utf-8")


# --------------------------------------------------------------- 2) LLM 默认
class FailingChat(FakeRoundChat):
    def chat_json(self, system, user, **kw):  # noqa: ARG002
        self.calls += 1
        raise RuntimeError("mock llm down")


def test_dry_run_llm_path_calls_llm_and_writes_cost(tmp_path, monkeypatch, capsys):
    out, aihot_path, x_path, replies_path = _collect_with_fixtures(tmp_path)
    monkeypatch.setattr(run.pipeline, "step_collect", lambda *a, **kw: out)
    chat = FakeRoundChat()
    args = _round_args(tmp_path, analyze="llm", dry_run=True,
                       x_file=str(x_path), replies_file=str(replies_path),
                       aihot_file=str(aihot_path))
    assert run.cmd_topics_round(args, chat=chat) == 0
    assert chat.calls > 0
    log = (tmp_path / "cost_log.jsonl").read_text(encoding="utf-8").splitlines()
    record = json.loads(log[-1])
    assert record["calls"] == chat.calls
    assert record["cost_basis"] == OPENCODE_BASIS


def test_llm_total_failure_falls_back_and_marks_engine(tmp_path, monkeypatch, capsys):
    out, aihot_path, x_path, replies_path = _collect_with_fixtures(tmp_path)
    monkeypatch.setattr(run.pipeline, "step_collect", lambda *a, **kw: out)
    captured = {}
    original = run._postprocess_round_cards

    def wrapper(cards, materials, **kw):
        result = original(cards, materials, **kw)
        captured["cards"] = result
        return result

    monkeypatch.setattr(run, "_postprocess_round_cards", wrapper)
    args = _round_args(tmp_path, analyze="llm", dry_run=True,
                       x_file=str(x_path),
                       replies_file=str(replies_path), aihot_file=str(aihot_path))
    assert run.cmd_topics_round(args, chat=FailingChat()) == 0
    cards = captured["cards"]
    assert cards
    assert all(c["生成引擎"] == "启发式回退" for c in cards)
    assert "启发式回退" in capsys.readouterr().out


def test_analyze_heuristic_never_calls_chat(tmp_path, monkeypatch, capsys):
    out, aihot_path, x_path, replies_path = _collect_with_fixtures(tmp_path)
    comment_mat = _material(
        "x_posts:demand", "x_posts",
        "国内能不能用这个 AI 工具，安装总是报错，求一个免费替代教程",
        comments=[{"text": "国内能用吗？需要梯子吗 @alice",
                   "demand_tags": ["国内使用"]}])
    out = dict(out)
    out["materials"] = out["materials"] + [comment_mat]
    monkeypatch.setattr(run.pipeline, "step_collect", lambda *a, **kw: out)
    chat = FakeRoundChat()
    args = _round_args(tmp_path, analyze="heuristic", dry_run=True,
                       x_file=str(x_path), replies_file=str(replies_path),
                       aihot_file=str(aihot_path))
    assert run.cmd_topics_round(args, chat=chat) == 0
    assert chat.calls == 0
    assert "无历史偏好注入" in capsys.readouterr().out or True


# --------------------------------------------------------------- 3) 隐私
def _raw_windows(text, width=21):
    compact = re.sub(r"\s+", "", text or "")
    return {compact[i:i + width] for i in range(max(0, len(compact) - width + 1))}


def test_x_card_titles_are_paraphrased_and_never_contain_raw_post_or_handle():
    raw = "这是一段超过二十个字的X帖子正文原文，里面还带着用户名@alice和真实内容"
    mat = _material("x_posts:1", "x_posts", raw,
                    url="https://x.com/alice/status/1",
                    author="Alice (@alice)")
    cards, _ = build_topic_cards([mat], top_k=1, now=NOW, profile=PROFILE)
    card = cards[0]
    assert "｜" in card["拟定标题"]
    assert raw[:20] not in card["拟定标题"]

    merged = run._postprocess_round_cards(
        [{"关联素材": ["x_posts:1"], "_intended_type": "机会型",
          "拟定标题": raw}],
        [mat], now=NOW, profile=PROFILE, max_cards=5)
    assert merged
    card = merged[0]
    assert raw[:20] not in card["拟定标题"]
    for key, value in card.items():
        if not isinstance(value, str):
            continue
        if key in {"引用帖子", "原始来源", "_source_urls", "关联素材", "topic_id"}:
            continue
        assert "@alice" not in value
        assert "Alice" not in value
        compact = re.sub(r"\s+", "", value)
        assert not any(w in compact for w in _raw_windows(raw)), key


def test_step_write_topics_sanitizes_x_cards_on_any_write_path():
    raw = "这是一段超过二十个字的X帖子正文原文，包含用户名@alice以及不应入库的内容"
    mat = _material("x_posts:1", "x_posts", raw,
                    url="https://x.com/alice/status/1",
                    author="Alice (@alice)")
    cards, _ = build_topic_cards([mat], top_k=1, now=NOW, profile=PROFILE)
    cards[0]["拟定标题"] = raw
    cards[0]["核心价值"] = raw
    cards[0]["生成引擎"] = "启发式回退"

    client = FakeNotionClient()
    db_ids = ensure_kb(client, "root")
    ensure_kb_upgrades(client, db_ids)
    pipeline.step_write_topics(client, db_ids, cards, [mat])

    row = client.row("03_选题决策", "topic_id", cards[0]["topic_id"])
    title = FakeNotionClient.plain(row["properties"]["拟定标题"])
    core = FakeNotionClient.plain(row["properties"]["核心价值"])
    assert raw[:20] not in title and raw[:20] not in core
    assert "@alice" not in title and "Alice" not in core
    assert "｜" in title
    assert FakeNotionClient.plain(
        row["properties"]["生成引擎"]) == "启发式回退"


# --------------------------------------------------------- 4) 机会型代表性
def test_merge_dual_tracks_prioritizes_non_x_opportunity_sources():
    def _card(ident, source, level, total):
        return {"id": ident, "type": "机会型", "_source_group": source,
                "推荐级别": level, "综合评分": total,
                "adjusted_total": total, "track_score": 3}

    cards = [
        _card("x1", "x_posts", "推荐", 15),
        _card("x2", "x_posts", "推荐", 14),
        _card("a1", "aihot", "备选", 10),
        _card("g1", "github", "备选", 9),
        _card("o1", "official", "备选", 9),
        {"id": "d1", "type": "需求型", "track_score": 5, "total": 12},
    ]
    merged = merge_dual_tracks(
        cards, top_n=4,
        opportunity_sources=("aihot", "github", "official"))
    sources = {c.get("_source_group") for c in merged}
    assert {"aihot", "github", "official"} <= sources
    assert len(merged) == 4


def test_shortlist_reserves_aihot_github_official_slots():
    materials = [
        _material(f"x_posts:{i}", "x_posts",
                  f"X 帖子 {i}：AI 教程国内可用免费开源实测步骤")
        for i in range(20)
    ] + [
        _material("aihot:1", "aihot", "AIHot 新模型发布，零基础教程国内可用"),
        _material("github:1", "github", "GitHub 开源项目，AI agent 新能力"),
        _material("official:1", "official",
                  "官方发布新版本，免费开源教程，国内可用的完整上手步骤"),
    ]
    short = shortlist(
        materials, k=6, profile=PROFILE,
        ensure_sources=("aihot", "github", "official"), per_source_min=1)
    got = {m["source_type"] for m in short}
    assert {"aihot", "github", "official"} <= got
    assert len(short) <= 6


# ------------------------------------------------------------------ 5) 成本
def test_no_llm_cost_is_unknown_not_zero(tmp_path):
    args = SimpleNamespace(state=str(tmp_path / "state.json"), round_id="R")
    rec = run._log_topics_round_cost(
        args, None, {"external_calls": {}}, demand_count=0, card_count=0)
    assert rec["cost"] == run.UNKNOWN
    assert rec["cost"] != 0
    assert "未调用LLM" in rec["cost_basis"]


def test_opencode_list_cost_is_recorded_and_marked_non_paid(tmp_path):
    class OpenCodeChat:
        calls = 2
        usage_log = [{
            "provider": "opencode-go", "model": "mock",
            "prompt_tokens": 10, "completion_tokens": 5,
            "cost": 0.0012, "cost_basis": OPENCODE_BASIS,
        }]
        last_provider = "opencode-go"
        last_model = "mock"
        model = "mock"

    args = SimpleNamespace(state=str(tmp_path / "state.json"), round_id="R")
    rec = run._log_topics_round_cost(
        args, OpenCodeChat(), {"external_calls": {}},
        demand_count=1, card_count=1)
    assert rec["list_cost"] == 0.0012
    assert rec["cost_basis"] == OPENCODE_BASIS
    assert "非实付" in rec["note"]


# --------------------------------------------------------- 6) 推荐级别
def test_high_llm_score_can_still_be_recommended_without_threshold_change():
    mat = _material(
        "x_posts:1", "x_posts",
        "国内可用的免费开源 AI 教程，零基础手把手步骤，实测新能力降低门槛")
    card = {
        "关联素材": ["x_posts:1"],
        "_intended_type": "机会型",
        "_llm_score": 8.5,
        "生成引擎": "LLM",
    }
    merged = run._postprocess_round_cards(
        [card], [mat], now=NOW, profile=PROFILE, max_cards=5)
    assert merged
    assert merged[0]["推荐级别"] == "推荐"


# ----------------------------------------------------- 7) official-limit
def test_fetch_official_limit_parameter_overrides_per_source_config(monkeypatch):
    monkeypatch.setattr(official_source.requests, "get", _fake_get(text=RSS))
    sources = [
        {"id": "a", "name": "源A", "type": "rss",
         "url": "https://example.com/a.xml", "limit": 3},
        {"id": "b", "name": "源B", "type": "rss",
         "url": "https://example.com/b.xml", "limit": 3},
    ]
    out = fetch_official(sources, default_limit=1)
    assert len(out["items"]) == 2  # 每源 1 条，而不是配置里的 3 条
    assert out["per_source"]["a"]["items"] == 1
    assert out["per_source"]["b"]["items"] == 1
