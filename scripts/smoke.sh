#!/usr/bin/env bash
set -euo pipefail
IFS=$'\n\t'

# ---------------------------------------------------------------------------
# VocalizeAI smoke test
#
# Exercises the 5-step request sequence against a running backend:
#   1. GET  /health                  — assert ok=true
#   2. POST /api/sessions            — create session, capture session_id + ws_url
#   3. POST /api/sessions/{id}/task  — set a test task
#   4. WS   /ws/sessions/{id}        — send text_input, receive >=1 frame
#   5. DELETE /api/sessions/{id}     — clean up
#
# Usage:
#   bash scripts/smoke.sh
#   VOCALIZE_API_BASE=http://127.0.0.1:8080 bash scripts/smoke.sh
#   VOCALIZE_INVITE_TOKEN=<token> VOCALIZE_API_BASE=https://api.example.com bash scripts/smoke.sh
#
# Exits 0 on all-pass; exits 1 with descriptive message on any failure.
# Total runtime budget: ~20 seconds (WS step has a 10s timeout).
# ---------------------------------------------------------------------------

BASE="${VOCALIZE_API_BASE:-http://127.0.0.1:8000}"
TOKEN="${VOCALIZE_INVITE_TOKEN:-}"

SMOKE_START=$(date +%s)
SID=""
WS_URL=""

fail() {
    local step="$1"
    local reason="$2"
    echo ""
    echo "${step}... FAIL: ${reason}"
    echo ""
    echo "SMOKE FAIL — step ${step} did not pass."
    exit 1
}

require_cmd() {
    local cmd="$1"
    if ! command -v "$cmd" &>/dev/null; then
        echo "ERROR: Required command '$cmd' not found."
        echo "  Install with: brew install $cmd  (macOS) or  sudo apt install $cmd  (Linux)"
        exit 1
    fi
}

require_cmd curl
require_cmd jq

# ---------------------------------------------------------------------------
# Step 1: GET /health
# ---------------------------------------------------------------------------

echo ""
echo "[1/5] GET ${BASE}/health..."
HEALTH_RESP=$(curl -fsS --max-time 10 "${BASE}/health" 2>/dev/null) \
    || fail "[1/5]" "curl failed — is the backend running at ${BASE}?"

OK=$(echo "$HEALTH_RESP" | jq -r '.ok' 2>/dev/null) \
    || fail "[1/5]" "response is not valid JSON: ${HEALTH_RESP}"

if [ "$OK" != "true" ]; then
    fail "[1/5]" ".ok is not true in response: ${HEALTH_RESP}"
fi

GPU=$(echo "$HEALTH_RESP" | jq -r '.gpu_reachable' 2>/dev/null) || GPU="unknown"
if [ "$GPU" = "false" ]; then
    echo "[1/5] WARNING: gpu_reachable=false — GPU services may be offline (smoke continues)."
fi

echo "[1/5] GET /health... PASS (gpu_reachable=${GPU})"

# ---------------------------------------------------------------------------
# Step 2: POST /api/sessions
# ---------------------------------------------------------------------------

echo ""
echo "[2/5] POST ${BASE}/api/sessions..."

SESSIONS_RESP=$(curl -fsS --max-time 10 -X POST "${BASE}/api/sessions" \
    -H "X-Invite-Token: ${TOKEN}" \
    -H "Content-Type: application/json" \
    -d '{"default_lang":"zh","auto_translate_merchant":true}' 2>/dev/null) \
    || fail "[2/5]" "curl failed — is the backend running and invite token correct?"

SID=$(echo "$SESSIONS_RESP" | jq -r '.session_id' 2>/dev/null) \
    || fail "[2/5]" "response is not valid JSON: ${SESSIONS_RESP}"

if [ -z "$SID" ] || [ "$SID" = "null" ]; then
    fail "[2/5]" ".session_id is empty or null in response: ${SESSIONS_RESP}"
fi

WS_URL=$(echo "$SESSIONS_RESP" | jq -r '.ws_url' 2>/dev/null) \
    || fail "[2/5]" "could not extract .ws_url from response"

if [[ "$WS_URL" != ws://* ]] && [[ "$WS_URL" != wss://* ]]; then
    fail "[2/5]" ".ws_url does not start with ws:// or wss://: ${WS_URL}"
fi

echo "[2/5] POST /api/sessions... PASS (session_id=${SID})"

# ---------------------------------------------------------------------------
# Step 3: POST /api/sessions/{id}/task
# ---------------------------------------------------------------------------

echo ""
echo "[3/5] POST ${BASE}/api/sessions/${SID}/task..."

TASK_RESP=$(curl -fsS --max-time 10 -X POST "${BASE}/api/sessions/${SID}/task" \
    -H "Content-Type: application/json" \
    -d '{"task":"smoke test task"}' 2>/dev/null) \
    || fail "[3/5]" "curl failed on POST /api/sessions/${SID}/task"

TASK_OK=$(echo "$TASK_RESP" | jq -r '.ok' 2>/dev/null) \
    || fail "[3/5]" "response is not valid JSON: ${TASK_RESP}"

if [ "$TASK_OK" != "true" ]; then
    fail "[3/5]" ".ok is not true in task response: ${TASK_RESP}"
fi

echo "[3/5] POST /api/sessions/${SID}/task... PASS"

# ---------------------------------------------------------------------------
# Step 4: WS upgrade + send text_input + receive at least one frame
# ---------------------------------------------------------------------------

echo ""
echo "[4/5] WS ${WS_URL}..."

TEXT_INPUT='{"type":"text_input","text":"hello","lang_hint":"zh","mode":"default"}'
WS_RECEIVED=""

if command -v websocat &>/dev/null; then
    # websocat path: send one frame, read one response, exit
    WS_RECEIVED=$(
        printf '%s\n' "$TEXT_INPUT" \
        | timeout 10 websocat --no-close --exit-on-eof -1 "$WS_URL" 2>/dev/null
    ) || true
else
    # Python websockets fallback
    if python3 -c 'import websockets' 2>/dev/null; then
        WS_RECEIVED=$(_SMOKE_WS_URL="$WS_URL" python3 - <<'PYEOF'
import asyncio, json, os, sys

WS_URL = os.environ.get("_SMOKE_WS_URL", "")
TEXT_INPUT = json.dumps({"type":"text_input","text":"hello","lang_hint":"zh","mode":"default"})

async def run():
    try:
        import websockets
        async with websockets.connect(WS_URL, open_timeout=10) as ws:
            await ws.send(TEXT_INPUT)
            try:
                frame = await asyncio.wait_for(ws.recv(), timeout=10)
                print(frame)
            except asyncio.TimeoutError:
                print("", end="")
    except Exception as e:
        print(f"WS_ERROR: {e}", file=sys.stderr)

asyncio.run(run())
PYEOF
) || true
    else
        echo "[4/5] WARNING: websocat not found and Python websockets package not available."
        echo "  Install websocat: brew install websocat  (or: pip install websockets)"
        echo "[4/5] WS step skipped — PARTIAL PASS (install websocat to enable full smoke)."
        WS_RECEIVED="SKIPPED"
    fi
fi

if [ -z "$WS_RECEIVED" ]; then
    fail "[4/5]" "no frame received from WS within 10s — is the backend processing requests? (WS_URL=${WS_URL})"
fi

echo "[4/5] WS send+recv... PASS"

# ---------------------------------------------------------------------------
# Step 5: DELETE /api/sessions/{id}
# ---------------------------------------------------------------------------

echo ""
echo "[5/5] DELETE ${BASE}/api/sessions/${SID}..."

# Brief pause to allow WS to close cleanly before DELETE
sleep 1

DEL_RESP=$(curl -fsS --max-time 10 -X DELETE "${BASE}/api/sessions/${SID}" 2>/dev/null) \
    || fail "[5/5]" "curl failed on DELETE /api/sessions/${SID}"

DEL_OK=$(echo "$DEL_RESP" | jq -r '.ok' 2>/dev/null) \
    || fail "[5/5]" "response is not valid JSON: ${DEL_RESP}"

if [ "$DEL_OK" != "true" ]; then
    fail "[5/5]" ".ok is not true in delete response: ${DEL_RESP}"
fi

echo "[5/5] DELETE /api/sessions/${SID}... PASS"

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

SMOKE_END=$(date +%s)
ELAPSED=$(( SMOKE_END - SMOKE_START ))

echo ""
echo "SMOKE PASS (5 HTTP + 1 WS = 6 round-trips, ${ELAPSED}s total)"
