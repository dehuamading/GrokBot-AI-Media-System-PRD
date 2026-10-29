# 工作流设计文档（任务 03：搭建工作流程序）

> 版本：1.0（2026-10-30）｜ 对应 PRD：GrokBot_AI自媒体系统_PRD_V1.0 §2/§4/§5/§6/§7/§9
> 状态：已实现（除标注「未完成」部分）｜ 测试：全部用假数据/mock，无真实批量抓取

## 1. 总体流程图

```text
┌────────────────────────────────────────────────────────────────────────────┐
│                         一次完整工作流运行（手动触发，无定时）                │
└────────────────────────────────────────────────────────────────────────────┘

 [S1 数据抓取 collect]
   aihot.news /api/v1（≤20 条/轮，匿名只读）──┐
   X 关注名单文件（接口化，只读文件，绝不访问 X）┤
                                              ↓
 [S2 分析 analyze]
   标准化(已有 normalize) → 去重(item_id+URL) → 分类(classify)
   → 七维可解释评分(scoring v1-heuristic) → 生成 5–10 张选题卡(topics)
                                              ↓
 [S3 写入 Notion write_topics]（幂等 upsert）
   01_原始素材 更新分数/状态；03_选题决策 写入选题卡（用户选择结果=待用户选择）
                                              ↓
 ════════════════════ ⏸ 第一人工关口 GATE-1 ══════════════════════════════════
   用户在 Notion 03_选题决策 中把 1–3 张卡改为「已选定」（可另指定平台）。
   程序任何自动流程到此必须停止；未读到「已选定」不进入 S4。
                                              ↓
 [S4 读取审核结果 gate1]（只读查询 Notion）
   查询 用户选择结果=已选定 的行；1–3 张通过校验，否则继续等待。
                                              ↓
 [S5 写文案 draft]（每题×每平台仅 1 份初稿，PRD 6.1）
   LLMClient（默认=离线模板引擎 template；可选=openai-compat，用户自备 API）
   → 初稿 V1（标题/正文/事实核对清单/来源链接/未证实内容提醒）
   → 幂等写入 04_文案与版本（状态=V1，待用户确认）+ 页面正文块
                                              ↓
 ════════════════════ ⏸ 第二人工关口 GATE-2 ══════════════════════════════════
   用户在 Notion 04_文案与版本 中审阅：改状态=已确认 或 反馈修订（状态=修订中）。
   ❌ 绝不发布：系统没有任何发布到社交媒体的代码路径（红线）。
                                              ↓
 [S6 修订循环 revise]  ──────────────── ⬛ 未完成（P4 范围，见 §8）
 [S7 复盘 review]      ──────────────── ⬛ 未完成（P5 范围，见 §8）
```

每步落盘 `data/state/workflow_state.json`（本地，不入库），支持断点续跑与崩溃恢复；
所有 Notion 写操作幂等（先按 title 查询再 upsert），重跑不重复入库（PRD 9.3）。

## 2. 模块清单

| 模块 | 路径 | 职责 | 网络 |
|---|---|---|---|
| aihot 连接器 | `src/connectors/aihot_source.py` | /api/v1 精选条目拉取/文件回放，归一化 | 可选（`--live` 才联网） |
| X 名单连接器 | `src/connectors/x_following_source.py` | 仅从文件读关注名单（接口化降级模式） | 无（永不访问 X） |
| 标准化层 | `src/normalize.py` | 统一素材格式、URL 规范化、item_id、去重 | 无 |
| 分类器 | `src/analysis/classify.py` | 素材→内容类型（工具教程/产品发布/行业动态/研究解读/观点评论/安全事件） | 无 |
| 评分引擎 | `src/analysis/scoring.py` | PRD 5.3 七维可解释评分→推荐级别（推荐/备选/观察） | 无 |
| 选题引擎 | `src/analysis/topics.py` | 高分素材→5–10 张选题卡（PRD 5.4 全字段） | 无 |
| LLM 接口 | `src/llm/base.py` | DraftClient 协议（可替换） | — |
| 模板引擎 | `src/llm/template_client.py` | 离线确定性初稿生成（默认，零成本） | 无 |
| OpenAI 兼容引擎 | `src/llm/openai_compat.py` | 通用 OpenAI 兼容 API 适配（用户自备 key 才启用） | 可选（默认禁用） |
| Notion 客户端 | `src/notion_store/client.py` | 限流/重试/幂等 upsert/追加正文块/只增列 | 是（测试全 mock） |
| 知识库结构 | `src/notion_store/bootstrap.py` | 幂等建库 + 幂等升级（只增属性不删） | 是（测试全 mock） |
| 人工关口 | `src/notion_store/gates.py` | 读 GATE-1 选择结果 / GATE-2 确认结果；写文案行 | 是（测试全 mock） |
| 工作流编排 | `src/workflows/pipeline.py` | S1–S6 各步骤函数（每步注入 mockable 依赖） | 取决于依赖 |
| 状态机 | `src/workflows/state.py` | 阶段/游标/关口快照持久化与恢复 | 无 |
| CLI 入口 | `src/workflows/run.py` | collect / gate1 / draft / gate2 / status / selfcheck 子命令 | — |

## 3. 数据结构（核心 JSON）

### 3.1 统一素材（沿用任务02 `schemas/source-item.schema.json`）
`item_id / source_type / source_url / author_or_publisher / published_at / collected_at /
title_or_summary / claims[] / evidence_status(verified|self_reported|third_party|unverified) /
processing_status(new|screened|candidate|archived) / extra{category, aihot_score, …}`

分析阶段新增（内存与 Notion 同步）：
- `content_type`：分类结果 + `classify_reason`
- `score`：`{total, level(推荐|备选|观察), version:"v1-heuristic", dimensions:[{name, grade(高|中|低), points, reason}]}`

### 3.2 选题卡（`schemas/topic-card.schema.json`，PRD 5.4 全字段）
`topic_id(T-YYYYMMDD-NNN) / 拟定标题 / 建议平台(抖音|小红书|两者) / 面向谁 /
核心价值 / 为什么现在 / 关联素材[item_id] / 需求证据（无直接证据必须写「待验证…」）/
可信度与风险 / 最强反证 / 差异与难度 / 推荐级别 / 推荐理由 / 用户选择结果(待用户选择)`

### 3.3 文案初稿（内存结构 → Notion 04 行 + 页面正文块）
```json
{
  "draft_id": "D-<topic_id>-<douyin|xhs>-v1",
  "topic_id": "T-20261030-001", "platform": "抖音|小红书", "version": 1,
  "status": "V1",
  "engine": "template@v1 | openai-compat:<model>",
  "title": "…", "body": "…",
  "fact_checklist": ["…出处…"],
  "sources": ["https://…"],
  "unverified_warnings": ["…未证实内容提醒…"]
}
```

### 3.4 工作流状态（`data/state/workflow_state.json`，本地不入库）
```json
{
  "run_id": "2026-10-30T…", "stage": "init|collected|waiting_gate1|drafted|waiting_gate2",
  "page_root": "408d7d3b…", "db_ids": {"01_原始素材": "…", …},
  "last_collect": {"materials": 20, "dropped": 0, "accounts": 135, "snapshot": "data/raw/run_…/materials.json"},
  "last_analyze": {"cards": 8, "scoring_version": "v1-heuristic"},
  "gate1": {"status": "waiting|passed", "selected": [{"topic_id": "…", "platforms": ["抖音"]}]},
  "gate2": {"status": "waiting|passed", "drafts": [{"draft_id": "…", "status": "V1"}]}
}
```

## 4. Notion 库结构（复用任务02已建 7 库，仅「只增列」升级）

| 库 | 用途（本工作流读写） | 关键列 | 本次升级 |
|---|---|---|---|
| 00_X关注账号名单 | X 情报源注册表（只写） | username… | 无 |
| 01_原始素材 | 素材池（S1 写入 / S3 更新分数） | item_id, screen_score… | 无 |
| 02_用户需求 | 需求证据库（本轮未写入，S2 卡片需求证据标注「待验证」） | — | 无 |
| 03_选题决策 | 选题卡（S3 写入 / **GATE-1 用户改选择结果**） | topic_id…用户选择结果 | ➕`用户指定平台`(select: 抖音/小红书/两者) |
| 04_文案与版本 | 初稿（S5 写入 / **GATE-2 用户审**） | draft_id, platform, version, status(V1/修订中/已确认) | ➕`生成引擎`(rich_text) |
| 05_经验与复盘 | 规则/经验种子（S0 一次性写入，已存在） | — | 无 |
| 08_维护与审计 | 每步操作审计（S1–S5 各写一条） | event_id… | 无 |

**升级方式**：`update_database_schema` 仅新增缺失属性，绝不删除/修改既有列与既有数据
（对齐「不删除既有资料」红线）。

## 5. 每步输入输出与停止条件

| 步骤 | 输入 | 输出 | 人工关口/停止条件 |
|---|---|---|---|
| S1 collect | aihot 响应文件或 `--live` 拉取（≤20）；X 名单文件 | 素材快照 `data/raw/run_<ts>/`、账号列表、state.stage=collected、Notion 01/00 幂等 upsert、08 审计 | 无；`--dry-run` 时完全离线 |
| S2 analyze | 素材快照 | 打分结果 + 5–10 张选题卡、state.stage=waiting_gate1 | 纯本地；评分版本固定 `v1-heuristic`，显式标注「启发式假设，非已验证模型」 |
| S3 write_topics | 选题卡 + NotionClient | 03 新行/更新（用户选择结果=待用户选择） | **GATE-1 开始**：程序退出，提示用户去 Notion 勾选 |
| S4 gate1 | 只读查询 03 | 已选定选题列表（含用户指定平台）；校验 1–3 张 | 未选定 → 输出「等待用户选择」并退出码 0；选定 → 可进入 S5 |
| S5 draft | 已选定选题×平台、素材事实、LLMClient | 04 行（version=1, status=V1）+ 正文块；每题×每平台恰好 1 份；08 审计 | **GATE-2 开始**：生成后程序停止，无任何发布动作 |
| S6 gate2 | 只读查询 04 | 各稿状态（V1/修订中/已确认）汇总 | 全部=已确认 → 流程闭环（无后续动作）；=修订中 → 修订循环（未完成，P4） |

## 6. 人工关口设计（不可绕过）

1. **GATE-1（选题）**：S3 结束即退出；S5 入口强制执行 `gate1.passed` 校验——
   必须从 Notion **实时读取**到 1–3 张「已选定」卡才继续，本地状态不能替代
   （防止用本地文件伪造审核）。用户也可把卡片改为「未选用」表示否决。
2. **GATE-2（终稿）**：S5 之后无任何自动步骤；代码中不存在发布接口，
   `grep -r publish` 无发布路径（测试断言）。用户确认/反馈都在 Notion 完成。
3. 两道关口的读操作只认 Notion 单一事实源；08_维护与审计 记录每次关口判定。

## 7. 红线落实

| 红线 | 落实方式 |
|---|---|
| 不访问 X | X 连接器只有文件读取接口；无任何 HTTP→x.com 代码；测试断言 requests 不被调用 |
| 不批量抓取 | S1 默认离线文件回放；`--live` 需显式传入且 limit≤20；本阶段仅 selfcheck 用 1 条真实请求 |
| 不建定时任务 | 仓库无 cron/systemd/GitHub Actions schedule（CI 仅 push/pr 触发） |
| 不发布内容 | 无发布代码路径；文案只写 Notion 04 且状态为 V1 待审 |
| 不删既有资料 | Notion 只 upsert/只增列；本地不删 data/raw |
| 密钥不入库/不打印 | token 仅从 NOTION_TOKEN 环境变量读取；.gitignore 屏蔽；healthcheck 有泄漏扫描 |
| 不购买服务 | 默认 LLM=离线模板引擎；openai-compat 需用户自备 key 并显式启用（本阶段未启用） |

## 8. 未完成（后续阶段）

- S6 修订循环（V1→V2→…按 Notion 反馈修订同一稿件）：P4
- S7 发布后复盘与知识迭代（OODA）：P5（发布本身永远人工）
- 海外案例/GitHub/飞书等 C/D 级连接器：仅留接口位
- 真实 LLM 接入：接口已留（openai-compat），待用户提供 API（待确认事项 #3）
