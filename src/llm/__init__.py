"""LLM 层（任务03）：文案初稿生成的可替换接口。

- DraftClient：协议（接口）。实现方必须只产初稿，绝不发布、绝不写 Notion 之外的外部系统。
- 默认实现 TemplateClient：离线确定性模板（零成本、无网络、可单测）。
- 可选实现 OpenAICompatClient：任意 OpenAI 兼容 API（用户自备 key 才启用）。

选择规则：环境变量 LLM_ENGINE=template（默认）| openai-compat。
"""
