# GrokBot AI 自媒体系统（AI-Media Brain）

> **v2 升级（分支 upgrade/v2，2026-11）当前能力**：多来源采集 → 用户需求识别 → 新技术机会发现 → 双引擎选题（v3-dual 评分）→ 偏好学习 → 单稿文案 → Notion 沉淀。全部手动触发，无定时任务；两道人工关口（GATE-1 选题、GATE-2 审稿），系统没有任何发布代码路径。详见 docs/upgrade-v2/final-report.md。

## v2 命令速查（手动触发）
```bash
export NOTION_TOKEN=$(cat /workspace/.secrets/notion_token)   # 绝不打印、绝不提交
source /workspace/ai-zimeiti/llm-env.sh                       # 模型 key（OpenCode Go→GLM→DeepSeek），只放仓库外
python3 -m src.workflows.run sources-check --github-top 5 [--write-notion --max-rows 10]   # 采集小样；写 01 只增、按 item_id 去重
python3 -m src.workflows.run topics-round --sources aihot,github,official,x \
   --x-file <Grok Bot 交付的 X 帖子文件> --x-replies-file <X 公开回复文件> --max-cards 10 --write-notion   # 到 GATE-1
python3 -m src.workflows.run feedback --topic <id> --pick 认可|一般|未选|否决 --say "原话"          # 写 05，认可另写认可素材
python3 -m src.workflows.run rules --propose        # 每 3 轮生成候选规则（待确认，绝不自动生效）
python3 -m src.workflows.run draft --topic <id> --llm chain --no-paid --export-dir <dir>         # 只为选中卡写稿 → 04 待审核 → GATE-2
```
| 模块 | 文件 |
|---|---|
| 来源 | src/connectors/{aihot,github,official,x_posts,import}_source.py（国内平台只留导入接口，见 docs/import-schema.md） |
| 校验/去重 | src/validate.py、src/normalize.py |
| 需求引擎（10 类，LLM+关键词回退，证据脱敏） | src/analysis/demand.py |
| 评分 v3-dual（9 维满分 18，传播潜力不计分，≥12 且轨道分≥3 推荐 / ≥9 备选） | src/analysis/scoring.py |
| 偏好学习（05 反馈/认可素材/正在使用规则注入提示词） | src/analysis/preference.py |
| 隐私与安全（X 用户名/正文、注册翻墙等敏感步骤、收益承诺拦截） | src/analysis/privacy.py、src/analysis/safety.py |
| 模型供应链与成本（未知记“未知”，OpenCode list_cost 为套餐折算非实付） | src/llm/provider_chain.py、src/cost_log.py |
| 文案 | src/llm/draft_writer.py |

---


以 X 关注账号为首要情报源、aihot.news 为核心补充的中文 AI 自媒体「情报 → 选题 → 单稿 → 复盘 → 知识沉淀」系统。按仓库根目录 PRD 文档分阶段实施（P0→P6）。

## 工作流（任务03 已实现，见 docs/workflow-design.md；任务05 增 X 帖子文件输入 + 受众画像 + GLM 分析）

```text
[S1 抓取] aihot /api/v1(≤20条) + X关注名单文件 + X帖子文件(本地JSON,任务05)
   ─→ [S2 分析] 去重/分类/七维评分(受众画像驱动,v2)/选题卡（可选 GLM 分析引擎）
   ─→ [S3 写 Notion] ─→ ⏸GATE-1 用户在 Notion 勾选选题 ─→ [S4 读取审核结果]
   ─→ [S5 LLM 初稿(默认离线模板引擎)] ─→ 写 Notion 04 待审 ─→ ⏸GATE-2 用户确认
   ❌ 绝不自动发布（系统无发布代码路径）；X 帖子输入只读本地文件，绝不访问 X
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

# 3b) 任务05：X 帖子文件输入（只读本地 JSON，不联网不访问 X）+ 受众画像 + 可选 GLM 分析
#     帖子文件由用户侧工具导出（字段 id/url/author_username/author_name/created_at/
#     text/lang/likes/reposts/replies/quotes/impressions/links），真实文件不入库
python3 -m src.workflows.run collect --x-posts /workspace/ai-zimeiti/inputs/x_posts_24h.json \
  --live --analyze glm        # glm 需 GLM_API_BASE/GLM_API_KEY 环境变量（用户自备套餐）
python3 -m src.workflows.run write-audience   # 受众画像写入 Notion「用户画像」页（幂等）

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
