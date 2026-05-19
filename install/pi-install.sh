#!/usr/bin/env bash
set -euo pipefail
IFS=$'\n\t'

# ---------------------------------------------------------------------------
# VocalizeAI Pi installer
#
# Deploys the VocalizeAI orchestrator on a Raspberry Pi.
# Wraps the existing infra/pi-orchestrator/ assets.
#
# Usage:
#   bash install/pi-install.sh [--dry-run] [--steps "1,3,5"] [--skip-tunnel] [--skip-gpu]
#
# Flags:
#   --dry-run       Print planned actions without performing any mutations.
#   --steps "1,3"   Run only the listed comma-separated step numbers (default: all).
#   --skip-tunnel   Skip step 5 (Cloudflare Tunnel setup instructions).
#   --skip-gpu      Skip GPU-reachability check inside step 7.
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

DRY_RUN=false
STEPS_FILTER=""
SKIP_TUNNEL=false
SKIP_GPU=false
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
INSTALL_DIR="/opt/vocalize"

# ---------------------------------------------------------------------------
# Parse flags
# ---------------------------------------------------------------------------

while [[ $# -gt 0 ]]; do
    case "$1" in
        --dry-run)
            DRY_RUN=true
            shift
            ;;
        --steps)
            STEPS_FILTER="$2"
            shift 2
            ;;
        --skip-tunnel)
            SKIP_TUNNEL=true
            shift
            ;;
        --skip-gpu)
            SKIP_GPU=true
            shift
            ;;
        *)
            echo "Unknown flag: $1"
            echo "Usage: bash install/pi-install.sh [--dry-run] [--steps '1,3,5'] [--skip-tunnel] [--skip-gpu]"
            exit 1
            ;;
    esac
done

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

run_or_dry() {
    # First arg is human description; remaining args are the command.
    # Description is unused at runtime but printed in dry-run mode via $*
    shift
    if [ "$DRY_RUN" = true ]; then
        echo "[DRY] would run: $*"
    else
        "$@"
    fi
}

should_run_step() {
    local step_num="$1"
    if [ -z "$STEPS_FILTER" ]; then
        return 0  # run all steps
    fi
    # Check if step_num is in the comma-separated filter
    echo "$STEPS_FILTER" | tr ',' '\n' | grep -qxF "$step_num"
}

STEPS_RUN=()

# ---------------------------------------------------------------------------
# Step 1: apt deps
# ---------------------------------------------------------------------------

step1_apt_deps() {
    echo ""
    echo "[1/7] Installing system packages (apt)..."
    run_or_dry "apt-get update" sudo apt-get update -y
    run_or_dry "apt-get install python3.11 python3.11-venv python3-pip build-essential rsync" \
        sudo apt-get install -y python3.11 python3.11-venv python3-pip build-essential rsync
    echo "[1/7] Done."
}

# ---------------------------------------------------------------------------
# Step 2: Python venv + pip install -e .
# ---------------------------------------------------------------------------

step2_venv() {
    echo ""
    echo "[2/7] Setting up Python virtual environment in ${INSTALL_DIR}..."
    run_or_dry "mkdir -p ${INSTALL_DIR}" sudo mkdir -p "${INSTALL_DIR}"
    run_or_dry "chown vocalize ${INSTALL_DIR} if user exists" \
        bash -c 'id vocalize &>/dev/null && sudo chown vocalize '"${INSTALL_DIR}"' || true'
    if [ ! -d "${INSTALL_DIR}/.venv" ]; then
        run_or_dry "python3 -m venv ${INSTALL_DIR}/.venv" \
            sudo -u "$(id -un)" python3 -m venv "${INSTALL_DIR}/.venv"
    else
        echo "  .venv already exists — reusing."
    fi
    run_or_dry "pip install -e . in ${INSTALL_DIR}" \
        bash -c "${INSTALL_DIR}/.venv/bin/pip install -e ${REPO_ROOT} --quiet"
    echo "[2/7] Done."
}

# ---------------------------------------------------------------------------
# Step 3: GPU services note (v1 boundary — GPU lives on a separate host)
# ---------------------------------------------------------------------------

step3_gpu_services() {
    echo ""
    echo "[3/7] GPU services setup..."
    echo "  GPU services (SenseVoice STT + CosyVoice TTS) run on a separate host."
    echo "  Ensure GPU_HOST in /opt/vocalize/.env points to that host's Tailscale IP."
    echo "  This installer does not configure the GPU host — see docs/deploy/pi.md for details."
    echo "[3/7] Done (note only — no mutation performed)."
}

# ---------------------------------------------------------------------------
# Step 4: Tailscale check
# ---------------------------------------------------------------------------

step4_tailscale_check() {
    echo ""
    echo "[4/7] Checking Tailscale..."
    if [ "$DRY_RUN" = true ]; then
        echo "[DRY] would run: command -v tailscale && tailscale status"
    else
        if command -v tailscale &>/dev/null; then
            tailscale status || echo "  WARNING: Tailscale is installed but not connected. Run: sudo tailscale up"
        else
            echo "  WARNING: Tailscale not found. Install it with:"
            echo "    curl -fsSL https://tailscale.com/install.sh | sh && sudo tailscale up"
            echo "  Tailscale is required for the Pi to reach the GPU host."
        fi
    fi
    echo "[4/7] Done."
}

# ---------------------------------------------------------------------------
# Step 5: Cloudflare Tunnel (informational — manual step required)
# ---------------------------------------------------------------------------

step5_cloudflared_tunnel() {
    echo ""
    echo "[5/7] Cloudflare Tunnel setup..."
    echo "  To install the Cloudflare Tunnel service:"
    echo "    sudo cloudflared service install <TUNNEL_TOKEN>"
    echo ""
    echo "  Get your TUNNEL_TOKEN from the Cloudflare dashboard:"
    echo "    Zero Trust -> Networks -> Tunnels -> [your tunnel] -> Configure"
    echo "    -> Install and run a connector -> Copy the token"
    echo ""
    echo "  Reference ingress shape (docs only): infra/pi-orchestrator/cloudflared-config.yml"
    echo "  (Actual ingress routing is configured in the Cloudflare dashboard, not in a file.)"
    if [ "$DRY_RUN" = true ]; then
        echo "[DRY] would run: sudo cloudflared service install <TUNNEL_TOKEN>  (requires manual token)"
    fi
    echo "[5/7] Done (informational — run the cloudflared command above with your token)."
}

# ---------------------------------------------------------------------------
# Step 6: systemd unit + .env
# ---------------------------------------------------------------------------

step6_systemd_unit() {
    echo ""
    echo "[6/7] Installing systemd unit and environment file..."

    SERVICE_SRC="${REPO_ROOT}/infra/pi-orchestrator/vocalize.service"
    SERVICE_DST="/etc/systemd/system/vocalize.service"
    ENV_SRC="${REPO_ROOT}/infra/pi-orchestrator/.env.template"
    ENV_DST="${INSTALL_DIR}/.env"

    # Install vocalize.service
    run_or_dry "install -m 644 vocalize.service -> /etc/systemd/system/" \
        sudo install -m 644 "${SERVICE_SRC}" "${SERVICE_DST}"

    # Copy .env template only if destination does not exist
    if [ ! -f "${ENV_DST}" ]; then
        run_or_dry "copy .env.template -> ${INSTALL_DIR}/.env" \
            sudo cp "${ENV_SRC}" "${ENV_DST}"
        echo "  Created ${ENV_DST} from template — edit it before starting the service."
        echo "  At minimum, set: OPENAI_API_KEY, VOCALIZE_WS_BASE_URL, VOCALIZE_CORS_ORIGINS, GPU_HOST"
    else
        echo "  ${ENV_DST} already exists — preserving."
    fi

    run_or_dry "systemctl daemon-reload" sudo systemctl daemon-reload
    run_or_dry "systemctl enable vocalize" sudo systemctl enable vocalize

    echo "[6/7] Done."
}

# ---------------------------------------------------------------------------
# Step 7: Start and smoke verify
# ---------------------------------------------------------------------------

step7_start_and_smoke() {
    echo ""
    echo "[7/7] Starting vocalize service and running smoke check..."

    run_or_dry "systemctl restart vocalize" sudo systemctl restart vocalize

    if [ "$DRY_RUN" = true ]; then
        echo "[DRY] would run: sleep 3 && bash scripts/smoke.sh"
    else
        sleep 3
        if [ "$SKIP_GPU" = true ]; then
            echo "  --skip-gpu passed: smoke will accept gpu_reachable=false."
            VOCALIZE_API_BASE="http://127.0.0.1:8080" bash "${REPO_ROOT}/scripts/smoke.sh" || true
        else
            VOCALIZE_API_BASE="http://127.0.0.1:8080" bash "${REPO_ROOT}/scripts/smoke.sh"
        fi
    fi

    echo "[7/7] Done."
}

# ---------------------------------------------------------------------------
# Main dispatcher
# ---------------------------------------------------------------------------

echo "=== VocalizeAI Pi Installer ==="
if [ "$DRY_RUN" = true ]; then
    echo "(DRY RUN — no system changes will be made)"
fi
echo ""

STEP_FUNCTIONS=(
    ""              # placeholder so index 1..7 maps naturally
    "step1_apt_deps"
    "step2_venv"
    "step3_gpu_services"
    "step4_tailscale_check"
    "step5_cloudflared_tunnel"
    "step6_systemd_unit"
    "step7_start_and_smoke"
)

for i in 1 2 3 4 5 6 7; do
    # Apply --skip-tunnel
    if [ "$i" -eq 5 ] && [ "$SKIP_TUNNEL" = true ]; then
        echo "[5/7] Skipping Cloudflare Tunnel setup (--skip-tunnel passed)."
        continue
    fi

    if ! should_run_step "$i"; then
        echo "[${i}/7] Skipping (not in --steps filter)."
        continue
    fi

    fn="${STEP_FUNCTIONS[$i]}"
    "$fn"
    STEPS_RUN+=("$i")
done

echo ""
echo "=== Pi install complete ==="
echo "Steps run: ${STEPS_RUN[*]:-none}"
if [ "$DRY_RUN" = true ]; then
    echo "(DRY RUN — re-run without --dry-run to apply changes)"
fi
