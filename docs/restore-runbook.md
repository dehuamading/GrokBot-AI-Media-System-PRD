# 恢复手册（PRD 第 10 节）

前提：本机持久（非会话级），但仍按「云端可能被清空」设计。三类持久资产：
① 本 GitHub 仓库（代码+配置模板+状态文件+脱敏数据）② Notion（正式业务知识库）③ 密钥（用户控制，绝不在仓库）。

## 恢复步骤

```bash
# 1) 重建执行环境后拉代码
git clone https://github.com/dehuamading/GrokBot-AI-Media-System-PRD.git
cd GrokBot-AI-Media-System-PRD

# 2) 依赖 + 本地测试 + dry-run（无网络写入）
./scripts/bootstrap.sh

# 3) 密钥重注入（用户提供 /workspace/.secrets/notion_token 后）
export NOTION_TOKEN=$(cat /workspace/.secrets/notion_token)

# 4) 健康检查：网络/aihot/GitHub/Notion 权限/状态文件/密钥泄漏
./scripts/healthcheck.sh

# 5) 恢复状态：data/state/workflow_state.json（本地，不入库）记录阶段、Notion 各库 id、
#    关口快照；丢失也不影响恢复——collect 会重建结构并幂等 upsert

# 6) 幂等重跑验证（不得重复入库、不得越过人工关口）
python3 -m src.workflows.run collect \
  --aihot-file data/examples/aihot_fake_sample.json \
  --x-following /workspace/ai-zimeiti/inputs/x_following.json
# 预期：素材/选题 全部为「更新」而非「新建」；08_维护与审计 新增 1 条事件；
#       工作流停在 GATE-1（waiting_gate1，未被越过）。
python3 -m src.workflows.run gate1   # 未在 Notion 勾选时应显示等待用户选择

./scripts/restore_check.sh   # 一键演练上述流程
```

## 边界（必须写进验收报告的实话）

- 恢复后**不会自动运行**：需要人工触发（无 cron、无外部调度器，任务02 明确本阶段不设）。
- X 关注名单文件若随本机丢失：需 Grok Bot 重新交付（不在 GitHub 内，含个人信息）。
- Notion 是业务数据的唯一完整副本；`data/exports/` 本地导出是辅助备份，GitHub 不含其完整版。
- 幂等保证范围：item_id/username/topic_id/record_id 唯一键；审计日志每次运行追加（属预期行为）。
