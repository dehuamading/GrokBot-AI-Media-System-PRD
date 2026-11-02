# 任务 02：P0 收尾 + P1 小规模端到端样例

用户已确认开工，决定如下：
1. 公开仓库即可：所有代码/配置/备份只推送到 https://github.com/dehuamading/GrokBot-AI-Media-System-PRD（gh 已登录）。绝不提交密钥；.gitignore 必须屏蔽 .env、.secrets/、各类 token、cookie。
2. X：你（Pi Agent）不得自行访问 X。由 Grok Bot（指挥官）读取用户的 X 关注列表并以文件交付。当前文件：/workspace/ai-zimeiti/inputs/x_following.json（135 个账号：username, name, description, followers）。X 帖子内容暂不可得；目前把 X 当作账号名单使用，并在报告中注明为缺口。
3. Notion：内部集成 token 位于 /workspace/.secrets/notion_token（运行时读入环境变量 NOTION_TOKEN；绝不打印、绝不提交）。它只能访问页面「AI 自媒体系统」（page id 408d7d3b292381068024e0548308c638）。使用 Notion-Version 2022-06-28 或更新版本。所有 Notion 结构必须建在该页面之下。
4. 调度：本机没有 cron；本阶段不要设置任何定时/调度。
5. 本机是持久的（非会话级），但仍需按 PRD 设计可从 GitHub + Notion 恢复。

本阶段目标：按 PRD 顺序完成 P0 收尾 + P1 小规模端到端样例：
- 初始化公开仓库结构（PRD 9.2）；
- 从 aihot.news /api/v1 拉取少量（≤20 条）精选信息 + 读取 X 关注名单文件，形成素材池；
- 在 Notion「AI 自媒体系统」页下建立 PRD 要求的最小知识库结构（数据库/页面），写入素材；
- 推荐 5—10 个选题写入 Notion 并在报告中列出；
- 在用户选题前停下（第一个人工关口），不要生成初稿。

代码用 Python，提交并推送到上述公开仓库（先在本地 clone 到 /workspace/ai-zimeiti/repo），不要删除或改写仓库里已有的 PRD 文件。

红线：未经用户批准不购买服务、不部署收费资源、不发布社交媒体内容、不删除既有资料；不访问 X；不建定时任务；密钥绝不入库、不打印。

交付：在仓库 docs/ 下提交本阶段执行记录、成功/失败证据、待确认事项；没做完的明确标注“未完成”。最后用中文输出阶段报告：做了什么、证据（commit 链接、Notion 页面链接、命令输出摘要）、选题列表、待用户确认事项。
