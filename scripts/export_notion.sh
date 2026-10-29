#!/usr/bin/env bash
# 导出 Notion 备份（需要 NOTION_TOKEN）
set -euo pipefail
cd "$(dirname "$0")/.."
: "${NOTION_TOKEN:?需要先 export NOTION_TOKEN（绝不打印、绝不提交）}"
python3 -m src.backup.export_notion "$@"
