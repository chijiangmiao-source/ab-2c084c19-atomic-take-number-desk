#!/usr/bin/env bash
# 一次性验收：依次运行 Vitest（前端单元）、pytest（并发/重启）、Playwright（浏览器）。
set -euo pipefail

API_BASE="${API_BASE:-http://api:8000}"
WEB_BASE="${WEB_BASE:-http://web}"

echo "== 等待 API 就绪: ${API_BASE} =="
for _ in $(seq 1 90); do
  if curl -fsS "${API_BASE}/api/health" >/dev/null 2>&1; then
    break
  fi
  sleep 1
done
curl -fsS "${API_BASE}/api/health"

echo
echo "== [1/3] Vitest 前端单元测试 =="
(cd /app/web && npx vitest run)

echo
echo "== [2/3] pytest API 并发 / 幂等 / 重启测试 =="
(cd /app/verify && python3 -m pytest tests -v)

echo
echo "== [3/3] Playwright 浏览器端到端测试 =="
(cd /app/verify && python3 -m pytest e2e -v)

echo
echo "======================================"
echo "  ALL CHECKS PASSED ✅"
echo "======================================"
