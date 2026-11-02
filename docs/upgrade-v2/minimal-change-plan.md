# 最小改动计划（第 2–4 阶段）
基线：main @ a5300b6（本地=远程，70 测试全过）。

## 分支与提交
1. 从 a5300b6 建本地分支 `upgrade/v2`；首个提交 = phase1 三份文档 + 允许推送的历史文档（task-0*.md、reflection-01、report-04、mediacrawler 报告，放 docs/history/ 与 docs/research/）。
2. 每阶段 1–3 个提交，阶段末推送 `upgrade/v2`（普通 push，禁止 force）。
3. 第 4 阶段末开 PR → main，**不合并**，等用户批准。
4. 每次推送前：密钥扫描 + 检查无 inputs/ 真实数据、无未脱敏评论。

## 第 2 阶段 信息采集扩展（Pi 开发，Grok Bot 交 X 数据并验收）
| 动作 | 文件 |
|---|---|
| 修 X 文件格式兼容（扁平 + X API 原样，含 replies[]） | 改 src/normalize.py、src/connectors/x_posts_source.py |
| 统一素材新增字段 | 改 src/normalize.py（只增） |
| GitHub 连接器（默认 5，可配） | 新 src/connectors/github_source.py；新 data/state/github_snapshots.json（忽略） |
| 官方源（≤5，RSS/Releases） | 新 src/connectors/official_source.py；config/sources.example.yaml 增项 |
| 国内导入接口（文件/Notion；飞书只留映射） | 新 src/connectors/import_source.py；docs/import-schema.md；tests/fixtures/import_sample_masked.json |
| 来源校验/去重/错误记录 | 新 src/validate.py |
| collect 接新来源 | 改 src/workflows/run.py、pipeline.py |
测试新增：test_x_posts（两种格式+回复）、test_github_source（mock）、test_official_source（mock）、test_import_source（脱敏样例、坏数据报错、去重）、test_validate。
验收：旧 70 项仍过；真实小样：AIHot 1 次、GitHub ≤5 个项目、Grok Bot 交 1 份 X 文件（≥1 帖 + 回复）能进入流程；成本日志有记录。预计 Pi 3–4 轮。

## 第 3 阶段 选题与经验学习
| 动作 | 文件 |
|---|---|
| 需求 10 类分类 + 证据保留 | 新 src/analysis/demand.py |
| 评分 v3-dual（新增 3 维、传播假设改不计分、双轨排序） | 改 src/analysis/scoring.py（保留 v2 函数可回退） |
| 画像补“副业创业”“国外工具国内用” | 改 src/analysis/audience.py（画像 v2） |
| 选题卡新列 | 改 src/analysis/topics.py、notion_store/bootstrap.py（ensure_kb_upgrades 只增列：03 与 05） |
| 偏好闭环 | 新 src/analysis/preference.py；新 run 子命令 feedback / rules |
| 02 写入需求条目 | 改 pipeline.py |
测试新增：test_demand、test_scoring_v3（不重复计分、副业证据封顶、无评论机会型不被淘汰）、test_preference（mock Notion：写反馈、读最近 5+5、3 轮出候选规则且状态=待验证）。
验收：场景 2/3/4/5 用真实 Notion 测试库条目跑通（写入前征得同意）；预计 Pi 4–5 轮。

## 第 4 阶段 文案与回归
| 动作 | 文件 |
|---|---|
| GLM 写稿客户端（3 标题/2 开头/口播/演示/核验清单/来源） | 新 src/llm/glm_draft.py；改 run.py make_llm |
| draft --topic 只写一张 | 改 run.py、pipeline.py |
| 成本日志 | 新 src/cost_log.py |
| 文档 | README、docs/architecture.md、docs/notion-schema.md、docs/maintenance.md、docs/phase-reports/* |
测试新增：test_glm_draft（mock）、test_cost_log（未知≠0）、端到端 dry-run 8 场景（模拟部分明确标“模拟”）。
验收：全量测试通过；场景 1–8；Codex CLI 只读审查（记录模型）；最终文件清单（已推/忽略/不应推/未提交）。预计 Pi 3 轮。

## 需要用户批准
1. 进入第 2 阶段并建分支 upgrade/v2、推送该分支。
2. 允许推送的历史文档清单（§审计 1 表中“应推送”项），pi-reply-*.md 是否推（建议不推）。
3. Notion 写操作：03/05 只增列（新增 05“类型=认可素材”选项）、02/05 写入新记录。
4. 认可素材复用 05 而不新建库。
5. 评分改 v3-dual（新增国内适用性/新技术机会/副业潜力 3 维，传播潜力假设降为不计分）。
6. 官方源首批名单（建议 ≤5 个）。
7. 文案默认改用 GLM（会产生模型调用费用，按实际记录）。
8. Codex CLI 用于独立只读审查（消耗用户 ChatGPT 额度）。
9. 最终 PR 合并到 main。
