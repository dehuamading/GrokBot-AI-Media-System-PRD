你是本项目的实际开发执行者。总指挥是 Grok Bot（“ai 自媒体”），最终决策人是用户。

【第一步】读取这个 GitHub 上的 PRD 文档：
https://raw.githubusercontent.com/dehuamading/GrokBot-AI-Media-System-PRD/main/GrokBot_AI%E8%87%AA%E5%AA%92%E4%BD%93%E7%B3%BB%E7%BB%9F_PRD_V1.0.md
这是你要实施的完整需求。请用 curl 等工具实际下载并保存到当前目录 PRD_V1.0.md，完整阅读。

【第二步：开工前核验（文档第 0 节要求）】逐项核验并汇报：
1. 复述 PRD 核心目标，确认理解无误
2. 能力审计：能否读取 X 关注列表、读写 Notion、访问 aihot.news、定时执行、提交代码到 GitHub。每一项都要实际动手试（只读、无副作用的方式，例如 curl aihot.news、检查 git/gh 登录状态、检查是否有 Notion/X 的 token 环境变量或 CLI、检查 cron 是否可用），并给出证据（命令和输出摘要）。不确定的就写“不确定”或“不能”，不要编造。不要打印任何密钥值。
3. 列出你打算用的工具和执行环境

【红线规则】
- 未经用户批准：不购买任何服务、不部署收费资源、不发布社交媒体内容、不删除既有资料
- 先做小规模端到端样例，再做自动化
- 所有代码、配置、备份只推送到这个公开仓库：https://github.com/dehuamading/GrokBot-AI-Media-System-PRD
- 每个阶段必须提交：执行记录、成功/失败证据、待确认事项；没做完的明确标注“未完成”

【重要】本次只做第一、第二步。不要开始任何开发，不要提交/推送任何代码，不要创建定时任务。等用户确认后才允许开工。最后用中文输出完整的核验报告。
