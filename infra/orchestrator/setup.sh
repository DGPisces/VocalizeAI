#!/usr/bin/env bash
set -euo pipefail

# setup.sh — one-time setup for running VocalizeAI on a remote Linux host.
#
# Tested on Debian 12, Ubuntu 22.04 / 24.04, and Raspberry Pi OS (Bookworm).
# Any modern Linux distro with systemd works.
#
# Usage:
#   1. Run this script from your workstation: TARGET_HOST=<ip-or-hostname> ./setup.sh
#   2. Follow the manual cloudflared token-install + service-start steps printed at the end.
#
# Prerequisites:
#   - Remote host running a recent Linux release with systemd
#   - SSH key auth: ssh ${TARGET_USER}@${TARGET_HOST}
#   - cloudflared installed on the host (sudo snap install cloudflared OR apt install cloudflared)
#
# Env vars (PI_HOST / PI_USER are accepted as legacy aliases of TARGET_HOST / TARGET_USER):
#   TARGET_HOST    Remote host IP or hostname (required).
#   TARGET_USER    SSH user on the remote host (default: current user).
#   VOCALIZE_HOME  Remote project directory (default: /home/${TARGET_USER}/vocalize).

TARGET_HOST="${TARGET_HOST:-${PI_HOST:-}}"
if [ -z "${TARGET_HOST}" ]; then
  echo "ERROR: set TARGET_HOST (or legacy PI_HOST) to your remote host IP or hostname." >&2
  exit 1
fi
TARGET_USER="${TARGET_USER:-${PI_USER:-$(whoami)}}"
TARGET_PROJECT_DIR="${VOCALIZE_HOME:-/home/${TARGET_USER}/vocalize}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "=== VocalizeAI Orchestrator One-Time Setup ==="
echo "Target: ${TARGET_USER}@${TARGET_HOST}:${TARGET_PROJECT_DIR}"
echo ""

# ---- Step 1: Rsync repo to remote host ----
echo "[1/5] Copying project to remote host..."
rsync -avz \
  --exclude '.venv' \
  --exclude '__pycache__' \
  --exclude '*.pyc' \
  --exclude '.git' \
  --exclude 'node_modules' \
  --exclude '.omc' \
  --exclude '.planning' \
  --exclude '.env' \
  "${SCRIPT_DIR}/../../" \
  "${TARGET_USER}@${TARGET_HOST}:${TARGET_PROJECT_DIR}/"

# ---- Step 2: Create venv and install deps ----
echo "[2/5] Creating Python venv on remote host..."
ssh "${TARGET_USER}@${TARGET_HOST}" "python3 -m venv ${TARGET_PROJECT_DIR}/.venv"

echo "[3/5] Installing Python dependencies..."
ssh "${TARGET_USER}@${TARGET_HOST}" "${TARGET_PROJECT_DIR}/.venv/bin/pip install --upgrade pip"
ssh "${TARGET_USER}@${TARGET_HOST}" "${TARGET_PROJECT_DIR}/.venv/bin/pip install -e ${TARGET_PROJECT_DIR}"

# ---- Step 3: Create .env if not exists ----
echo "[4/5] Setting up .env..."
ssh "${TARGET_USER}@${TARGET_HOST}" "test -f ${TARGET_PROJECT_DIR}/.env || cp ${TARGET_PROJECT_DIR}/infra/orchestrator/.env.template ${TARGET_PROJECT_DIR}/.env"
echo "  -> .env created from template (edit it with real values before starting the service)"

# ---- Step 4: Install systemd service ----
echo "[5/5] Installing systemd vocalize service..."

VOCALIZE_SERVICE=$(cat <<'SERVICEEOF'
[Unit]
Description=VocalizeAI orchestrator service
After=network-online.target
Wants=network-online.target

[Service]
# Install path is /opt/vocalize by convention; override by editing this unit
# or by running setup.sh with VOCALIZE_HOME=/your/path set.
Type=simple
User=vocalize
WorkingDirectory=/opt/vocalize
EnvironmentFile=/opt/vocalize/.env
ExecStart=/opt/vocalize/.venv/bin/python -m vocalize.main
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
SERVICEEOF
)

ssh "${TARGET_USER}@${TARGET_HOST}" "echo '${VOCALIZE_SERVICE}' | sudo tee /etc/systemd/system/vocalize.service > /dev/null"
ssh "${TARGET_USER}@${TARGET_HOST}" "sudo systemctl daemon-reload"
ssh "${TARGET_USER}@${TARGET_HOST}" "sudo systemctl enable vocalize"

echo "  -> vocalize.service installed and enabled"

echo ""
echo "=== Setup complete ==="
echo ""
echo "=== MANUAL STEPS REQUIRED ==="
echo ""
echo "1. Edit .env on the remote host with real values:"
echo "     ssh ${TARGET_USER}@${TARGET_HOST} 'nano ${TARGET_PROJECT_DIR}/.env'"
echo ""
echo "2. Install cloudflared service on the host using a tunnel token (token-based auth):"
echo "   a. Get the connector token from the Cloudflare dashboard:"
echo "        Zero Trust → Networks → Tunnels → your-tunnel-name → Configure"
echo "        → Install and run a connector → copy the long token string"
echo "   b. SSH to the host and install the service with that token:"
echo "        ssh ${TARGET_USER}@${TARGET_HOST} 'sudo cloudflared service install <TUNNEL_TOKEN>'"
echo "      The token embeds tunnel id, credentials, and ingress config — no on-disk"
echo "      config.yml is needed. (If you ever want to inspect intended ingress, see"
echo "      infra/orchestrator/cloudflared-config.yml; that file is reference-only.)"
echo ""
echo "3. Make sure Public Hostname routing is configured in the dashboard:"
echo "        api.example.com → http://localhost:8080"
echo ""
echo "4. Start services:"
echo "     ssh ${TARGET_USER}@${TARGET_HOST} 'sudo systemctl start vocalize cloudflared'"
echo ""
echo "5. Verify both services are running and the public URL responds:"
echo "     ssh ${TARGET_USER}@${TARGET_HOST} 'sudo systemctl status vocalize cloudflared --no-pager'"
echo "     curl -fsS https://api.example.com/health"
echo ""
echo "After completing manual steps, use deploy.sh for subsequent deploys."
