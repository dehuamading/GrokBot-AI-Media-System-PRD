"""可选：OpenAI 兼容 API 引擎（用户自备 key 时启用，本阶段默认关闭）。

- 配置：LLM_API_BASE（如 https://llm.example.com/v1）、LLM_API_KEY、LLM_MODEL
- 三者任一缺失即抛 LLMNotConfigured —— 不会回退联网，也不会打印 key；
- 仅调用 {base}/chat/completions 一个端点；key 只进请求头，绝不入日志/异常文本。
- 本阶段未配置任何真实服务（红线：未经批准不购买服务）。
"""

from __future__ import annotations

import json
import os

import requests

from src.llm.base import Draft, DraftClient


class LLMNotConfigured(RuntimeError):
    pass


SYSTEM_PROMPT = (
    "你是中文 AI 自媒体编辑。按 prompts/script-{platform}.md 的初始写作假设写初稿，"
    "禁止编造收益/时间/效果/价格；无法溯源的内容写进『未证实内容提醒』。"
    "严格输出 JSON：{{\"title\":..., \"body\":..., \"unverified_warnings\":[...]}}"
)


class OpenAICompatClient(DraftClient):
    def __init__(self, api_base: str | None = None, api_key: str | None = None,
                 model: str | None = None, timeout: int = 60):
        self._base = (api_base or os.environ.get("LLM_API_BASE", "")).rstrip("/")
        self._key = api_key or os.environ.get("LLM_API_KEY", "")
        self._model = model or os.environ.get("LLM_MODEL", "")
        self._timeout = timeout
        if not (self._base and self._key and self._model):
            raise LLMNotConfigured(
                "openai-compat 引擎未配置：需同时设置 LLM_API_BASE / LLM_API_KEY / LLM_MODEL"
                "（默认使用离线模板引擎 template，无需任何服务）")

    def generate(self, platform: str, card: dict, materials: list[dict]) -> Draft:
        user_msg = json.dumps({
            "平台": platform, "选题卡": card,
            "素材事实": [{"事实": c, "出处": m.get("source_url")}
                      for m in materials for c in (m.get("claims") or [])],
        }, ensure_ascii=False)
        resp = requests.post(
            f"{self._base}/chat/completions",
            headers={"Authorization": f"Bearer {self._key}",
                     "Content-Type": "application/json"},
            json={"model": self._model, "temperature": 0.7,
                  "response_format": {"type": "json_object"},
                  "messages": [
                      {"role": "system",
                       "content": SYSTEM_PROMPT.format(platform="douyin" if platform == "抖音" else "xiaohongshu")},
                      {"role": "user", "content": user_msg},
                  ]},
            timeout=self._timeout,
        )
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"]
        data = json.loads(content)
        return Draft(topic_id=card["topic_id"], platform=platform,
                     title=data["title"], body=data["body"],
                     fact_checklist=[f"素材出处：{m.get('author_or_publisher')}｜{m.get('title_or_summary')}"
                                     for m in materials],
                     sources=[m["source_url"] for m in materials if m.get("source_url")],
                     unverified_warnings=data.get("unverified_warnings") or [],
                     engine=f"openai-compat:{self._model}")
