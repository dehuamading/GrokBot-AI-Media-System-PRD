# 讲解报告：GrokBot AI 自媒体系统（写给非程序员用户）

> 任务 04 产出 ｜ 2026-10-30 ｜ 依据：仓库 `/workspace/ai-zimeiti/repo` 的代码/文档/测试（42 项测试实际运行全部通过），以及 Notion「AI 自媒体系统」页面的**只读实时查询**结果。
> 本文只描述已存在的事实；凡是没做完的，都明确标了「未完成」。

---

## 一、整体框架：一句话 + 流程图

**一句话**：这是一套「替你盯着 AI 圈新闻 → 自动挑出好选题 → **你**拍板选 1–3 个 → 自动写出初稿 → **你**审阅确认」的中文 AI 自媒体辅助系统；它把所有资料和稿件都存在你的 Notion 里，**完全没有自动发布功能**，发布永远由你手动完成。

```text
[S1 抓取] aihot.news 精选条目(每轮≤20条) + X关注名单文件(只读文件,绝不访问X)
      │
      ▼
[S2 分析] 统一格式 → 去重 → 自动分类(6类) → 七维打分(推荐/备选/观察)
      │
      ▼
[S3 写入 Notion] 素材和分数进「01_原始素材」；5–10张选题卡进「03_选题决策」
      │
      ▼
╔══════════ ⏸ 第一人工关口 GATE-1 ═══════════════════════════╗
║ 你打开 Notion「03_选题决策」，把中意的 1–3 张卡改成「已选定」 ║
║ （也可以顺手指定平台：抖音/小红书/两者）                      ║
╚═══════════════════════════════════════════════════════════╝
      │
      ▼
[S4 读取审核结果] 程序只读查询 Notion；没见到「已选定」就一直等
      │
      ▼
[S5 写初稿] 每个选题×每个平台只写 1 份初稿（含标题/正文/事实核对清单/
      │        来源链接/未证实内容提醒），写入「04_文案与版本」，状态=V1 待审
      ▼
╔══════════ ⏸ 第二人工关口 GATE-2 ═══════════════════════════╗
║ 你在 Notion「04_文案与版本」审阅：改成「已确认」或「修订中」   ║
║ ❌ 系统没有任何发布到社交媒体的代码，绝不会自动发布            ║
╚═══════════════════════════════════════════════════════════╝
      │
      ▼
[S6 关口观测] 程序只读汇总各稿状态（待审/修订中/已确认）
      └→ 修订循环(V1→V2…) ⬛ 未完成（P4）；发布后复盘 ⬛ 未完成（P5）
```

另有两道贯穿始终的安全设计：
- **幂等**：所有写 Notion 的操作都是「先查有没有、有就更新、没有才新建」，程序重跑多少遍都不会产生重复数据。
- **断点续跑**：每一步都把进度存在本地文件 `data/state/workflow_state.json`，中断后可从断点继续。

---

## 二、编写了哪些程序（逐文件说明）

全部代码在 `repo/src/` 下，共 4 层：连接器 → 标准化 → 分析 → Notion 存储，加一个工作流编排层。运行时唯一外部依赖是 `requests`（requirements.txt 里只有 requests 和 pytest 两个包）。

### 1. 信息源连接器 `src/connectors/`

| 文件 | 作用 | 输入 | 输出 | 能产生什么 |
|---|---|---|---|---|
| `aihot_source.py` | 从 aihot.news 的公开接口 `/api/v1/items` 拉取精选条目；也能从本地文件回放（默认离线模式） | 接口地址、条数上限（**写死 ≤20**）、时间窗口 7 天；或一个已保存的 JSON 文件 | 统一格式的素材列表 | 一轮最多 20 条 AI 新闻素材（标题、原始出处链接、发布者、发布时间等） |
| `x_following_source.py` | 只从**本地文件**读取你的 X（推特）关注账号名单，做去重 | `inputs/x_following.json`（135 个账号） | 账号列表（username/名称/简介/粉丝数） | X 情报源注册表数据。**绝不访问 X 网站**——代码里没有任何连接 x.com/twitter.com 的语句（有专门测试守着这条红线） |

### 2. 标准化层 `src/normalize.py`

| 作用 | 输入 | 输出 | 能产生什么 |
|---|---|---|---|
| 把不同来源的素材变成统一格式；清洗 URL（去掉跟踪参数）；生成稳定编号 `item_id`；按编号+URL 双重去重；校验必填字段；以及一个透明的关键词初筛打分（0–10 分，每分都给理由） | 连接器产出的原始记录 | 合法、去重后的统一素材 | 干净的素材池，以及每条的初筛分数和「为什么给这个分」的文字理由 |

### 3. 分析层 `src/analysis/`

| 文件 | 作用 | 输入 | 输出 | 能产生什么 |
|---|---|---|---|---|
| `classify.py` | 自动把素材分成 6 类：工具教程/产品发布/行业动态/研究解读/观点评论/安全事件（规则可解释：关键词+类目+来源名，判不出就保守归入「行业动态」） | 单条素材 | (类型, 判断理由) | 每条素材的内容类型，决定后续用哪套标题模板和平台建议 |
| `scoring.py` | **七维可解释评分**：真实用户价值/信息可信度/受众匹配/新颖与时效/可讲清与可验证/传播潜力(**明确标注是假设**)/制作成本。每维只有 高(2)/中(1)/低(0) 三档并给理由，总分 0–14 → 推荐级别（≥10 推荐 / ≥7 备选 / 其余观察）。版本号 `v1-heuristic`，代码里明确写着「启发式假设，未验证，仅用于排序辅助，不承诺传播效果」 | 单条素材+当前时间 | 分数、级别、七个维度各自的理由 | 排序依据；不会伪装成「爆款预测」 |
| `topics.py` | 按总分取前 5–10 名素材，生成**选题卡**（PRD 5.4 全部 14 个字段）。诚实性约束写死在代码里：没有需求证据必须写「待验证：…」；「最强反证」必填（取最弱两维的理由）；平台建议标注为初始假设 | 素材池 | 选题卡列表 + 打分明细 | 5–10 张完整选题卡：拟定标题、建议平台、面向谁、核心价值、为什么现在、关联素材编号、需求证据、可信度与风险、最强反证、差异与难度、推荐级别与理由 |

### 4. LLM 文案引擎 `src/llm/`

| 文件 | 作用 | 输入 | 输出 | 能产生什么 |
|---|---|---|---|---|
| `base.py` | 定义「文案引擎」的统一接口（DraftClient）——任何引擎都只负责**产文本**，接口层面就不存在发布能力；还定义了初稿 Draft 的结构（标题/正文/事实核对清单/来源链接/未证实内容提醒） | — | — | 可替换的引擎插座 |
| `template_client.py` | **默认引擎：离线模板**。零网络、零成本，按抖音口播稿/小红书图文两套模板，用选题卡+素材原文拼装成稿；不编造数字，无法溯源的内容进「未证实提醒」 | 平台、选题卡、关联素材 | 完整初稿（V1） | 一份结构完整、事实可溯源、带提醒清单的初稿。文风是模板化的，质量属于「能用的起点」 |
| `openai_compat.py` | 可选引擎：接任何 OpenAI 兼容接口（**用户自备 API key 才会启用**；环境变量 LLM_API_BASE/LLM_API_KEY/LLM_MODEL 缺一就报错，绝不静默联网）。key 只进请求头，不进日志 | 同上 | 同上 | 更自然的初稿。**当前未配置、从未启用（未完成：等用户提供 API）** |

### 5. Notion 存储层 `src/notion_store/`

| 文件 | 作用 | 输入 | 输出 | 能产生什么 |
|---|---|---|---|---|
| `client.py` | Notion 官方接口的客户端：自动限速（约 2.9 请求/秒，低于官方 3/秒上限）、429/5xx 自动重试（最多 4 次）、**幂等 upsert**（先按唯一键查询再更新/新建）、只增列升级（只加新列，绝不删改旧列）。token 只从环境变量 `NOTION_TOKEN` 读，绝不打印 | token（环境变量） | 各种 API 调用 | 安全、可重跑的 Notion 读写能力 |
| `bootstrap.py` | 知识库结构定义：在授权页「AI 自媒体系统」下幂等地建 7 个数据库（见第四节）+ 1 个「📖 知识库导航」页；任务 03 又定义了两处「只增列」升级：03 库加「用户指定平台」、04 库加「生成引擎」 | 页面 ID | {库名: 库ID} | 一套完整知识库骨架；重跑不会重建、不会破坏 |
| `gates.py` | **两道人工关口的实现**。GATE-1：实时只读查询 03 库里「用户选择结果=已选定」的卡，还原成完整选题卡（校验 1–5 张）；GATE-2：只读汇总 04 库各稿状态；另含把初稿（行属性+正文块）幂等写入 04 库的函数。文件头注释明确：Notion 是唯一事实源，本地状态不能替代；本模块无任何发布能力 | 库ID | 已选定选题/稿件状态汇总 | 关口判定。程序**只认你在 Notion 里的实际操作** |

### 6. 工作流编排 `src/workflows/`

| 文件 | 作用 |
|---|---|
| `pipeline.py` | S1–S6 每一步都是一个独立函数：`step_collect`（抓取+存快照）、`step_analyze`（分析+选题卡）、`step_write_topics`（写 Notion 01/03）、`step_gate1`（读关口一）、`step_draft`（生成初稿写 04）、`step_gate2`（关口二观测）。所有外部依赖从外面注入，测试时换成假的即可全程离线 |
| `state.py` | 本地状态机：阶段只能在 init→collected→waiting_gate1→drafted→waiting_gate2 之间合法流转（跳关会直接报错）；任何阶段都可以重新 collect 开新一轮 |
| `run.py` | **命令行入口**，6 个子命令：`collect` / `gate1` / `draft` / `gate2` / `status` / `selfcheck`（用法见第三节） |

### 7. 备份与脚本

| 文件 | 作用 |
|---|---|
| `src/backup/export_notion.py` | 把 Notion 的 01/03/05/08 四个库导出成 JSON+CSV 存到本地 `data/exports/`（已实际运行过，文件在仓库 data/exports/ 可见，但该目录不入公开 git 库） |
| `scripts/bootstrap.sh` | 一键引导：装依赖 → 跑全部单元测试 → 用假数据 dry-run 一轮（全程不写 Notion、不联网抓取） |
| `scripts/healthcheck.sh` | 健康检查 6 项：网络、aihot 接口、GitHub 远端、Notion 根页面可读、本地状态文件、密钥泄漏扫描（扫描 git 已跟踪文件里是否混入 token 样式的字符串） |
| `scripts/export_notion.sh` | 备份导出的安全包装（强制要求 NOTION_TOKEN 已注入） |
| `scripts/restore_check.sh` | 灾难恢复演练脚本（clone→测试→健康检查→幂等验证的检查清单） |

### 8. 其他资产

- `schemas/`：3 个 JSON 数据规格（素材、选题卡、初稿），程序按规格校验数据；
- `prompts/`：4 份提示词/写作假设文档（选题筛选、抖音稿、小红书稿、复盘），是模板引擎和未来接真 LLM 时的「写作规范」；
- `config/`：环境变量模板和信息源配置模板（只有变量名，无任何真实密钥）；
- `.github/workflows/ci.yml`：GitHub 自动化检查——每次 push/PR 自动跑测试和 dry-run（**刻意不用定时触发**，红线）；
- `tests/`：**42 项单元测试，本次实际运行全部通过（42 passed）**，全部离线假数据/mock。其中 8 项是「红线守卫测试」：自动检查代码里没有访问 X 的路径、没有发布函数、CI 没有定时任务、.gitignore 覆盖密钥、样例必须是假数据、仓库文件里没有密钥字符串、代码里只允许出现 aihot.news 和 api.notion.com 两个域名。

---

## 三、工作流程 S1–S6：每步做什么、你在哪里介入、怎么运行

所有命令都在 `repo/` 目录下执行；凡是连 Notion 的命令，先注入密钥（这行命令本身不会泄露密钥）：

```bash
cd /workspace/ai-zimeiti/repo
export NOTION_TOKEN=$(cat /workspace/.secrets/notion_token)
```

| 步骤 | 程序做什么 | 你的介入 | 运行命令 |
|---|---|---|---|
| **S1 抓取** | 从 aihot 拉/回放素材（≤20 条），读 X 名单文件（135 账号），标准化+去重，快照存本地 | 无 | `python3 -m src.workflows.run collect --aihot-file data/examples/aihot_fake_sample.json --x-following /workspace/ai-zimeiti/inputs/x_following.json`（默认离线回放假数据；加 `--live` 才真实抓取） |
| **S2 分析** | 分类、七维评分、生成 5–10 张选题卡 | 无 | （S1 命令自动包含）想先看看效果不写库：同命令加 `--dry-run` |
| **S3 写库** | 素材/分数写 01 库，选题卡写 03 库（全部=「待用户选择」），并写一条审计到 08 库 | 无 | （同上，collect 一步到关口一为止） |
| **⏸ GATE-1** | 程序自动退出，打印提示 | **你**：打开 Notion「03_选题决策」，把 1–3 张卡的「用户选择结果」改成「已选定」，需要的话在「用户指定平台」里指定抖音/小红书/两者 | （在 Notion 网页/App 里手动操作） |
| **S4 读关口一** | 只读查询 03 库；没检出「已选定」就提示等待并退出（退出码 0）；检出 1–5 张则通过 | 无（也可以只运行这一步看状态） | `python3 -m src.workflows.run gate1` |
| **S5 写初稿** | 对每张已选定选题×每个平台生成恰好 1 份初稿，写入 04 库（状态=V1）+正文块，写审计。默认用离线模板引擎（`--llm template`） | 无 | `python3 -m src.workflows.run draft`（=S4+S5 连跑） |
| **⏸ GATE-2** | 程序停止。系统不存在任何发布动作 | **你**：在 Notion「04_文案与版本」审阅，把状态改成「已确认」，或改「修订中」并填反馈 | （在 Notion 里手动操作） |
| **S6 关口观测** | 只读汇总各稿状态：待审/修订中/已确认 | 无 | `python3 -m src.workflows.run gate2` |

辅助命令：

```bash
python3 -m src.workflows.run status            # 看本地进度；加 --live 附带 Notion 关口实况
python3 -m src.workflows.run selfcheck         # 连通性自检（会向 aihot 发 1 条真实请求 + 检查 Notion 结构）
./scripts/bootstrap.sh                          # 装依赖+跑测试+dry-run（新环境第一步）
./scripts/healthcheck.sh                        # 6 项健康检查
./scripts/export_notion.sh                      # Notion 备份导出为 JSON/CSV
```

**重要说明**：程序不会自己定时运行。每次都要你手动敲命令；两道关口更是「程序停下等你」的设计，本地状态无法伪造你的审核结果。

---

## 四、Notion 知识库现状（2026-10-30 只读实时查询）

### 怎么建的

由程序 `bootstrap.py` 在你授权的页面「AI 自媒体系统」（page id `408d7d3b292381068024e0548308c638`，创建于 2026-10-29）之下自动建立：**7 个数据库 + 1 个「📖 知识库导航」页**。建库过程幂等（已存在就复用，不重建）；任务 03 只做了「只增列」升级（03 库加「用户指定平台」、04 库加「生成引擎」，已实际生效），**没有删除或修改过任何既有列和数据**。

### 各库用途、主要字段与当前行数（实测）

| 库 | 用途 | 主要字段 | 当前行数 |
|---|---|---|---|
| 00_X关注账号名单 | X 情报源注册表（降级模式：只有账号，没有帖子内容） | username(编号)、name、description、followers、processing_status | **135** |
| 01_原始素材 | 素材池：S1 写入、S3 更新分数 | item_id(编号)、来源类型/URL、发布者、发布时间、标题、claims、证据等级、处理状态、screen_score、aihot_score、类目 | **20**（处理状态：13 candidate / 7 screened；证据等级：5 verified / 3 third_party / 12 unverified） |
| 02_用户需求 | 真实用户问题与证据 | problem_id、user_type、problem、evidence_url、status | **0（空：需求证据采集未开始，选题卡需求一律标「待验证」）** |
| 03_选题决策 | 选题卡：S3 写入；**GATE-1 你在这里勾选** | topic_id(编号)、拟定标题、建议平台、面向谁、核心价值、为什么现在、关联素材、需求证据、可信度与风险、最强反证、差异与难度、推荐级别、用户选择结果、➕用户指定平台 | **8（全部「待用户选择」；6 推荐 + 2 备选）** |
| 04_文案与版本 | 初稿版本链：S5 写入；**GATE-2 你在这里审** | draft_id(编号)、topic_id、platform、version、status(待生成/V1/修订中/已确认)、feedback_summary、change_summary、➕生成引擎 | **0（初稿从未真实生成过）** |
| 05_经验与复盘 | 规则/经验/复盘知识，区分性质（用户偏好/AI推断假设/已验证经验等）与状态（待验证→已验证→正在使用→需要复核→已过时） | record_id、类型、适用范围、内容、依据、状态、性质、version | **4**（3 条规则 + 1 条经验：RULE-001 每题每平台一稿、RULE-002 两道人工关口、RULE-003 Pi 不访问 X、HYP-001 初筛启发式未验证） |
| 08_维护与审计 | 系统操作日志 | event_id、对象、操作、结果、详情、occurred_at | **3**（均为 success：2 条 2026-10-29 的采集事件 + 1 条任务03 selfcheck） |

### 库与库之间的关系

用**编号互相关联**（存在文本字段里，不是 Notion 原生的关系字段）：

```text
00_X关注账号名单 ──(账号是信息来源，但帖子内容缺失=已知缺口)
01_原始素材 item_id ◄────「03_选题决策.关联素材」引用（一张卡通常关联 1 条素材）
03_选题决策 topic_id ◄────「04_文案与版本.topic_id」引用（一题×每平台一行，版本链）
02_用户需求 ──(本应支撑 03 的「需求证据」字段，当前为空 → 03 的需求证据全是「待验证」)
05_经验与复盘 ──(写作/选题规则，供人查阅，程序暂不自动调用)
08_维护与审计 ──(独立记录 S1/S3/S5/selfcheck 的每次操作)
```

也就是说：素材 → 选题 → 文案 三层由编号串成一条可追溯链；你随时可以从一张选题卡点回它的原始出处链接。

---

## 五、现在能干什么 / 还不能干什么

### ✅ 现在就能用的（已验证）

1. **离线全流程演练**：`bootstrap.sh` / `--dry-run`，用假数据从抓取跑到选题卡，不写任何外部系统（42 项测试已实际运行全部通过，CI 会在每次 push/PR 自动跑）。
2. **真实采集与选题（已跑过一轮）**：2026-10-29 的 P1 样例已真实完成——aihot 20 条素材 + X 135 账号 + 8 张选题卡全部入库 Notion；并验证过**幂等**（重跑 0 新建、全更新）和健康检查 6/6。
3. **人工选题（GATE-1）随时可做**：你现在就能打开 Notion「03_选题决策」勾选 1–3 张卡——这是当前流程**卡在等你**的一步。
4. **初稿生成（代码就绪、真实环境未跑过）**：一旦你勾选，`python3 -m src.workflows.run draft` 就会用离线模板引擎（免费、不联网）生成初稿写入 04 库。该路径有完整测试覆盖，但**尚未在真实 Notion 上运行**（因为关口一还没通过）。
5. **观测与自检**：gate1/gate2/status/selfcheck 随时可查；healthcheck 有密钥泄漏扫描；export_notion 可把 4 个库备份成 JSON/CSV。

### ⬛ 还不能干的（明确标注）

| 事项 | 状态 |
|---|---|
| **GATE-1 人工选题** | **进行中/等待你**：8 张卡全部「待用户选择」，一张都还没选定 |
| **真实环境生成初稿（S5）** | **未执行过**：04 库 0 行。代码与测试已就绪，等你勾选后即可 |
| **修订循环（V1→V2 按你的反馈改同一稿）** | **未完成**（规划 P4）：目前改稿只能你手动在 Notion 编辑 |
| **发布到抖音/小红书** | **永不做**：代码里不存在任何发布路径（有测试守卫）。发布永远由你人工完成 |
| **发布后复盘、数据回流、知识迭代（S7）** | **未完成**（规划 P5） |
| **接入真实 LLM** | **未完成**：接口已留好（openai-compat），等你提供自备 API（阶段报告待确认事项之一） |
| **X 帖子内容采集** | **不可得（已知缺口）**：X 只作账号名单，帖子内容拿不到；需求证据因此全部「待验证」 |
| **02_用户需求证据采集** | **未开始**：库是空的 |
| **定时自动运行** | **刻意不做**：无任何 cron/定时任务（红线） |
| **海外案例 / GitHub / 飞书等更多信息源** | **未接**：只留了配置开关和接口位 |
| **把代码推送到 GitHub（git push）** | **被 403 阻塞**：本地领先远端 4 个提交推不上去，根因见下节诊断 |

---

## 六、git push 403 诊断（只诊断，未修复，未真正 push）

### 现象

`git push origin main`（含 `--dry-run` 干跑）返回 403。本地 main 领先远端 **4 个提交**待推送。全程只执行了 dry-run 和只读检查，未真正推送、未修改任何 remote/凭据/配置。

### 根因（结论先说）

git 推送用的凭据是**环境变量 `GITHUB_TOKEN` 里的一枚细粒度 PAT（fine-grained personal access token，形如 `github_pat_***`）**。这枚 token 属于账号 **dehuamading**（与拥有该仓库 ADMIN 权限的账号是**同一个账号**，不存在用错账号的问题），但 **token 本身没有被授予对该仓库的「Contents: Read and write（内容写入）」权限**。细粒度 PAT 的权限永远是账号权限的子集——账号是 ADMIN 没用，token 没写权限，GitHub 就在接收推送时拒绝：403。之前阶段报告里 REST PUT 报的「Resource not accessible by personal access token」是同一种问题的典型报错。

### 证据清单（token 一律打码）

1. **remote**：`git remote -v` → `https://github.com/dehuamading/GrokBot-AI-Media-System-PRD.git`（fetch/push 同一 URL，URL 里**没有**内嵌 token）。
2. **凭据助手**：`git config --show-origin --get-all credential.helper` 输出为空（无全局助手）；但 `git config --list --show-origin` 显示 `~/.gitconfig` 中有针对 github.com 的按 URL 配置：`credential.https://github.com.helper = !/usr/local/bin/gh auth git-credential`——即 **gh 已经被配置为 github.com 的 git 凭据助手**（等效于 `gh auth setup-git` 的效果，已就位）。无 `~/.git-credentials` 文件。
3. **gh 状态**：`gh auth status` → 已登录 github.com 账号 **dehuamading**，token 来源标注为 **GITHUB_TOKEN**（环境变量），token 形如 `github_pat_***`（细粒度 PAT）；Git 协议 https；无 `~/.config/gh/hosts.yml` 存储的 token。
4. **环境变量**：`GH_TOKEN` **未设置**；`GITHUB_TOKEN` **已设置**；`GIT_ASKPASS` 未设置。
5. **git 实际用的凭据**：`git credential fill`（probe）返回 `username=x-access-token`、`password=github_pat_***`；与 `GITHUB_TOKEN` 环境变量做哈希比对**完全相同** → git push 用的就是这枚细粒度 PAT，不是另一个账号的另一枚 token。
6. **账号有权限、token 没有**：用同一 token 调 REST `GET /repos/dehuamading/GrokBot-AI-Media-System-PRD` 返回 200，permissions 为 `{admin: true, maintain: true, push: true, triage: true, pull: true}` → 账号 dehuamading 对仓库确有 ADMIN（与此前 gh 显示一致）。但只读 git 操作 `git ls-remote --heads origin` 正常（远端 main=b41f318，说明 token 有读权限），**写**（push）被拒。
7. **确切错误**（`GIT_TRACE=1 GIT_TERMINAL_PROMPT=0 git push --dry-run origin main`，已打码）：
   ```text
   trace: run_command: '/usr/local/bin/gh auth git-credential get'
   remote: Permission to dehuamading/GrokBot-AI-Media-System-PRD.git denied to dehuamading.
   fatal: unable to access 'https://github.com/dehuamading/GrokBot-AI-Media-System-PRD.git/':
          The requested URL returned error: 403
   ```
   trace 同时证实 git 是通过 `gh auth git-credential` 取得凭据后再连 GitHub 的。
8. **历史佐证**：阶段报告记载此前 REST PUT（写文件接口）同样 403「Resource not accessible by personal access token」——细粒度 PAT 缺所需权限的标志性报错，与 git push 403 同源。

### 判断：是不是「git 用了不同凭据」？

**不是**。git 用的凭据 = gh 用的凭据 = `GITHUB_TOKEN` 环境变量里的那枚细粒度 PAT（哈希比对一致），账号也是同一个 dehuamading。问题不在「谁在认证」，而在「这枚 token 被授权了什么」：它对仓库只有读权限（Contents 只读或仅 Metadata），没有 Contents 写权限，所以账号虽有 ADMIN、token 依然推不动。

### 最安全的修复选项（均未执行，需用户决定）

1. **首选：给现有 PAT 补权限**。GitHub 网页 → Settings → Developer settings → Personal access tokens → Fine-grained tokens → 编辑这枚 token → 确认 Repository access 包含 `GrokBot-AI-Media-System-PRD` → Permissions → Repository permissions → **Contents 改为 Read and write** → 保存。token 值不变的话，环境变量无需更换，直接重试 `git push origin main` 即可（建议先 `git push --dry-run origin main` 验证）。
2. **或：换一枚有 Contents:write 的新 PAT**，替换到该 token 的存放处并重新注入 `GITHUB_TOKEN` 环境变量。
3. **关于 `gh auth setup-git`**：本例**帮不上忙**——它只负责把 gh 配置成 git 的凭据助手，而这步已经配置好了（见证据 2）。问题出在 token 权限本身，不在 git↔gh 接线。
4. **备选：改用 SSH**（给账号配 SSH key、remote 改 SSH 地址）可绕开 PAT 权限问题，但会改动 remote 配置，属于变更，需另行批准；对单一仓库场景不如选项 1 简单。

---

## 附：本次任务执行说明

- 只读红线遵守情况：未修改仓库任何文件、未 git commit/push/fetch（诊断仅 `--dry-run`）、未访问 X 和 aihot.news、Notion 仅做 GET/数据库查询（未创建/更新/删除任何内容）、token 全程未打印（输出中出现的 token 一律为 `github_pat_***` 打码形式）。
- 运行过的写操作仅一项：单元测试 `python3 -m pytest tests/ -q`（42 passed，未产生仓库改动——`git status` 干净）。
- 本报告是本次任务唯一写入的文件。
