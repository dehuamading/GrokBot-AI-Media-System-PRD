#!/usr/bin/env bash
# 环境引导：安装依赖并做本地自检（不写任何外部系统）
set -euo pipefail
cd "$(dirname "$0")/.."

if ! python3 -c "import requests" 2>/dev/null; then
  echo "[bootstrap] 安装 Python 依赖（requests/pytest）..."
  pip3 install --break-system-packages -r requirements.txt \
    || pip3 install --user -r requirements.txt \
    || pip3 install -r requirements.txt
fi

echo "[bootstrap] 运行单元测试（无网络，假数据/mock）..."
python3 -m pytest tests/ -q

echo "[bootstrap] 工作流 dry-run（假数据，不写 Notion、不真实抓取）..."
python3 -m src.workflows.run collect \
  --aihot-file data/examples/aihot_fake_sample.json \
  --x-following data/examples/x_following.example.json \
  --dry-run

echo "[bootstrap] 完成。写 Notion 前：export NOTION_TOKEN=\$(cat /workspace/.secrets/notion_token)"
