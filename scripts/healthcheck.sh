#!/usr/bin/env bash
# 健康检查（PRD 10.2 步骤4）：网络 / aihot / Notion 权限 / 状态一致性。
# 绝不打印任何密钥值。
set -euo pipefail
cd "$(dirname "$0")/.."

pass=0; fail=0
ok()   { echo "✅ $1"; pass=$((pass+1)); }
bad()  { echo "❌ $1"; fail=$((fail+1)); }

# 1) 网络
if curl -s -m 10 -o /dev/null -w "" https://aihot.news/; then ok "网络可达"; else bad "网络不可达"; fi

# 2) aihot API v1（注意旧 /api/public 已停服）
code=$(curl -s -m 10 -o /dev/null -w "%{http_code}" "https://aihot.news/api/v1/items?mode=selected&limit=1")
[ "$code" = "200" ] && ok "aihot /api/v1 可用 (HTTP $code)" || bad "aihot /api/v1 异常 (HTTP $code)"

# 3) GitHub 远端
if git ls-remote --heads origin >/dev/null 2>&1; then ok "GitHub 远端可达"; else bad "GitHub 远端不可达"; fi

# 4) Notion（需要 NOTION_TOKEN 环境变量）
PAGE="${NOTION_ROOT_PAGE_ID:-408d7d3b292381068024e0548308c638}"
if [ -n "${NOTION_TOKEN:-}" ]; then
  code=$(curl -s -o /tmp/notion_hc.json -w "%{http_code}" \
    -H "Authorization: Bearer ${NOTION_TOKEN}" \
    -H "Notion-Version: 2022-06-28" \
    "https://api.notion.com/v1/pages/${PAGE}")
  [ "$code" = "200" ] && ok "Notion 根页面可读 (HTTP $code)" || bad "Notion 读取失败 (HTTP $code)"
  rm -f /tmp/notion_hc.json
else
  bad "未设置 NOTION_TOKEN（export NOTION_TOKEN=\$(cat /workspace/.secrets/notion_token)）"
fi

# 5) 本地状态文件
[ -f data/state/workflow_state.json ] && ok "工作流状态文件存在（断点可续）" || echo "ℹ️  状态文件不存在（首轮运行属正常）"

# 6) 密钥泄漏粗查（已跟踪文件中不得出现密钥样式串；模式拼接避免脚本自匹配）
PAT1='ntn''_'; PAT2='secret''_[a-z0-9]'; PAT3='sk-''[a-zA-Z0-9]{20}'
LEAK=$(git ls-files -z | xargs -0 grep -lE "(${PAT1}|${PAT2}|${PAT3})" 2>/dev/null || true)
if [ -n "$LEAK" ]; then
  bad "疑似密钥字符串出现在: $LEAK"
else
  ok "未发现疑似密钥入库"
fi

echo "---- 健康检查: ${pass} 通过 / ${fail} 失败 ----"
[ "$fail" -eq 0 ]
