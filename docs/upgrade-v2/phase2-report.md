# 阶段 2 报告：信息采集扩展（2026-11-02，分支 upgrade/v2）

## 1. 执行记录
| 步骤 | 执行者 | 结果 |
|---|---|---|
| 建分支 upgrade/v2（自 a5300b6）+ 文档提交 c08746a | Grok Bot | 完成，推送前密钥扫描通过 |
| 任务 A：LLM 供应链 + X 格式修复（704c5e2） | Pi（opencode-go/deepseek-v4.1-flash） | 70→86 测试全过 |
| X 公开回复真实小样 | Grok Bot（X 只读工具 search_posts_all，6 次调用） | 5 帖 / 124 条公开回复，存本地 inputs/，未入库 |
| 任务 B：GitHub/官方源/导入接口/校验去重/接线（63d3a31） | Pi（opencode-go） | 86→114 全过，真实 sources-check 一次 |
| 独立审查 | Codex CLI 失败（ChatGPT 额度用尽，提示 2026-11-05 05:27 后恢复，模型显示 gpt-5.6-sol，未产出审查）；改由 Grok Bot 人工只读审查 | 发现 2 个重要问题 + 1 建议 |
| 修复轮 1 | Pi（opencode-go） | 114→121 全过；问题全部修复 |

## 2. 真实运行（非模拟）
命令：`python3 -m src.workflows.run sources-check --github-top 5 --x-file <本地X样本> --import-file tests/fixtures/import_sample_masked.json`（默认不写 Notion）
| 来源 | 条数 | 错误 | 去重 | 外部请求 |
|---|---|---|---|---|
| AIHot（现有连接器，未改） | 3 | 0 | 0 | 1 |
| GitHub（官方 REST，top 5） | 5 | 0 | 0 | 18 |
| 官方源（5 个 × 3 条） | 15 | 0 | 0 | 5 |
| X（Grok Bot 文件，含 124 条回复） | 5 | 0 | 0 | 0（代码不访问 X） |
| 导入（脱敏虚构样例，**模拟**） | 2 | 0 | 0 | 0 |
GitHub 5 项：morluto/rea、affaan-m/ECC、n8n-io/n8n、langgenius/dify、deepseek-ai/deepseek-harness（stars/release/license 为 API 返回值；安装难度/是否值得做视频标“推测”，付费 API/Windows/国内可用标“待验证”）。第二次运行时有快照，才计算 stars 增量（约 13 分钟间隔，仅证明机制可用）。
官方源：OpenAI News RSS、Google AI Blog RSS、anthropics/claude-code Releases、openai/codex Releases、QwenLM/qwen-code Releases（QwenLM/Qwen3 等无 Releases，已替换）。
LLM 供应链真实调用（各 1 次极短测试）：OpenCode Go 正常服务（36+2 tokens，list_cost 0.0000066 美元=套餐额度折算，非实付）；人为使 OpenCode key 失效 → 回退 GLM glm-5.1 成功（金额未知）；再使 GLM 失效 → DeepSeek deepseek-chat 成功（11+1 tokens，金额未知）。

## 3. X 能力实测
- 能取：帖子正文、作者、互动数；公开回复正文、时间、各自互动数（search_posts_all `conversation_id:<id> is:reply`）。
- 限制：每页实际最多返回 25 条且常带 next_token；部分页面不返回 conversation_id/lang（采集侧按查询补 conversation_id）；不代表能拿到全部评论。
- 回复作者用户名不进入统一素材 comments（隐私）。

## 4. 测试
旧 70 项全部保留、全过；新增 51 项；全量 121 passed（mock/假数据，不联网）。真实运行见 §2，与模拟测试分开。

## 5. 成本
GitHub/AIHot/RSS 免费接口；LLM：OpenCode Go 套餐额度折算约 0.0000066 美元（非实付）；GLM 金额未知；DeepSeek 付费 1 次极短调用，金额未知（约十几 tokens）。Pi 开发 4 次均由 OpenCode Go 服务（套餐额度，金额未知）。

## 6. 待确认 / 未完成
- Codex 独立审查未完成（额度用尽），需 2026-11-05 后补跑或批准其他独立审查方式。
- Notion 未写入（sources-check 默认只本地）；01 小样写入未做。
- 飞书只留接口；需求分类为关键词 stub（第 3 阶段替换）。
- 中文厂商官方源仅 Qwen Code Releases；智谱/Kimi 无稳定 Releases/RSS，未纳入。

## 7. 文件同步清单
- 已推送：docs/upgrade-v2/*、docs/history/*、docs/research/mediacrawler-report.md、src/**、tests/**（含脱敏虚构 fixtures）、config/sources.example.yaml、config/example.env（仅变量名）、docs/import-schema.md。
- 被忽略（.gitignore）：data/state/（快照、成本日志、sources_check 结果）、inputs/、*.log、.env、.secrets/。
- 不应推送：/workspace/ai-zimeiti/inputs/x_replies_sample_20261102.json（真实回复）、pi-reply-*.md、pi-stderr-*、outputs/、/workspace/.secrets/*、llm-env.sh。
- 未提交：无（工作区干净；仓库外的上述文件按设计不提交）。
