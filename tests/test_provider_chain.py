"""LLM 提供方回退链测试：全部 mock requests，绝不访问网络。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.llm.provider_chain import LLMChainExhausted, ProviderChain

ENV_KEYS = [
    "OPENCODE_GO_API_KEY", "OPENCODE_GO_API_BASE", "OPENCODE_GO_MODEL",
    "GLM_API_KEY", "GLM_API_BASE", "GLM_MODEL",
    "DEEPSEEK_API_KEY", "DEEPSEEK_API_BASE", "DEEPSEEK_MODEL",
]


class FakeResp:
    def __init__(self, status: int = 200, payload: dict | None = None):
        self.status_code = status
        self.payload = payload or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}", response=self)

    def json(self):
        return self.payload


def ok(content: str = "ok", usage: dict | None = None) -> FakeResp:
    payload = {"choices": [{"message": {"content": content}}]}
    if usage is not None:
        payload["usage"] = usage
    return FakeResp(200, payload)


def configure_all(monkeypatch):
    for k in ENV_KEYS:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("OPENCODE_GO_API_KEY", "opencode-key-very-secret")
    monkeypatch.setenv("OPENCODE_GO_API_BASE", "https://opencode-go.example.com/v1")
    monkeypatch.setenv("GLM_API_KEY", "glm-key-very-secret")
    monkeypatch.setenv("GLM_API_BASE", "https://glm.example.com/v1")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "deepseek-key-very-secret")
    monkeypatch.setenv("DEEPSEEK_API_BASE", "https://deepseek.example.com/v1")


def scripted_post(monkeypatch, responses):
    calls = []

    def fake_post(url, headers=None, json=None, timeout=None):  # noqa: A002
        calls.append({"url": url, "headers": headers or {}, "body": json})
        idx = min(len(calls) - 1, len(responses) - 1)
        return responses[idx]

    monkeypatch.setattr("src.llm.provider_chain.requests.post", fake_post)
    return calls


# ------------------------------------------------------------------ 顺序与配置
def test_order_opencode_first_and_session_header(monkeypatch):
    configure_all(monkeypatch)
    calls = scripted_post(monkeypatch, [ok("from-opencode", {
        "prompt_tokens": 10, "completion_tokens": 5,
        "opencode": {"list_cost": 0.0012}, "cost": 0,
    })])

    chain = ProviderChain()
    assert chain.chat("system", "user") == "from-opencode"
    assert len(calls) == 1
    assert calls[0]["url"].startswith("https://opencode-go.example.com/v1")
    assert calls[0]["url"].endswith("/chat/completions")
    assert calls[0]["headers"]["Authorization"] == "Bearer opencode-key-very-secret"
    sid = calls[0]["headers"].get("x-opencode-session")
    assert sid and len(sid) >= 32
    assert chain.model == "opencode-go/deepseek-v4.1-flash"
    assert chain.usage_log[0]["cost"] == 0.0012
    assert chain.usage_log[0]["cost_basis"] == (
        "opencode-go套餐额度折算(美元,非实付)")
    assert chain.usage_log[0]["prompt_tokens"] == 10
    assert chain.usage_log[0]["completion_tokens"] == 5


def test_missing_key_is_skipped(monkeypatch):
    for k in ENV_KEYS:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("GLM_API_KEY", "glm-key-very-secret")
    monkeypatch.setenv("GLM_API_BASE", "https://glm.example.com/v1")
    calls = scripted_post(monkeypatch, [ok("from-glm")])

    chain = ProviderChain()
    assert chain.chat("s", "u") == "from-glm"
    assert len(calls) == 1
    assert calls[0]["url"].startswith("https://glm.example.com/v1")
    skipped = {s["provider"]: s["reason"] for s in chain.skipped}
    assert skipped["opencode-go"] == "未配置"
    assert skipped["deepseek"] == "未配置"
    assert chain.model == "glm/glm-5.1"


# ------------------------------------------------------------------ 失败回退
def test_first_429_falls_back_to_glm(monkeypatch):
    configure_all(monkeypatch)
    calls = scripted_post(monkeypatch, [
        FakeResp(429, {}),
        ok("from-glm", {"prompt_tokens": 3, "completion_tokens": 4, "cost": 0.02}),
    ])

    chain = ProviderChain()
    assert chain.chat("s", "u") == "from-glm"
    assert len(calls) == 2
    assert "glm.example.com" in calls[1]["url"]
    assert chain.model == "glm/glm-5.1"
    assert chain.usage_log[0]["cost"] == 0.02
    assert chain.calls == 2


def test_first_two_fail_falls_back_to_deepseek(monkeypatch):
    configure_all(monkeypatch)
    calls = scripted_post(monkeypatch, [
        FakeResp(402, {}),
        FakeResp(500, {}),
        ok("from-deepseek", {"prompt_tokens": 7, "completion_tokens": 8, "cost": 0.03}),
    ])

    chain = ProviderChain()
    assert chain.chat("s", "u") == "from-deepseek"
    assert len(calls) == 3
    assert "deepseek.example.com" in calls[2]["url"]
    assert chain.model == "deepseek/deepseek-chat"
    assert chain.usage_log[0]["cost"] == 0.03


def test_4xx_param_error_falls_back_and_records_reason(monkeypatch):
    configure_all(monkeypatch)
    scripted_post(monkeypatch, [FakeResp(400, {}), ok("from-glm")])

    chain = ProviderChain()
    assert chain.chat("s", "u") == "from-glm"
    assert chain.attempts[0]["provider"] == "opencode-go"
    assert chain.attempts[0]["ok"] is False
    assert "400" in chain.attempts[0]["reason"]


def test_no_choices_falls_back(monkeypatch):
    configure_all(monkeypatch)
    scripted_post(monkeypatch, [FakeResp(200, {"choices": []}), ok("from-glm")])

    chain = ProviderChain()
    assert chain.chat("s", "u") == "from-glm"
    assert "choices" in chain.attempts[0]["reason"]


def test_all_fail_raises_without_leaking_keys(monkeypatch):
    configure_all(monkeypatch)
    scripted_post(monkeypatch, [FakeResp(500, {})])

    chain = ProviderChain()
    with pytest.raises(LLMChainExhausted) as excinfo:
        chain.chat("s", "u")
    msg = str(excinfo.value)
    assert "opencode-key-very-secret" not in msg
    assert "glm-key-very-secret" not in msg
    assert "deepseek-key-very-secret" not in msg
    assert chain.calls == 3


# ------------------------------------------------------------------ usage / JSON
def test_cost_basis_for_api_returned_cost(monkeypatch):
    configure_all(monkeypatch)
    scripted_post(monkeypatch, [ok("from-opencode", {
        "prompt_tokens": 1, "completion_tokens": 2, "cost": 0.02,
    })])

    chain = ProviderChain()
    chain.chat("s", "u")
    assert chain.usage_log[0]["cost"] == 0.02
    assert chain.usage_log[0]["cost_basis"] == "接口返回"


def test_unknown_cost_is_unknown_not_zero(monkeypatch):
    configure_all(monkeypatch)
    scripted_post(monkeypatch, [ok("from-opencode")])  # 无 usage

    chain = ProviderChain()
    chain.chat("s", "u")
    assert chain.usage_log[0]["cost"] == "未知"
    assert chain.usage_log[0]["cost_basis"] == "未知"
    assert chain.usage_log[0]["cost"] != 0
    assert chain.usage_log[0]["prompt_tokens"] == "未知"
    assert chain.usage_log[0]["completion_tokens"] == "未知"


def test_chat_json_parses_fenced_json(monkeypatch):
    configure_all(monkeypatch)
    scripted_post(monkeypatch, [ok('```json\n{"score": 8}\n```')])

    chain = ProviderChain()
    assert chain.chat_json("s", "u") == {"score": 8}
