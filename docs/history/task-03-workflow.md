# 任务 03：搭建工作流程序（覆盖任务 02）

用户澄清：本阶段是搭建工作流程序，不是现在就抓信息。任务 02 已被中止（未提交），/workspace/ai-zimeiti/repo 中有其未提交的半成品，以及 Notion「AI 自媒体系统」页下已建的数据库（含 00_X关注账号名单 等），可复用或调整，但不要删除既有 Notion 资料；若需清理请列入待确认事项。

按 PRD 设计并实现一条完整工作流：数据抓取（aihot.news /api/v1 等源的采集模块；X 源只留接口，从文件读入，不访问 X）→ 分析（去重、分类、选题评分推荐）→ 用户审核（人工关口：选题写入 Notion，用户在 Notion 里勾选/改状态，程序读取审核结果才继续）→ 写文案（用 LLM 生成初稿，写回 Notion 待审，绝不发布）。

先输出工作流设计文档（流程图、模块、数据结构、Notion 库结构、每步输入输出、人工关口），再写 Python 代码和单元测试；测试用假数据/mock，不要真实批量抓取；允许用 1–2 条真实请求做连通性自检。可以在 Notion「AI 自媒体系统」页（page id 408d7d3b292381068024e0548308c638）下创建工作流需要的空数据库结构。Notion token 在 /workspace/.secrets/notion_token，运行时读入 NOTION_TOKEN，绝不打印。

代码和文档推送到公开仓库 https://github.com/dehuamading/GrokBot-AI-Media-System-PRD（本地 /workspace/ai-zimeiti/repo），不动已有 PRD 文件，密钥不入库（.gitignore 屏蔽 .env、.secrets/、token、cookie），inputs/x_following.json 及任何真实 X 名单数据不入库，本阶段真实抓取的数据也不要入库（只放假数据样例）。不建定时任务（包括 GitHub Actions 的 schedule 触发）。

红线不变：未经用户批准不购买服务、不部署收费资源、不发布社交媒体内容、不删除既有资料；不访问 X；不建定时任务；密钥绝不入库、不打印。

交付：在仓库 docs/ 下提交执行记录、成功/失败证据（测试输出、commit 链接）、待确认事项，没做完的标注“未完成”。最后用中文输出阶段报告。
