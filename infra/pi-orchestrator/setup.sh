#!/usr/bin/env bash
set -euo pipefail

# setup.sh — one-time setup for running VocalizeAI on a Raspberry Pi.
#
# Usage:
#   1. Run this script from the Mac: ./setup.sh
#   2. Follow the manual cloudflared token-install + service-start steps printed at the end.
#
# Prerequisites:
#   - Raspberry Pi running Ubuntu 24.04
#   - SSH key auth: ssh <pi-user>@<PI_HOST>
#   - cloudflared installed on Pi (sudo snap install cloudflared  OR  apt install cloudflared)

PI_HOST="${PI_HOST:?set to your Pi IP or hostname}"
PI_USER="${PI_USER:-$(whoami)}"
PI_PROJECT_DIR="${VOCALIZE_HOME:-/home/${PI_USER}/vocalize}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "=== VocalizeAI Pi One-Time Setup ==="
echo ""

# ---- Step 1: Rsync repo to Pi ----
echo "[1/5] Copying project to Pi..."
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
  "${PI_USER}@${PI_HOST}:${PI_PROJECT_DIR}/"

# ---- Step 2: Create venv and install deps ----
echo "[2/5] Creating Python venv on Pi..."
ssh "${PI_USER}@${PI_HOST}" "python3 -m venv ${PI_PROJECT_DIR}/.venv"

echo "[3/5] Installing Python dependencies..."
ssh "${PI_USER}@${PI_HOST}" "${PI_PROJECT_DIR}/.venv/bin/pip install --upgrade pip"
ssh "${PI_USER}@${PI_HOST}" "${PI_PROJECT_DIR}/.venv/bin/pip install -e ${PI_PROJECT_DIR}"

# ---- Step 3: Create .env if not exists ----
echo "[4/5] Setting up .env..."
ssh "${PI_USER}@${PI_HOST}" "test -f ${PI_PROJECT_DIR}/.env || cp ${PI_PROJECT_DIR}/infra/pi-orchestrator/.env.template ${PI_PROJECT_DIR}/.env"
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

ssh "${PI_USER}@${PI_HOST}" "echo '${VOCALIZE_SERVICE}' | sudo tee /etc/systemd/system/vocalize.service > /dev/null"
ssh "${PI_USER}@${PI_HOST}" "sudo systemctl daemon-reload"
ssh "${PI_USER}@${PI_HOST}" "sudo systemctl enable vocalize"

echo "  -> vocalize.service installed and enabled"

echo ""
echo "=== Setup complete ==="
echo ""
echo "=== MANUAL STEPS REQUIRED ==="
echo ""
echo "1. Edit .env on the Pi with real values:"
echo "     ssh ${PI_USER}@${PI_HOST} 'nano ${PI_PROJECT_DIR}/.env'"
echo ""
echo "2. Install cloudflared service on Pi using a tunnel token (token-based auth):"
echo "   a. Get the connector token from the Cloudflare dashboard:"
echo "        Zero Trust → Networks → Tunnels → dgpisces-server1 → Configure"
echo "        → Install and run a connector → copy the long token string"
echo "   b. SSH to Pi and install the service with that token:"
echo "        ssh ${PI_USER}@${PI_HOST} 'sudo cloudflared service install <TUNNEL_TOKEN>'"
echo "      The token embeds tunnel id, credentials, and ingress config — no on-disk"
echo "      config.yml is needed. (If you ever want to inspect intended ingress, see"
echo "      infra/pi-orchestrator/cloudflared-config.yml; that file is reference-only.)"
echo ""
echo "3. Make sure Public Hostname routing is configured in the dashboard:"
echo "        vocalize-api.dgpisces.com → http://localhost:8080"
echo ""
echo "4. Start services:"
echo "     ssh ${PI_USER}@${PI_HOST} 'sudo systemctl start vocalize cloudflared'"
echo ""
echo "5. Verify both services are running and the public URL responds:"
echo "     ssh ${PI_USER}@${PI_HOST} 'sudo systemctl status vocalize cloudflared --no-pager'"
echo "     curl -fsS https://vocalize-api.dgpisces.com/health"
echo ""
echo "After completing manual steps, use deploy.sh for subsequent deploys."
