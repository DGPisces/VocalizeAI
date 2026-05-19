#!/usr/bin/env bash
# 远程探测 VocalizeAI GPU 服务的可用性
#
# 用途：从 Mac / Pi 上跑这脚本探测 Windows GPU 主机（或云 GPU 实例）的两个服务是否
# 健康。在 CI / 排障时也可用。
#
# 用法：
#   bash healthcheck.sh                       # 用 env $GPU_HOST 或 localhost
#   bash healthcheck.sh --gpu-host 100.x.x.x  # 显式指定
#   GPU_HOST=100.x.x.x bash healthcheck.sh    # 通过 env
#
# 退出码：
#   0 = 全绿（两个服务的 /health 都返 status=ok）
#   1 = 任一探测失败（连接失败 / 状态非 ok / JSON 解析失败）
#
# 依赖：bash, curl；jq 可选（没装会回退到 grep）

set -uo pipefail

GPU_HOST_DEFAULT="${GPU_HOST:-localhost}"
SENSEVOICE_PORT_HTTP="${SENSEVOICE_PORT_HTTP:-8080}"
COSYVOICE_PORT_HTTP="${COSYVOICE_PORT_HTTP:-8081}"
TIMEOUT="${HEALTHCHECK_TIMEOUT:-5}"

GPU_HOST="$GPU_HOST_DEFAULT"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --gpu-host)
            GPU_HOST="$2"; shift 2 ;;
        --gpu-host=*)
            GPU_HOST="${1#--gpu-host=}"; shift ;;
        -h|--help)
            grep '^#' "$0" | sed 's/^# \?//'
            exit 0 ;;
        *)
            echo "Unknown arg: $1" >&2; exit 2 ;;
    esac
done

# 颜色（仅 stderr 是 tty 时用）
if [[ -t 1 ]]; then
    GREEN=$'\033[32m'; RED=$'\033[31m'; YELLOW=$'\033[33m'; BOLD=$'\033[1m'; RESET=$'\033[0m'
else
    GREEN=""; RED=""; YELLOW=""; BOLD=""; RESET=""
fi

have_jq=0
command -v jq >/dev/null 2>&1 && have_jq=1

failures=0

probe_health() {
    local name="$1" url="$2"
    local body status_code body_status body_model_loaded
    # 同时拿 body 与 status code；错误重定向防 stderr 污染
    local resp
    resp=$(curl -sS -m "$TIMEOUT" -w "\n%{http_code}" "$url" 2>&1) || {
        echo "${RED}FAIL${RESET} $name health: connect error → $url"
        echo "    $resp"
        failures=$((failures + 1))
        return 1
    }
    status_code=$(echo "$resp" | tail -n1)
    body=$(echo "$resp" | sed '$d')

    if [[ "$status_code" != "200" ]]; then
        echo "${RED}FAIL${RESET} $name health: HTTP $status_code → $url"
        echo "    $body"
        failures=$((failures + 1))
        return 1
    fi

    if (( have_jq )); then
        body_status=$(echo "$body" | jq -r '.status // "?"')
        body_model_loaded=$(echo "$body" | jq -r '.model_loaded // false')
    else
        body_status=$(echo "$body" | grep -oE '"status"[ :]*"[^"]+"' | sed 's/.*"\([^"]*\)"$/\1/')
        body_model_loaded=$(echo "$body" | grep -oE '"model_loaded"[ :]*[a-z]+' | awk -F: '{print $2}' | tr -d ' ')
    fi

    if [[ "$body_status" == "ok" && "$body_model_loaded" == "true" ]]; then
        echo "${GREEN}OK${RESET}   $name health: status=ok model_loaded=true"
        return 0
    else
        echo "${YELLOW}WARN${RESET} $name health: status=$body_status model_loaded=$body_model_loaded"
        echo "    $body"
        failures=$((failures + 1))
        return 1
    fi
}

probe_metrics() {
    local name="$1" url="$2"
    local resp
    resp=$(curl -sS -m "$TIMEOUT" "$url" 2>&1) || {
        echo "${RED}FAIL${RESET} $name metrics: connect error → $url"
        failures=$((failures + 1))
        return 1
    }
    if echo "$resp" | head -1 | grep -q "^# HELP"; then
        local lines
        lines=$(echo "$resp" | wc -l | tr -d ' ')
        echo "${GREEN}OK${RESET}   $name metrics: $lines lines (Prometheus format)"
    else
        echo "${RED}FAIL${RESET} $name metrics: not Prometheus format"
        echo "$resp" | head -5
        failures=$((failures + 1))
        return 1
    fi
}

echo "${BOLD}probing GPU host: ${GPU_HOST}${RESET}"
echo

probe_health "sensevoice" "http://${GPU_HOST}:${SENSEVOICE_PORT_HTTP}/health"
probe_metrics "sensevoice" "http://${GPU_HOST}:${SENSEVOICE_PORT_HTTP}/metrics"
echo
probe_health "cosyvoice " "http://${GPU_HOST}:${COSYVOICE_PORT_HTTP}/health"
probe_metrics "cosyvoice " "http://${GPU_HOST}:${COSYVOICE_PORT_HTTP}/metrics"

echo
if (( failures == 0 )); then
    echo "${GREEN}${BOLD}all probes green${RESET}"
    exit 0
else
    echo "${RED}${BOLD}${failures} probe(s) failed${RESET}"
    exit 1
fi
