#!/usr/bin/env bash
set -euo pipefail

# deploy.sh — rsync VocalizeAI from Mac to Raspberry Pi and restart the vocalize service.
#
# Usage:
#   ./deploy.sh
#
# Prerequisites:
#   - SSH key auth to Pi at ${PI_HOST}
#   - setup.sh has been run on the Pi at least once
#   - cloudflared-config.yml has a real tunnel ID (not placeholder)

PI_HOST="${PI_HOST:?set to your Pi IP or hostname}"
PI_USER="${PI_USER:-$(whoami)}"
PI_PROJECT_DIR="${VOCALIZE_HOME:-/home/${PI_USER}/vocalize}"
MAC_PROJECT_DIR="$(cd "$(dirname "$0")/../.." && pwd)"

echo "=== Deploying VocalizeAI to Pi ==="

echo "[1/4] Rsync project to Pi..."
rsync -avz \
  --exclude '.venv' \
  --exclude '__pycache__' \
  --exclude '*.pyc' \
  --exclude '.git' \
  --exclude 'node_modules' \
  --exclude '.omc' \
  --exclude '.planning' \
  --exclude '.env' \
  "${MAC_PROJECT_DIR}/" \
  "${PI_USER}@${PI_HOST}:${PI_PROJECT_DIR}/"

echo "[2/4] Install/update Python deps on Pi..."
ssh "${PI_USER}@${PI_HOST}" \
  "cd ${PI_PROJECT_DIR} && .venv/bin/pip install -e ."

echo "[3/5] Restart vocalize service..."
ssh "${PI_USER}@${PI_HOST}" \
  "sudo systemctl restart vocalize"

echo "[4/5] Restart cloudflared service..."
ssh "${PI_USER}@${PI_HOST}" \
  "sudo systemctl restart cloudflared"

echo "[5/5] Check service status..."
ssh "${PI_USER}@${PI_HOST}" \
  "sudo systemctl status vocalize cloudflared --no-pager -l"

echo "=== Deploy complete ==="
