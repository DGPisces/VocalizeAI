#!/usr/bin/env bash
set -euo pipefail

# deploy.sh — rsync VocalizeAI from your workstation to a remote Linux host
# and restart the vocalize + cloudflared services.
#
# Usage:
#   TARGET_HOST=<ip-or-hostname> ./deploy.sh
#
# Prerequisites:
#   - SSH key auth to ${TARGET_HOST}
#   - setup.sh has been run on the host at least once
#   - cloudflared-config.yml has a real tunnel ID (not placeholder)
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
LOCAL_PROJECT_DIR="$(cd "$(dirname "$0")/../.." && pwd)"

echo "=== Deploying VocalizeAI to ${TARGET_USER}@${TARGET_HOST} ==="

echo "[1/5] Rsync project to remote host..."
rsync -avz \
  --exclude '.venv' \
  --exclude '__pycache__' \
  --exclude '*.pyc' \
  --exclude '.git' \
  --exclude 'node_modules' \
  --exclude '.omc' \
  --exclude '.planning' \
  --exclude '.env' \
  "${LOCAL_PROJECT_DIR}/" \
  "${TARGET_USER}@${TARGET_HOST}:${TARGET_PROJECT_DIR}/"

echo "[2/5] Install/update Python deps on remote host..."
ssh "${TARGET_USER}@${TARGET_HOST}" \
  "cd ${TARGET_PROJECT_DIR} && .venv/bin/pip install -e ."

echo "[3/5] Restart vocalize service..."
ssh "${TARGET_USER}@${TARGET_HOST}" \
  "sudo systemctl restart vocalize"

echo "[4/5] Restart cloudflared service..."
ssh "${TARGET_USER}@${TARGET_HOST}" \
  "sudo systemctl restart cloudflared"

echo "[5/5] Check service status..."
ssh "${TARGET_USER}@${TARGET_HOST}" \
  "sudo systemctl status vocalize cloudflared --no-pager -l"

echo "=== Deploy complete ==="
