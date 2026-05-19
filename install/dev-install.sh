#!/usr/bin/env bash
set -euo pipefail
IFS=$'\n\t'

# ---------------------------------------------------------------------------
# VocalizeAI dev install — cross-platform (Mac + Linux)
# Installs all dependencies and sets up a local development environment.
# ---------------------------------------------------------------------------

echo "=== VocalizeAI dev install ==="
echo ""

# ---------------------------------------------------------------------------
# 1. Detect Python >= 3.11
# ---------------------------------------------------------------------------

PYTHON_BIN=""
for candidate in python3 python python3.11; do
    if command -v "$candidate" &>/dev/null; then
        version_output=$("$candidate" --version 2>&1 || true)
        major=$(echo "$version_output" | grep -oE '[0-9]+\.[0-9]+' | head -1 | cut -d. -f1)
        minor=$(echo "$version_output" | grep -oE '[0-9]+\.[0-9]+' | head -1 | cut -d. -f2)
        if [ -n "$major" ] && [ -n "$minor" ] && [ "$major" -ge 3 ] && [ "$minor" -ge 11 ]; then
            PYTHON_BIN="$candidate"
            break
        fi
    fi
done

if [ -z "$PYTHON_BIN" ]; then
    echo "ERROR: Python 3.11+ not found."
    echo "Install it before re-running:"
    echo "  macOS:  brew install python@3.11"
    echo "  Debian/Ubuntu: sudo apt install python3.11"
    exit 1
fi

echo "Python: $($PYTHON_BIN --version)"

# ---------------------------------------------------------------------------
# 2. Detect Node >= 20
# ---------------------------------------------------------------------------

if ! command -v node &>/dev/null; then
    echo "ERROR: Node.js not found."
    echo "Install it before re-running:"
    echo "  nvm:   nvm install 20"
    echo "  macOS: brew install node@20"
    echo "  Debian/Ubuntu: curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash - && sudo apt install -y nodejs"
    exit 1
fi

NODE_VERSION=$(node --version | grep -oE '[0-9]+' | head -1)
if [ "$NODE_VERSION" -lt 20 ]; then
    echo "ERROR: Node.js $NODE_VERSION found, but 20+ is required."
    echo "Upgrade via nvm: nvm install 20 && nvm use 20"
    exit 1
fi

echo "Node: $(node --version)"

# ---------------------------------------------------------------------------
# 3. Create .venv (idempotent)
# ---------------------------------------------------------------------------

if [ ! -d ".venv" ]; then
    echo ""
    echo "Creating virtual environment..."
    # equivalent to: python -m venv .venv
    "$PYTHON_BIN" -m venv .venv
else
    echo "Virtual environment already exists (.venv) — reusing."
fi

# ---------------------------------------------------------------------------
# 4. Activate venv
# ---------------------------------------------------------------------------

# shellcheck source=/dev/null
source .venv/bin/activate

# ---------------------------------------------------------------------------
# 5. Bootstrap uv
# ---------------------------------------------------------------------------

echo ""
echo "Upgrading pip and installing uv..."
pip install --upgrade pip uv --quiet

# ---------------------------------------------------------------------------
# 6. Install Python dependencies
# ---------------------------------------------------------------------------

echo ""
if [ -f "uv.lock" ]; then
    echo "Installing from uv.lock (deterministic)..."
    # uv pip sync installs exact locked versions but does not install the local
    # project in editable mode, so we follow with pip install -e . for that.
    uv pip sync uv.lock --quiet
    echo "Installing local package in editable mode..."
    pip install -e . --quiet --no-deps
else
    echo "uv.lock not found — falling back to pip install -e . (non-deterministic)."
    pip install -e . --quiet
fi

# ---------------------------------------------------------------------------
# 7. Install frontend dependencies
# ---------------------------------------------------------------------------

echo ""
echo "Installing frontend dependencies (npm ci)..."
(cd frontend && npm ci --silent)

# ---------------------------------------------------------------------------
# 8. Copy .env.example -> .env (only if .env does not already exist)
# ---------------------------------------------------------------------------

echo ""
if [ ! -f ".env" ]; then
    cp .env.example .env
    echo "Created .env from .env.example — edit it before running smoke.sh."
    echo "  At minimum, set OPENAI_API_KEY."
else
    echo "Preserving existing .env."
fi

# ---------------------------------------------------------------------------
# 9. Summary
# ---------------------------------------------------------------------------

echo ""
echo "=== Install complete ==="
echo ""
echo "Next steps:"
echo "  1. Activate venv:    source .venv/bin/activate"
echo "  2. Edit .env:        set OPENAI_API_KEY (and GPU_HOST if using STT/TTS)"
echo "  3. Start backend:    uvicorn vocalize.main:app --host 127.0.0.1 --port 8000 --reload"
echo "  4. Start frontend (second terminal):"
echo "                       cd frontend && npm run dev"
echo "  5. Verify:           bash scripts/smoke.sh"
echo ""
echo "For the full Mac/Linux runbook, see docs/deploy/local.md."
