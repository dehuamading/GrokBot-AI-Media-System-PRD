# 任务 04：讲解报告（只读）

请阅读仓库 /workspace/ai-zimeiti/repo（重点 docs/workflow-design.md、全部代码、tests）以及 Notion「AI 自媒体系统」页（page id 408d7d3b292381068024e0548308c638）下的结构，为一位非程序员用户写一份中文讲解报告，内容包括：

1. 整体框架：一句话概括 + 流程图（文字箭头或 mermaid）
2. 编写了哪些程序：每个文件/模块的名字、作用、输入、输出、能产生什么结果
3. 工作流程每一步（S1–S6）做什么、用户在哪里介入、怎么运行（具体命令）
4. Notion 知识库：怎么建的、各数据库的用途、主要字段、当前行数、库之间的关系
5. 现在能干什么、还不能干什么（没做完的明确标注“未完成”）

报告写到 /workspace/ai-zimeiti/report-04-explain.md（不要放在 repo 里）。实事求是，不要夸大，以代码和 Notion 实际情况为准。

严格只读红线：
- 不修改仓库任何文件，不 git commit / push / fetch，不运行会写文件或抓取线上数据的程序（可以运行单元测试，但不要产生仓库改动）。
- Notion 只读：token 在 /workspace/.secrets/notion_token，运行时读入环境变量 NOTION_TOKEN，绝不打印 token；只允许 GET/查询（search、retrieve、databases query），禁止创建、更新、删除任何 Notion 内容。
- 不访问 X，不访问 aihot.news 等线上数据源。
- 唯一允许写入的文件是 /workspace/ai-zimeiti/report-04-explain.md。

最后在输出中简要说明报告已写好及主要结论。

## 附加任务：诊断 git push 403（只诊断，不修复）
在 /workspace/ai-zimeiti/repo 诊断 `git push origin main` 为何返回 403。禁止真正 push，禁止修改 remote/凭据/gh auth，绝不打印任何 token（输出中出现的 token 一律打码为 ***）。检查：
- `git remote -v`（URL 中如含 token 要打码）
- `git config --show-origin --get-all credential.helper`
- `gh auth status`（账号、token 类型、scopes）
- 环境变量 GH_TOKEN / GITHUB_TOKEN 等是否设置（只说名字和是否存在）
- git 对 github.com 实际使用哪个凭据（来源/账号，不打印值）
- `GIT_TRACE=1 git push --dry-run origin main` 的确切错误（仅 dry-run，打码）
对比：gh 之前显示 dehuamading 对该仓库有 ADMIN 权限，判断 git 是否用了不同凭据（例如缺少 Contents:write 的细粒度 PAT 或另一个账号）。
在报告末尾加一节「git push 403 诊断」：中文根因、证据、最安全的修复选项（如 `gh auth setup-git`），不要执行修复。
