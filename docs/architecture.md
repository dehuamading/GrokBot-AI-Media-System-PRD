# 架构说明（对应 PRD 第 2/9 节）

## 当前实现（任务03：完整工作流程序）

```text
[X 关注名单文件(GrokBot交付)] ──账号级──┐                 [S1 抓取 collect]
[aihot.news /api/v1 (≤20条/轮)] ─内容级─┤   （--live 才联网；默认离线回放假数据）
                                       ↓
                 [标准化层 src/normalize.py]  URL规范化/item_id幂等键/去重
                                       ↓
                 [分析层 src/analysis/]  分类(6类) → 七维可解释评分(v1-heuristic)
                                       → 选题卡 5–10 张(PRD 5.4 全字段)   [S2 分析]
                                       ↓
                 [Notion 适配层 src/notion_store/]  限流+429退避 / 幂等upsert
                   01_原始素材 更新分数；03_选题决策 写卡(待用户选择)      [S3 写库]
                                       ↓
       ═══════════ ⏸ GATE-1 第一人工关口：用户在 Notion 03 勾选「已选定」═══════════
                                       ↓
                 [人工关口 src/notion_store/gates.py]  实时读 Notion（唯一事实源）
                                       ↓ [S4 gate1]                 [S5 draft]
                 [LLM 层 src/llm/]  默认=离线模板引擎(零成本)；可选=openai-compat
                   每题×每平台仅 1 份初稿 → 04_文案与版本(状态V1)+正文块
                                       ↓
       ═══════════ ⏸ GATE-2 第二人工关口：用户在 Notion 04 审阅（已确认/修订中）══════
       ❌ 无发布代码路径；修订循环(P4)与复盘(P5)未完成
```

- **02_用户需求 / 05_经验与复盘**：05 已写入种子规则（任务02）；02 仍空（需求证据采集未开始，
  选题卡需求证据一律「待验证」）。
- **08_维护与审计**：S1/S3/S5/selfcheck 各写一条事件，Notion 内可审计。

## 关键设计决定

1. **幂等**：写 Notion 先按 title 唯一键查询再 upsert；`item_id = source:original_id`；
   重跑不重复入库；状态机允许任意阶段重开 collect 新一轮（PRD 9.3）。
2. **状态外置（本地）**：`data/state/workflow_state.json` 记录阶段
   （init→collected→waiting_gate1→drafted→waiting_gate2）、db_ids、关口快照。
   该文件含真实 item_id 游标 → gitignore，不入公开库；丢失可由 collect 幂等重建。
3. **关口以 Notion 为唯一事实源**：gate1/draft 每次实时查询 03_选题决策，
   本地状态不能替代人工勾选；卡片也可被用户改为「未选用」否决。
4. **只增列升级**：任务03 对既有 7 库仅新增 03.用户指定平台、04.生成引擎，
   绝不删除/修改既有列与数据（红线：不删除既有资料）。
5. **评分透明**：七维高/中/低 + 理由，版本 v1-heuristic，标注 HYP-002（未验证假设），
   禁止用于宣称预测传播；「传播潜力」维度显式标注假设。
6. **X 降级模式**：X 仅作账号名单（00 库，文件读入），帖子内容为已知缺口，
   代码无任何访问 x.com/twitter.com 的路径（红线测试断言）。
7. **LLM 可替换**：DraftClient 协议；默认离线模板引擎（未购买任何服务）；
   openai-compat 需用户自备 LLM_API_BASE/KEY/MODEL 才启用。
8. **连接器独立开关**：sources.example.yaml enabled；aihot 失败不影响 X 名单入库。

## 技术栈

Python 3.13 + requests（唯一运行时依赖）；Notion REST API（Notion-Version: 2022-06-28）；
GitHub Actions CI 仅 push/PR 触发（无 schedule，红线）。测试 42 项全部离线（假数据/mock）。
