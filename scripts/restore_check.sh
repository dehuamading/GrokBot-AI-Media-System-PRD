#!/usr/bin/env bash
# 恢复演练检查（PRD 10.2/10.3）：验证从 GitHub + Notion 可恢复。
# 在干净环境执行：git clone → bootstrap → healthcheck → 用状态文件验证幂等。
set -euo pipefail
cd "$(dirname "$0")/.."

echo "[restore] 1/5 校验代码来源..."
git remote get-url origin
git log --oneline -1

echo "[restore] 2/5 依赖与测试..."
python3 -c "import requests" || pip3 install --break-system-packages -r requirements.txt
python3 -m pytest tests/ -q

echo "[restore] 3/5 密钥重注入（本地文件→环境变量，不入库）..."
[ -n "${NOTION_TOKEN:-}" ] && echo "NOTION_TOKEN 已就绪" \
  || { [ -f /workspace/.secrets/notion_token ] && echo "提示: export NOTION_TOKEN=\$(cat /workspace/.secrets/notion_token)"; }

echo "[restore] 4/5 健康检查..."
./scripts/healthcheck.sh

echo "[restore] 5/5 幂等验证：重跑工作流应全部命中更新而非重复新建。"
echo "（需 NOTION_TOKEN；执行 python3 -m src.workflows.run collect ... 后，"
echo "  观察 Notion 侧各库新建数为 0、更新数=已有行数，且 08_维护与审计 新增一条事件；"
echo "  gate1 子命令应显示等待用户选择 —— 关口未被越过）"
