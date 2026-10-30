"""GLM 聊天客户端（任务05）：OpenAI 兼容端点，用于帖子分析/打分与选题卡撰写。

- 端点/密钥/模型全部从环境变量读：GLM_API_BASE、GLM_API_KEY（必填），
  GLM_MODEL（默认 glm-5.1）；密钥只进请求头，绝不打印、绝不入日志/库；
- 主机名不硬编码（红线测试：src 中 https:// 字面量仅允许 aihot.news/api.notion.com），
  由运行方在环境中注入（Z.ai coding 端点，OpenAI 兼容）；
- 这是用户自备套餐（Z.ai coding），仅用于分析与文本生成，无发布能力。
"""

from __future__ import annotations

import json
import os
import re

import requests

DEFAULT_MODEL = "glm-5.1"
TIMEOUT = 300


class GLMNotConfigured(RuntimeError):
    pass


class GLMParseError(RuntimeError):
    pass


class GLMChat:
    """极薄封装：chat(system, user) -> str；chat_json(...) -> dict（容错解析 code fence）。"""

    def __init__(self, api_base: str | None = None, api_key: str | None = None,
                 model: str | None = None, timeout: int = TIMEOUT):
        self._base = (api_base or os.environ.get("GLM_API_BASE", "")).rstrip("/")
        self._key = api_key or os.environ.get("GLM_API_KEY", "")
        self._model = model or os.environ.get("GLM_MODEL", DEFAULT_MODEL)
        self._timeout = timeout
        self.calls = 0  # 请求计数（审计/报告用，不含任何内容）
        self.model = self._model  # 只读暴露（审计/报告用）
        if not (self._base and self._key):
            raise GLMNotConfigured(
                "GLM 未配置：需设置 GLM_API_BASE 与 GLM_API_KEY 环境变量"
                "（密钥绝不打印；不配置则用启发式分析，无需 GLM）")

    # ------------------------------------------------------------------
    def chat(self, system: str, user: str, temperature: float = 0.3,
             max_tokens: int = 6000) -> str:
        self.calls += 1
        resp = requests.post(
            f"{self._base}/chat/completions",
            headers={"Authorization": f"Bearer {self._key}",
                     "Content-Type": "application/json"},
            json={"model": self._model, "temperature": temperature,
                  "max_tokens": max_tokens,
                  "messages": [{"role": "system", "content": system},
                               {"role": "user", "content": user}]},
            timeout=self._timeout,
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"] or ""

    def chat_json(self, system: str, user: str, **kw) -> dict:
        content = self.chat(system, user, **kw)
        return parse_json_loose(content)


def parse_json_loose(content: str) -> dict:
    """容错解析：去 ```json fence / 前后杂文本，取第一个完整 JSON 对象。"""
    text = (content or "").strip()
    fence = re.search(r"```(?:json)?\s*(.+?)\s*```", text, re.S)
    if fence:
        text = fence.group(1)
    start = text.find("{")
    if start < 0:
        raise GLMParseError("GLM 返回中无 JSON 对象")
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start:i + 1])
                except json.JSONDecodeError as e:
                    raise GLMParseError(f"GLM JSON 解析失败：{e}") from e
    raise GLMParseError("GLM 返回的 JSON 对象不完整（括号不闭合）")
