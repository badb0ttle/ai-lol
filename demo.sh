#!/usr/bin/env bash
set -euo pipefail

# ═══════════════════════════════════════════
# AI 任务后端 — 端到端演示脚本
# ═══════════════════════════════════════════

API_BASE="http://localhost:8000"
MAX_WAIT=120  # 最多等 120 秒
POLL_INTERVAL=2

# ── 颜色 ──
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
CYAN='\033[0;36m'
NC='\033[0m'

section()  { echo -e "\n${CYAN}═════ $1 ═════${NC}"; }
ok()       { echo -e "  ${GREEN}✓${NC} $1"; }
waiting()  { echo -e "  ${YELLOW}⏳${NC} $1"; }
fail()     { echo -e "  ${RED}✗${NC} $1"; }

cleanup() {
    echo -e "\n${YELLOW}⏹  停止服务...${NC}"
    docker compose down 2>/dev/null || true
}

trap cleanup EXIT

# ──────────────────────────────────────────
# 1. 启动服务
# ──────────────────────────────────────────
section "启动服务"
docker compose down -v 2>/dev/null || true
docker compose up -d

# ──────────────────────────────────────────
# 2. 健康检查
# ──────────────────────────────────────────
section "等待服务就绪"
elapsed=0
while [ $elapsed -lt $MAX_WAIT ]; do
    if curl -sf "${API_BASE}/health" > /dev/null 2>&1; then
        ok "API 就绪 (${elapsed}s)"
        break
    fi
    waiting "等待 API... (${elapsed}s)"
    sleep $POLL_INTERVAL
    elapsed=$((elapsed + POLL_INTERVAL))
done

if [ $elapsed -ge $MAX_WAIT ]; then
    fail "API 启动超时"
    docker compose logs api
    exit 1
fi

# ──────────────────────────────────────────
# 3. 提交任务
# ──────────────────────────────────────────
section "提交 AI 任务"
echo "  任务: 读取 README 并总结项目亮点"

RESPONSE=$(curl -sf -X POST "${API_BASE}/tasks" \
    -H "Content-Type: application/json" \
    -d '{
        "instruction": "读取 README.md 文件，理解这个项目的架构和核心功能，然后用中文写一段 200 字以内的总结，重点说明这个项目的技术栈和设计亮点。",
        "context": "这是一个演示任务，验证 agent 的 file_reader 和 chat_ai 工具。"
    }')

TASK_ID=$(echo "$RESPONSE" | python3 -c "import sys,json; print(json.load(sys.stdin)['task_id'])")
STATUS=$(echo "$RESPONSE" | python3 -c "import sys,json; print(json.load(sys.stdin)['status'])")
ok "任务已提交  task_id=${TASK_ID:0:8}...  status=${STATUS}"

# ──────────────────────────────────────────
# 4. 轮询状态
# ──────────────────────────────────────────
section "轮询任务状态"
elapsed=0
last_status=""
while [ $elapsed -lt $MAX_WAIT ]; do
    TASK_JSON=$(curl -sf "${API_BASE}/tasks/${TASK_ID}")
    STATUS=$(echo "$TASK_JSON" | python3 -c "import sys,json; print(json.load(sys.stdin)['status'])")

    if [ "$STATUS" != "$last_status" ]; then
        case "$STATUS" in
            queued)  waiting "状态: queued" ;;
            running) waiting "状态: running (agent 执行中...)" ;;
            success) ok "状态: success"; break ;;
            failed)  fail "状态: failed"
                     echo "$TASK_JSON" | python3 -m json.tool
                     exit 1 ;;
        esac
        last_status="$STATUS"
    fi

    sleep $POLL_INTERVAL
    elapsed=$((elapsed + POLL_INTERVAL))
done

if [ $elapsed -ge $MAX_WAIT ]; then
    fail "任务执行超时 (${MAX_WAIT}s)"
    echo "$TASK_JSON" | python3 -m json.tool
    exit 1
fi

# ──────────────────────────────────────────
# 5. 展示结果
# ──────────────────────────────────────────
section "任务结果"
echo "$TASK_JSON" | python3 -c "
import sys, json
data = json.load(sys.stdin)
print(f\"  task_id:   {data['task_id']}\")
print(f\"  status:    {data['status']}\")
print(f\"  steps:     {len(data['steps'])} 个步骤\")
for s in data['steps']:
    print(f\"    [{s['seq']}] {s['type']:10s} {s.get('tool_name','')}\")
print()
print(f\"  输出:\")
print(data.get('result', '(无)')[:800])
"

# ──────────────────────────────────────────
# 6. 幂等去重验证 (可选)
# ──────────────────────────────────────────
section "幂等去重验证"
IDEM_KEY="demo-$(date +%s)"
DUP=$(curl -sf -X POST "${API_BASE}/tasks" \
    -H "Content-Type: application/json" \
    -d "{
        \"instruction\": \"test\",
        \"idempotent_key\": \"${IDEM_KEY}\"
    }")
DUP_ID=$(echo "$DUP" | python3 -c "import sys,json; print(json.load(sys.stdin)['task_id'])")
ok "第 1 次提交  task_id=${DUP_ID:0:8}..."

DUP2=$(curl -sf -X POST "${API_BASE}/tasks" \
    -H "Content-Type: application/json" \
    -d "{
        \"instruction\": \"test\",
        \"idempotent_key\": \"${IDEM_KEY}\"
    }")
DUP_ID2=$(echo "$DUP2" | python3 -c "import sys,json; print(json.load(sys.stdin)['task_id'])")
if [ "$DUP_ID" = "$DUP_ID2" ]; then
    ok "第 2 次提交 → 返回相同 task_id (幂等确认)"
else
    fail "幂等失败: ${DUP_ID} ≠ ${DUP_ID2}"
fi

echo ""
echo -e "${GREEN}═════ 演示完成 ═════${NC}"
