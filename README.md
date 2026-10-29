# GrokBot AI 自媒体系统（AI-Media Brain）

以 X 关注账号为首要情报源、aihot.news 为核心补充的中文 AI 自媒体「情报 → 选题 → 单稿 → 复盘 → 知识沉淀」系统。按仓库根目录 PRD 文档分阶段实施（P0→P6）。

## 工作流（任务03 已实现，见 docs/workflow-design.md）

```text
[S1 抓取] aihot /api/v1(≤20条) + X关注名单文件 ─→ [S2 分析] 去重/分类/七维评分/选题卡
   ─→ [S3 写 Notion] ─→ ⏸GATE-1 用户在 Notion 勾选选题 ─→ [S4 读取审核结果]
   ─→ [S5 LLM 初稿(默认离线模板引擎)] ─→ 写 Notion 04 待审 ─→ ⏸GATE-2 用户确认
   ❌ 绝不自动发布（系统无发布代码路径）
```

## 快速开始

```bash
# 1) 引导（装依赖 + 单元测试[假数据/mock] + dry-run，不写任何外部系统）
./scripts/bootstrap.sh

# 2) 健康检查（网络/aihot/GitHub/Notion/状态/密钥扫描）
export NOTION_TOKEN=$(cat /workspace/.secrets/notion_token)   # 绝不打印、绝不提交
./scripts/healthcheck.sh

# 3) 跑一轮工作流到第一人工关口（默认离线回放假数据；--live 才真实抓取≤20条）
export NOTION_TOKEN=$(cat /workspace/.secrets/notion_token)
python3 -m src.workflows.run collect \
  --aihot-file data/examples/aihot_fake_sample.json \
  --x-following /workspace/ai-zimeiti/inputs/x_following.json
#    ⏸ 到 Notion「03_选题决策」把 1–3 张卡改为「已选定」（可另选「用户指定平台」）

# 4) 读取审核结果并生成初稿（停在第二人工关口）
python3 -m src.workflows.run draft          # 默认 --llm template（离线零成本）
#    ⏸ 到 Notion「04_文案与版本」审阅：状态改 已确认 或 修订中（附反馈）

# 5) 关口观测 / 状态 / 连通性自检
python3 -m src.workflows.run gate1
python3 -m src.workflows.run gate2
python3 -m src.workflows.run status [--live]
python3 -m src.workflows.run selfcheck      # aihot 1条真实请求 + Notion 结构检查

# 6) 备份导出 Notion → JSON/CSV
./scripts/export_notion.sh
```

## 目录（PRD 9.2）

- `docs/` PRD、工作流设计、架构、来源权限、恢复手册、各阶段执行报告
- `config/` 环境变量与信息源配置模板（仅变量名，无真实值）
- `prompts/` 选题筛选 / 抖音 / 小红书 / 复盘提示词模板
- `schemas/` 素材、选题卡、文案的 JSON Schema
- `src/connectors/` 信息源适配器（aihot / X 关注名单降级模式：只读文件，不访问 X）
- `src/analysis/` 分类 / 七维可解释评分 / 选题卡生成
- `src/llm/` 文案引擎接口（默认离线 template；可选 openai-compat，用户自备 key）
- `src/workflows/` 管线编排 + 状态机 + CLI（collect/gate1/draft/gate2/status/selfcheck）
- `src/notion_store/` Notion 适配层（限流重试、幂等 upsert、只增列升级、人工关口）
- `src/backup/` Notion 导出备份
- `tests/` 无网络单元测试（假数据/mock）；`data/examples/` 假数据样例（真实抓取数据不入库）

## 红线（与 PRD 第 0 节一致）

- 未经用户批准：不购买服务、不部署收费资源、不发布社交媒体内容、不删除既有资料；
- Pi Agent 不访问 X（名单由 Grok Bot 交付，只从文件读取）；不建定时任务（CI 仅 push/PR 触发）；
- 两道人工关口不可跳过：①用户在 Notion 选定选题 ②用户在 Notion 确认终稿；
- 每题×每平台只生成 1 份初稿，按反馈修订同一稿件；
- 密钥（.env / .secrets/ / token / cookie）绝不入库；含个人信息的原始输入（如 X 关注名单）与真实抓取数据不入公开库。

## 恢复

见 `docs/restore-runbook.md`：GitHub（代码+配置模板+假数据样例）+ Notion（业务知识）+ 密钥重注入 → 健康检查 → 幂等重跑。
