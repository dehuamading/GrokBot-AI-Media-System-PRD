# 信息源权限与边界（P0 审计结论 + 本轮实测）

更新：2026-10-30。原则：不得把「已连接账号」当作「已具备可编程读写权限」（PRD 0.1）。

| 来源 | 级别 | 状态 | 依据/边界 |
|---|---|---|---|
| aihot.news `/api/v1` | B级(核心补充) | ✅ 已验证可用 | robots.txt 放行 `/api/v1`；llms.txt 声明匿名只读无需 Key。本轮实测 20 条成功。旧 `/api/public/*` 已于 2026-10-31 停服，禁用。单轮 ≤20 条（任务02 约束）。 |
| X 关注列表 | A级(最优先) | ⚠️ 降级模式 | Pi Agent 不访问 X（任务02 决定）。名单由 Grok Bot 读取后以文件交付（`/workspace/ai-zimeiti/inputs/x_following.json`，135 账号）。**帖子内容不可得 → 已知缺口**。 |
| X 话题/发现 | B级 | ❌ 未启用 | 无授权。 |
| 海外案例 | C级 | ❌ 未启用 | P1 未完成项。 |
| GitHub 国内入口 | D级 | ❌ 未配置 | 只读桩（sources.example.yaml 预留）。 |
| 飞书国内入口 | D级 | ❌ 未配置 | 只读桩（sources.example.yaml 预留）。 |
| Notion API | 唯一知识库 | ✅ 已验证 | 内部集成 token（环境变量 NOTION_TOKEN 注入，不打印不提交），仅授权页面「AI 自媒体系统」(408d7d3b-2923-8106-8024-e0548308c638)，Notion-Version 2022-06-28。本轮实测：读页面 ✅ / 建库 ✅ / 建行 ✅ / 更新 ✅ / 按键查询 ✅。 |
| GitHub (dehuamading/GrokBot-AI-Media-System-PRD) | 版本库 | ✅ 已验证 | 公开仓库（用户决定）；gh 已登录；推送/CI 见 docs/phase-reports。 |
| 定时任务 | — | ❌ 本机无 cron；本阶段不设置任何调度（任务02 决定） | 后续如需，评估 GitHub Actions cron（公开仓库免费），需另行批准。 |

## 隐私与安全

- X 关注名单含个人信息 → **不入公开库**（仅本地 inputs/，示例为脱敏假数据）；
- Notion 备份导出仅含业务字段；`data/raw/`、`data/exports/` 已在 .gitignore 屏蔽（data/examples/ 为脱敏样本）；
- 密钥扫描：`scripts/healthcheck.sh` 步骤 6。
