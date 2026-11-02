# 第一阶段 现状核查报告（2026-11-02 14:35–15:00 美东时间，Grok Bot）
> 只读核查：未改代码、未写 Notion、未推送、未建分支。凡未做的标「未完成」。

## 1. Git 与文件同步
命令与结果（repo=/workspace/ai-zimeiti/repo）：
```
git fetch origin            → 无新内容
git status -sb              → ## main...origin/main（干净，无修改/无未跟踪）
git rev-list --left-right --count HEAD...origin/main → 0 0
git diff origin/main --stat → 空
git stash list              → 空；分支仅 main / origin/main
HEAD = a5300b6（任务05）    共 6 个提交，69 个跟踪文件
git status --ignored        → 仅忽略 data/raw/ data/exports/ data/state/（运行数据，正确忽略）
```
其他副本：全盘（/workspace、/home/box，排除 node_modules）只找到这一个 origin 指向本仓库的克隆，无更新版本藏在别处。
结论：**本地与远程完全一致，没有未推送提交，也没有远程独有提交。** 旧阶段报告里写的“推送 403 未完成”已过时（远程已有 a5300b6）。

仓库外、看似项目成果的文件（/workspace/ai-zimeiti 下）：
| 文件 | 内容 | 分类 |
|---|---|---|
| PRD_V1.0.md | 与 repo/docs/PRD_V1.0.md 逐字节相同 | 已在库，无需处理 |
| task-01…05-*.md | 历次给 Pi 的任务书 | 应推送（建议放 docs/tasks/，先人工看一遍） |
| tasks/upgrade-v2-master-instruction.md | 本次升级指令摘要 | 应推送 |
| report-04-explain.md | 系统讲解报告；token 均已打码；含 1 个 x.com 链接 | 应推送（推前复核链接是否公开帖） |
| reflection-01-depth.md | 深度反思（认可素材库/偏好闭环建议） | 应推送 |
| pi-reply-01/03/04/05.md | Pi 原始回复日志；05 含 8 个真实 X 链接 | 用途不明（过程日志，建议不推或脱敏后摘要推） |
| pi-reply-02.md | 空文件 | 不应推送 |
| pi-stderr-*.log, .model-usage.log | 运行日志 | 不应推送（.gitignore 已覆盖 *.log） |
| inputs/x_following.json, x_posts_24h*.json | 真实 X 数据（753+ 条帖子） | 不应推送（含私人/未脱敏数据） |
| outputs/picks_20261101.py, notion_urls_20261101.json | 真实选题内容与 Notion 链接 | 不应推送 |
| /workspace/research/mediacrawler/report.md | 国内平台调研报告 | 应推送（仅报告，不推 raw/ 与 MediaCrawler 代码，后者许可禁商用） |
| phase1/*.md（本目录） | 本阶段三份文档 | 应推送（第二阶段分支第一个提交） |
.gitignore 已覆盖 .env/.secrets/*token*/*secret*/cookies/inputs/data 运行目录/*.log，覆盖充分；缺口：outputs/ 不在仓库内所以无风险，但若将来搬进仓库需加入忽略。

## 2. 功能清单（代码事实 vs 文档）
| 能力 | 代码 | 文档 | 实际状态 |
|---|---|---|---|
| AIHot 采集 | src/connectors/aihot_source.py（/api/v1/items，mode=selected，limit≤20 硬限制） | README ✅ | 正常；本次实测 1 条 HTTP 200 |
| X 关注名单导入 | connectors/x_following_source.py（读本地 JSON） | ✅ | 可用；Notion 00 库 135 行 |
| X 帖子文件导入 | connectors/x_posts_source.py（纯本地，必填 id/url/text/created_at） | ✅ | x_posts_24h.json（1043 条，扁平字段）可用；**发现缺陷**：x_posts_24h_20261101.json（753 条）是 X API 原样格式（public_metrics 嵌套、username），normalize_x_post 只认扁平字段 → 作者为空、互动数全为 0（读代码确认，未运行） |
| 标准化/去重 | src/normalize.py | 部分 | 可用 |
| 七维评分 | analysis/scoring.py，版本 v2-audience | ✅ | 见下 |
| 受众画像 v2 | analysis/audience.py + notion_store/audience_page.py | ✅ | 画像 audience-zh-ai-creator-v1；**只覆盖旧受众，未含“副业创业”“国外工具国内用”两类** |
| GLM 分析引擎 | analysis/llm_topics.py + llm/glm.py（短名单→GLM 打 0–10 分→生成卡） | ✅ | 上轮真实用过（glm-5.1，7 次调用） |
| Notion 7 库 | notion_store/bootstrap.py（建库+只增列升级） | ✅ | 见第 4 节 |
| 两道人工关口 | notion_store/gates.py；run.py gate1/gate2 | ✅ | 机制在；**GATE-1 从未被真正通过**（36 张卡全是“待用户选择”） |
| 文案生成 | llm/template_client.py（默认）/ openai_compat.py（可选） | ✅ | **默认是模板，不是 GLM**；04 库 0 行，初稿从未真实生成 |
| 经验与复盘 | 只在 bootstrap 建库、backup 导出 | README 称“结构” | **代码不读也不写 05**，未接入流程 |
| 02 用户需求 | 只建库 | 文档注明“当前为空” | 未接入，0 行 |
| 修订循环 S6 / 复盘 S7 | 无 | 报告标未完成 | 未实现 |
| GitHub/飞书来源 | 无代码，仅配置桩 | source-permissions.md 标“未配置” | 未实现 |
| 备份导出 / 健康检查 | backup/export_notion.py；scripts/*.sh | ✅ | 存在，本次未运行 |

七维（每维 高2/中1/低0，等权，总分 0–14；≥10 推荐、≥7 备选、否则观察；命中负面信号再扣 2×min(2,个数)）：
1 真实用户价值（可上手关键词）2 信息可信度（证据等级）3 受众匹配（画像信号）4 新颖与时效（2天/7天）5 可讲清与可验证（有出处+有步骤词）6 传播潜力假设（张力词，明确标为未验证）7 制作成本（反向）。

## 3. 测试（真实运行）
```
python3 -m pytest -q   → 70 passed in 0.53s
```
注意：70 项全部是假数据/mock 单元测试，不联网。
`selfcheck` 读代码后**未运行**：它会写 Notion（只增列升级 + 写一条审计），违反本阶段只读。`status`（不带 --live）只读本地状态，已运行：stage=collected，上轮 1063 条素材（X 1043），GATE-1 waiting，已选 0。

## 4. Notion 实际状态（只读查询，2026-11-02）
| 库 | 行数 | 字段数 | 要点 |
|---|---|---|---|
| 00_X关注账号名单 | 135 | 8 | username/name/followers/processing_status… |
| 01_原始素材 | 32 | 16 | item_id/source_type/evidence_status/screen_score…（只写了入选的素材，不是全部 1063 条） |
| 02_用户需求 | 0 | 6 | problem_id/problem/user_type/evidence_url/status/sample_limit |
| 03_选题决策 | 36 | 20 | T01–T08 共 8、TX-20261030-* 8、TX-20261101-* 20；推荐 26/备选 10；**用户选择结果 36 张全是“待用户选择”** |
| 04_文案与版本 | 0 | 9 | 含 生成引擎 列 |
| 05_经验与复盘 | 4 | 9 | 规则3/经验1；正在使用3/待验证1；性质 用户偏好3/AI推断假设1 |
| 08_维护与审计 | 5 | 6 | 全部 success |
03 现有列：topic_id、拟定标题、推荐级别、综合评分、关联素材、引用帖子、需求证据、面向谁、为什么适合、为什么现在、国内可用性、核心价值、切入角度与钩子、推荐理由、差异与难度、可信度与风险、最强反证、建议平台、用户指定平台、用户选择结果。
知识库导航 / 用户画像 页面：本次未逐页读取（「未完成」，非必需）。

## 5. 连接能力（真实请求）
- AIHot：`GET /api/v1/items?mode=selected&limit=1&window=7d` → 200，1.4 秒，返回正常条目。
- GitHub：带 token `GET /rate_limit` → core 5000/h（剩 4992），search 30/min；`search/repositories?q=topic:mcp pushed:>2026-10-25` → 正常返回 total_count 与 stars/license；不带 token 也 200（60/h）。
- X（Grok Bot 内置公开读，2 次调用）：
  - `get_posts_by_id` → 正文、发布时间、作者 id、互动数（点赞/转发/回复/引用/收藏/浏览）、引用的原帖与视频地址 ✅
  - `search_posts_all` + `conversation_id:<id> is:reply` → 公开回复 10 条/页（含正文、语言、作者、各自互动数、next_token 可翻页） ✅
  - 限额：每分钟 30 次、每天 1000 次读取（按用户计）。每次最多约 100 条（本次用 10）。
  - 限制：回复按时间倒序，热门帖有几千条回复时只能抽样读前几页，**读不全**；不能按点赞排序回复；不能读私密账号；用户本人的 X 连接（user-X）可读首页时间线/提及/书签/列表。只读，不发帖不点赞不私信。
- Codex CLI：已安装 codex-cli 0.165.0，`codex login status` → Logged in using ChatGPT（未消耗额度，额度余量未知）。
- Pi：pi-auto 在 /home/box/.local/bin；历史日志显示 glm-5.1 为主、deepseek-flash 兜底。

## 6. 已有报告中的未完成事项
1. S6 修订循环（V1→V2）未实现；2. S7 发布后复盘/知识迭代未实现；3. 真实 LLM 写稿未启用（默认模板）；4. 海外案例源、GitHub、飞书入口只有配置桩；5. 02 用户需求为空，需求证据采集未开始；6. GATE-1 从未通过、初稿从未生成；7. 反思报告建议的“认可素材库 + 偏好闭环 + 国内可复现栏 + 数字验收”均未做；8. MediaCrawler 调研：抖音/小红书不登录拿不到、MediaCrawler 禁商用、巨量算数/小红书热榜「未完成」，只有 B站公开接口勉强可行。
（“推送 403”已解决，旧报告未更新。）
