"""LLM 提供方回退链：OpenCode Go → 智谱 GLM → DeepSeek（付费，最后）。

设计约束：
- 所有端点在运行时从环境变量读取，源码不硬编码任何 https:// 主机；
- 密钥只进入请求头，绝不打印、绝不写入日志或异常文本；
- 任一提供方缺 key/base 直接跳过并记录“未配置”；
- 失败（HTTP 非 2xx、超时/网络错误、响应无 choices 等）回退下一个；
- 全部失败抛 LLMChainExhausted；
- 接口与 GLMChat 保持鸭子类型兼容：chat / chat_json / calls / model。

usage_log 每次成功调用记录 {provider, model, prompt_tokens, completion_tokens,
cost, cost_basis}。cost 取接口返回（opencode usage.opencode.list_cost 或
usage.cost）；拿不到写字符串“未知”，绝不用 0 冒充未知。cost_basis 标明口径：
opencode list_cost 是套餐额度按标价折算（非实付），usage.cost 是接口返回。
"""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass, field

import requests

from src.llm.glm import GLMParseError, parse_json_loose

DEFAULT_OPENCODE_MODEL = "deepseek-v4.1-flash"
DEFAULT_GLM_MODEL = "glm-5.1"
DEFAULT_DEEPSEEK_MODEL = "deepseek-chat"
TIMEOUT = 300
UNKNOWN = "未知"


class LLMChainExhausted(RuntimeError):
    """所有已配置提供方均失败。消息只含 provider/原因，不含任何密钥。"""


@dataclass
class Provider:
    name: str
    api_base: str
    api_key: str
    model: str
    extra_headers: dict[str, str] = field(default_factory=dict)


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def load_providers(session_id: str) -> list[Provider]:
    """按固定顺序从环境变量装载提供方；缺 key/base 的也保留（供记录未配置）。"""
    return [
        Provider(
            name="opencode-go",
            api_base=_env("OPENCODE_GO_API_BASE").rstrip("/"),
            api_key=_env("OPENCODE_GO_API_KEY"),
            model=_env("OPENCODE_GO_MODEL", DEFAULT_OPENCODE_MODEL),
            # 服务端要求每次运行一个 session uuid，否则 MissingSessionID。
            extra_headers={"x-opencode-session": session_id},
        ),
        Provider(
            name="glm",
            api_base=_env("GLM_API_BASE").rstrip("/"),
            api_key=_env("GLM_API_KEY"),
            model=_env("GLM_MODEL", DEFAULT_GLM_MODEL),
        ),
        Provider(
            name="deepseek",
            api_base=_env("DEEPSEEK_API_BASE").rstrip("/"),
            api_key=_env("DEEPSEEK_API_KEY"),
            model=_env("DEEPSEEK_MODEL", DEFAULT_DEEPSEEK_MODEL),
        ),
    ]


class ProviderChain:
    """多提供方回退链；一个实例代表一次运行（共享同一个 opencode session uuid）。"""

    def __init__(self, providers: list[Provider] | None = None,
                 session_id: str | None = None, timeout: int = TIMEOUT):
        self.session_id = session_id or str(uuid.uuid4())
        self._providers = providers if providers is not None else load_providers(self.session_id)
        self._timeout = timeout
        self.calls = 0
        self.usage_log: list[dict] = []
        self.attempts: list[dict] = []
        self.skipped: list[dict] = []
        self._last_provider: str | None = None
        self._last_model: str | None = None
        for p in self._providers:
            if not (p.api_base and p.api_key):
                self.skipped.append({"provider": p.name, "reason": "未配置"})

    # ------------------------------------------------------------------
    @property
    def model(self) -> str:
        """最近一次实际服务的 provider/model；未调用时返回“未调用”。"""
        if self._last_provider and self._last_model:
            return f"{self._last_provider}/{self._last_model}"
        return "未调用"

    @property
    def last_provider(self) -> str:
        return self._last_provider or "未调用"

    @property
    def last_model(self) -> str:
        return self._last_model or "未调用"

    # ------------------------------------------------------------------
    def chat(self, system: str, user: str, temperature: float = 0.3,
             max_tokens: int = 6000) -> str:
        failures: list[str] = []
        for p in self._providers:
            if not (p.api_base and p.api_key):
                continue
            self.calls += 1
            try:
                data = self._request(p, system, user, temperature, max_tokens)
                content = self._content(data)
                usage = self._usage(data)
                rec = {
                    "provider": p.name,
                    "model": p.model,
                    "prompt_tokens": usage["prompt_tokens"],
                    "completion_tokens": usage["completion_tokens"],
                    "cost": usage["cost"],
                    "cost_basis": usage["cost_basis"],
                }
                self.usage_log.append(rec)
                self.attempts.append({"provider": p.name, "model": p.model,
                                      "ok": True, "reason": ""})
                self._last_provider, self._last_model = p.name, p.model
                return content
            except Exception as e:  # noqa: BLE001 - 所有失败都回退下一个提供方
                reason = self._safe_reason(e, p)
                failures.append(f"{p.name}: {reason}")
                self.attempts.append({"provider": p.name, "model": p.model,
                                      "ok": False, "reason": reason})
        detail = "；".join(failures) or "无已配置的提供方"
        raise LLMChainExhausted(f"LLM 提供方全部失败：{detail}")

    def chat_json(self, system: str, user: str, **kw) -> dict:
        content = self.chat(system, user, **kw)
        return parse_json_loose(content)

    # ------------------------------------------------------------------
    def _request(self, p: Provider, system: str, user: str,
                 temperature: float, max_tokens: int) -> dict:
        headers = {
            "Authorization": f"Bearer {p.api_key}",
            "Content-Type": "application/json",
            **p.extra_headers,
        }
        resp = requests.post(
            f"{p.api_base}/chat/completions",
            headers=headers,
            json={
                "model": p.model,
                "temperature": temperature,
                "max_tokens": max_tokens,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
            },
            timeout=self._timeout,
        )
        resp.raise_for_status()
        return resp.json()

    @staticmethod
    def _content(data: dict) -> str:
        choices = data.get("choices") if isinstance(data, dict) else None
        if not choices:
            raise ValueError("响应无 choices")
        try:
            content = choices[0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as e:
            raise ValueError("响应 choices 中无 message.content") from e
        if not isinstance(content, str):
            raise ValueError("响应 choices 中 content 非字符串")
        return content

    @staticmethod
    def _usage(data: dict) -> dict:
        usage = (data.get("usage") or {}) if isinstance(data, dict) else {}
        opencode = usage.get("opencode") or {}
        if isinstance(opencode, dict) and opencode.get("list_cost") is not None:
            cost = opencode["list_cost"]
            cost_basis = "opencode-go套餐额度折算(美元,非实付)"
        elif usage.get("cost") is not None:
            cost = usage["cost"]
            cost_basis = "接口返回"
        else:
            cost = UNKNOWN
            cost_basis = UNKNOWN

        def _token(key: str):
            value = usage.get(key)
            return value if value is not None else UNKNOWN

        return {
            "prompt_tokens": _token("prompt_tokens"),
            "completion_tokens": _token("completion_tokens"),
            "cost": cost,
            "cost_basis": cost_basis,
        }

    @staticmethod
    def _safe_reason(exc: Exception, p: Provider) -> str:
        """记录可审计的失败原因，且绝不包含密钥。"""
        if isinstance(exc, requests.HTTPError):
            status = getattr(getattr(exc, "response", None), "status_code", None)
            reason = f"HTTP {status}" if status else "HTTPError"
        elif isinstance(exc, requests.Timeout):
            # requests.Timeout 同时是 RequestException 子类，放在前面判断
            reason = "超时"
        elif isinstance(exc, requests.RequestException):
            reason = f"网络错误（{type(exc).__name__}）"
        elif isinstance(exc, (ValueError, GLMParseError)):
            reason = str(exc) or type(exc).__name__
        else:
            reason = f"异常（{type(exc).__name__}）"
        return reason.replace(p.api_key, "***") if p.api_key else reason


# 兼容不同调用方可能使用的类名。
LLMProviderChain = ProviderChain
LLMChain = ProviderChain


def make_llm_chain() -> ProviderChain:
    return ProviderChain()
