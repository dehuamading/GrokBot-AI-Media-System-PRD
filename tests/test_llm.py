"""LLM 层测试：模板引擎（离线）+ openai-compat（mock，绝不真实调用）。"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.analysis.topics import build_topic_cards
from src.connectors.aihot_source import load_from_file
from src.llm.base import platforms_of
from src.llm.openai_compat import LLMNotConfigured, OpenAICompatClient
from src.llm.template_client import TemplateClient

FAKE = Path(__file__).resolve().parents[1] / "data" / "examples" / "aihot_fake_sample.json"


def _cards_materials():
    mats = load_from_file(FAKE)
    cards, _ = build_topic_cards(mats, top_k=8)
    return cards, mats


def test_template_douyin_structure():
    cards, mats = _cards_materials()
    d = TemplateClient().generate("抖音", cards[0], mats[:3])
    assert d.platform == "抖音" and d.version == 1
    assert d.title == cards[0]["拟定标题"]
    assert "收藏" in d.body and "出处" in d.body  # 口语化开头 + 出处提示
    assert d.sources and all(u for u in d.sources)
    assert d.engine.startswith("template@")
    assert any("待验证" in w for w in d.unverified_warnings)  # 需求假设必须提醒
    assert len(d.fact_checklist) == 3


def test_template_xhs_structure():
    cards, mats = _cards_materials()
    d = TemplateClient().generate("小红书", cards[0], mats[:3])
    assert "适合谁" in d.body and "#AI" in d.body  # 小红书：人群+标签
    assert d.platform == "小红书"


def test_template_no_fabricated_numbers():
    """模板正文只能由素材 claims 拼装：正文中的事实性数字（≥2位）必须源自素材；
    单个数字为模板序号（第1点/1.），不计。"""
    import re
    cards, mats = _cards_materials()
    d = TemplateClient().generate("抖音", cards[0], mats[:3])
    claims_text = " ".join(c for m in mats[:3] for c in (m.get("claims") or []))
    allowed = {n for n in re.findall(r"\d+", claims_text + d.title +
               cards[0]["拟定标题"] + cards[0]["面向谁"]) if len(n) >= 2}
    nums = {n for n in re.findall(r"\d+", d.body) if len(n) >= 2}
    assert nums <= allowed, f"正文出现素材之外编造的数字: {nums - allowed}"


def test_template_platforms_of():
    assert platforms_of({"建议平台": "两者"}) == ["抖音", "小红书"]
    assert platforms_of({"建议平台": "抖音"}) == ["抖音"]


# ---------------------------------------------------------------- openai-compat
def test_openai_compat_requires_env(monkeypatch):
    for k in ("LLM_API_BASE", "LLM_API_KEY", "LLM_MODEL"):
        monkeypatch.delenv(k, raising=False)
    with pytest.raises(LLMNotConfigured):
        OpenAICompatClient()


def test_openai_compat_payload_and_no_key_leak(monkeypatch):
    monkeypatch.setenv("LLM_API_BASE", "https://llm.example.com/v1")
    monkeypatch.setenv("LLM_API_KEY", "sk-test-DO-NOT-LEAK")
    monkeypatch.setenv("LLM_MODEL", "fake-model")
    cards, mats = _cards_materials()

    captured = {}

    class FakeResp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"choices": [{"message": {"content": json.dumps(
                {"title": "AI标题", "body": "正文", "unverified_warnings": ["x 未证实"]})}}]}

    def fake_post(url, headers=None, json=None, timeout=None):  # noqa: A002
        captured["url"] = url
        captured["headers"] = headers
        captured["body"] = json
        return FakeResp()

    with mock.patch("src.llm.openai_compat.requests.post", side_effect=fake_post):
        client = OpenAICompatClient()
        d = client.generate("抖音", cards[0], mats[:2])

    assert captured["url"] == "https://llm.example.com/v1/chat/completions"
    assert captured["headers"]["Authorization"] == "Bearer sk-test-DO-NOT-LEAK"
    assert "选题卡" in captured["body"]["messages"][1]["content"]
    assert d.engine == "openai-compat:fake-model"
    assert d.title == "AI标题" and "未证实" in d.unverified_warnings[0]
