# 架构 v2（增量升级设计，2026-11-02）
原则：复用现有模块，只加适配层与字段；全部手动触发；Notion 是长期记忆。

## 1. 总体链路（括号内为复用的现有文件）
```
[采集] Grok Bot: X 帖子+公开回复 → inputs/x_*.json（扩展现有格式）
       Pi:  AIHot(aihot_source.py 原样) + GitHub(新 github_source.py) + 官方源(新 official_source.py，≤5 个)
       国内(未来): 飞书/Notion/文件 → 新 import_source.py（只做校验+映射）
          ↓ 全部转成「统一素材」(normalize.py，现有格式基础上加字段)
[去重/来源校验] normalize.make_item_id + 新 validate.py
[分析] 需求引擎(新 analysis/demand.py：评论→10类需求，保留原文证据)
       机会引擎(复用 classify.py + 新判断项)
       评分(scoring.py 七维 + 扩展项，见 §4) + 可选 GLM(llm_topics.py)
       偏好参考(新 analysis/preference.py：读 05 最近认可/否决)
[选题卡] topics.py → 03_选题决策（只增列）
[GATE-1 人工选] gates.py（不变）→ 反馈写 05 + 认可素材
[文案] 仅对选中卡，llm/ 下新增 GLM 写稿客户端 → 04（不变）
[GATE-2 人工确认] 不变；永不自动发布
```

## 2. 采集分工
| 来源 | 谁 | 方式 | 产出 |
|---|---|---|---|
| X 帖子/回复/互动 | Grok Bot | 内置 X 只读工具（get_posts_by_id、search_posts_all conversation_id:…；30次/分、1000次/天） | inputs/x_posts_*.json（含 replies 数组） |
| AIHot | Pi（代码） | 现有连接器，不重写 | 统一素材 |
| GitHub | Pi（代码） | 官方 REST：search/repositories + releases/latest + 仓库元数据；默认 5 个可配置 | 统一素材 + 项目字段 |
| 官方源 | Pi（代码） | 首批 ≤5：OpenAI/Anthropic/Google 官方博客或 changelog RSS、GitHub Releases | 统一素材 |
| 国内平台 | 用户本地电脑（未来） | 飞书/Notion/文件导入；本阶段只做接口+脱敏样例 | 统一素材 |
Stars 增长只在 data/state 里有前一次快照时才计算，否则标“无历史，不计增长”；不把 Trending 当增长。

## 3. 统一输入格式（在现有素材字段上只增不减）
现有字段保留：item_id, source_type, source_url, author_or_publisher, published_at, collected_at, title_or_summary, claims, evidence_status, user_problem_evidence, extra …
新增字段：platform, origin_agent(grokbot/pi/import), keywords[], comments[]{text,url,created_at,metrics}, metrics{likes,reposts,replies,quotes,views,stars}, license_status(公开/授权/待确认), import_batch, errors[]

| 统一字段 | X 扁平文件 | X API 原样文件 | AIHot | GitHub | 国内导入 |
|---|---|---|---|---|---|
| source_url | url | url | url/源链接 | html_url | 原始链接 |
| title_or_summary | text | text | 标题+摘要 | full_name+description | 标题 |
| author_or_publisher | author_name(@author_username) | username | 来源 | owner.login | 作者 |
| published_at | created_at | created_at | 发布时间 | pushed_at | 发布时间 |
| metrics | likes/reposts/replies/quotes/impressions | public_metrics.* | aihot_score→extra | stargazers_count | 互动指标(可空) |
| comments | 新增 replies[] | 同左 | — | — | 公开评论文本 |
| platform | x | x | aihot | github | douyin/bilibili/xhs… |
| license_status | 公开 | 公开 | 公开 | 公开+许可证 | 必填 |
X 文件同时兼容两种格式（修复 §审计 2 的缺陷）；不另建 X 数据库。

## 4. 评分扩展（避免重复计算）
| 新判断 | 处理 | 理由 |
|---|---|---|
| 用户需求真实性 | 并入现有「真实用户价值」：有回复证据（≥N 条同类原文）可升档 | 同一因素，不新增维 |
| 事实可信度 | = 现有「信息可信度」 | 已有 |
| 内容时效性 | = 现有「新颖与时效」 | 已有 |
| 实际可操作性 | = 现有「可讲清与可验证」+「制作成本」 | 已有，不加 |
| 国内适用性 | **新增维**（可用/需梯子/需外币卡/不可用/待验证） | 现有没有 |
| 新技术机会 | **新增维**（新能力/降门槛/降成本） | 现有没有 |
| AI 副业潜力 | **新增维**，证据分级：真实案例/潜在机会/个人经验/未经验证宣传；仅后两类时最高 1 档 | 防夸大 |
| 内容差异化 | 现有「传播潜力假设」保留但降为不计分参考，用差异化替代计分 | 张力词与差异化重叠 |
满分 0–18（9 维计分）。分轨：需求型看 需求真实性+可操作+国内；机会型看 新机会+时效+国内；各自排序后合并 Top N，标注轨道。版本号 v3-dual，旧 v2 结果保留可对比。不声称预测爆款。

## 5. 选题卡新增列（03，只增）
选题类型(需求型/机会型)、面向用户(5 类多选)、是否需付费、能否实际验证、主要风险、原始来源链接(必填，缺失则不入库)、副业证据等级、轨道得分。复用现有：面向谁→保留、为什么现在、国内可用性、切入角度与钩子、推荐理由、可信度与风险。未验证内容一律写“待验证”。

## 6. 偏好学习闭环（复用 05，不新建库）
- 每轮 GATE-1 后：对每张被选/被否卡写 05 一条：类型=复盘，性质=用户偏好，内容=原始选题+推荐理由+选择+原话，依据=topic_id 与链接，状态=待验证。05 新增列：topic_id、用户选择(认可/否决)、原话反馈、原因标签、轮次。
- 认可素材记录：复用 05（类型新增选项“认可素材”）而非新建库——字段完全重合，避免重复库。
- 下一轮：preference.py 读最近 5 认可 + 5 否决，作为 GLM 提示示例与规则加减分参考（加减分仅限已确认规则）。
- 每 3 轮：生成 ≤10 条候选规则写 05（状态=待验证），用户确认后改为“正在使用”才生效。不自动改代码。

## 7. 文案输出（仅选中卡）
3 个标题、2 个开头、1 份口播全文、操作演示建议、事实核验清单、原始来源；写入 04 一条版本记录；默认 GLM，失败退回模板并在“生成引擎”列如实标注。

## 8. 手动命令（无任何定时任务）
```
run collect --sources aihot,github,x,import   # 选题全流程到 GATE-1
run github --top 5                            # 只做 GitHub 研究
run x-research --file inputs/x_posts_*.json   # X 教程/需求研究（Grok Bot 先交文件）
run demand --category 副业                     # 用户需求研究
run feedback --topic TX-…-03 --pick 认可 --say "原话"  # 记录反馈到 05
run draft --topic TX-…-03                      # 只写这一张
run rules --propose / --confirm R-xxx          # 候选规则
```

## 9. 成本记录
每次运行在 data/state/cost_log.jsonl 追加：时间、命令、GLM/DeepSeek 调用次数与 token（接口返回的才记）、GitHub/AIHot/X 调用次数、金额（未知写“未知”，不写 0）；并在 08 审计写一条摘要。
