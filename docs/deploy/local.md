# Local Development Setup (Mac/Linux)

This runbook covers setting up a local VocalizeAI development environment on
macOS or Linux. It is the detailed companion to the README Quick Start.

---

## Prerequisites

**Required:**
- **Python 3.11+**
  - macOS: `brew install python@3.11`
  - Debian/Ubuntu: `sudo apt install python3.11 python3.11-venv`
- **Node 20+**
  - Via nvm (recommended): `nvm install 20 && nvm use 20`
  - macOS: `brew install node@20`
  - Debian/Ubuntu: `curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash - && sudo apt install -y nodejs`
- **git** and **curl** (standard on both platforms)

**Optional but recommended:**
- `uv` — automatically installed by `dev-install.sh`; enables `uv pip sync uv.lock`
  for deterministic Python dep installation
- `websocat` — used by `scripts/smoke.sh` for the WS step (falls back to Python `websockets`)
  - macOS: `brew install websocat`
  - Debian/Ubuntu: download from https://github.com/vi/websocat/releases
- `jq` — required by `scripts/smoke.sh` for JSON parsing
  - macOS: `brew install jq`
  - Debian/Ubuntu: `sudo apt install jq`
- `shellcheck` — for verifying shell scripts during development

---

## One-Shot Install

```bash
# 1. Clone the repository:
git clone https://github.com/DGPisces/VocalizeAI.git vocalize
cd vocalize

# 2. Run the dev installer:
bash install/dev-install.sh
```

**What the installer does:**
1. Detects Python 3.11+ (fails with a hint if absent or older)
2. Detects Node 20+ (fails with a hint if absent or older)
3. Creates `.venv` via `python -m venv .venv` (skips if already present)
4. Activates the venv and runs `pip install --upgrade pip uv`
5. If `uv.lock` exists: runs `uv pip sync uv.lock` for deterministic install,
   then `pip install -e .` to register the local package in editable mode
6. If `uv.lock` absent (fresh checkout without lock): falls back to `pip install -e .`
7. Runs `cd frontend && npm ci` to install frontend dependencies
8. Copies `.env.example` → `.env` **only if `.env` does not already exist**
   (preserves any local config you have set)

The installer is idempotent — re-running it on an existing clone is safe.

---

## Environment Configuration

After the installer runs, edit `.env`:

```bash
$EDITOR .env
```

**All 17 env vars explained:**

| Key | Required? | Purpose |
|-----|-----------|---------|
| `OPENAI_API_KEY` | **yes** | LLM authentication — any OpenAI-compatible provider (OpenAI, DeepSeek, Qwen, etc.) |
| `OPENAI_BASE_URL` | default ok | LLM endpoint; default `https://api.deepseek.com/v1` |
| `OPENAI_MODEL` | default ok | Model name; default `deepseek-chat` |
| `GPU_HOST` | only if using GPU | STT/TTS host; use `localhost` for single-machine dev, Tailscale IP for remote-GPU deployment (e.g. Raspberry Pi orchestrator → GPU node) |
| `SENSEVOICE_WS_PORT` | default ok | SenseVoice STT WebSocket port; default `8000` |
| `COSYVOICE_WS_PORT` | default ok | CosyVoice TTS WebSocket port; default `8001` |
| `VOCALIZE_HOST` | default ok | uvicorn bind host; `127.0.0.1` for local dev, `0.0.0.0` for production |
| `VOCALIZE_PORT` | default ok | uvicorn bind port; default `8080` (note: dev `main.py` defaults to 8000) |
| `ORCHESTRATOR_LISTEN_PORT` | default ok | Orchestrator service port; default `8080` (legacy; mirrors `VOCALIZE_PORT`) |
| `VOCALIZE_WS_BASE_URL` | required when non-localhost | Public WS base URL (e.g. `wss://api.example.com`); startup raises if missing in non-localhost mode (D-11) |
| `VOCALIZE_CORS_ORIGINS` | default ok | Comma-separated allowed CORS origins; auto-picked from VOCALIZE_HOST in dev mode |
| `DEFAULT_LANGUAGE` | default ok | Session default language; `zh` or `en`; default `zh` |
| `LOG_DIR` | default ok | Log directory; default `logs` |
| `NEXT_PUBLIC_VOCALIZE_API_BASE_URL` | yes for frontend | Frontend API base URL baked into the Next.js JS bundle at build time |
| `NEXT_PUBLIC_VOCALIZE_WS_BASE_URL` | optional | Frontend WS base; derived from `NEXT_PUBLIC_VOCALIZE_API_BASE_URL` if absent |

**Backend auth posture:** v1 ships no request-level auth on
`POST /api/sessions` or the WebSocket. For non-localhost deployments,
restrict reachability at the network or proxy layer (Cloudflare Access,
VPN, reverse-proxy auth, etc.). Per-user auth is v1.x scope
(requirement `AUTH-01`).

**Minimum for local dev (no GPU):**
```bash
OPENAI_API_KEY=<your-key>
NEXT_PUBLIC_VOCALIZE_API_BASE_URL=http://127.0.0.1:8000
```

With these two set, the backend and frontend work end-to-end. GPU services
(`GPU_HOST`) are optional — `GET /health` will report `gpu_reachable=false` but
the LLM path (task planning, preflight) still works.

---

## Start the Backend

```bash
# Activate the venv:
source .venv/bin/activate

# Start the backend with hot-reload:
uvicorn vocalize.main:app --host 127.0.0.1 --port 8000 --reload
```

Alternative launcher (same result, uses `uvicorn.run` under the hood):
```bash
python -m vocalize.main
```

The backend listens on `http://127.0.0.1:8000` by default. You can confirm it is
running:

```bash
curl -s http://127.0.0.1:8000/health
# → {"ok": true, "gpu_reachable": false}
```

---

## Start the Frontend

Open a second terminal:

```bash
cd frontend
npm run dev -- --hostname 127.0.0.1 --port 3000
```

Open `http://127.0.0.1:3000` in your browser.

The frontend calls the backend directly through `NEXT_PUBLIC_VOCALIZE_API_BASE_URL`.
If the frontend was built without setting this variable, it will default to
`http://127.0.0.1:8000`. Set it explicitly in `.env` for consistent behaviour.

---

## Verify

```bash
bash scripts/smoke.sh
```

Exit code 0 = the development environment is working. The smoke script exercises
6 round-trips: health check, create session, set task, WS upgrade + send/recv,
delete session. Total runtime is ~20 seconds.

The smoke script uses `VOCALIZE_API_BASE` (default `http://127.0.0.1:8000`).

---

## Running Tests

**Backend (pytest):**
```bash
source .venv/bin/activate
pytest
```

**Frontend unit tests (vitest):**
```bash
cd frontend && npm test
```

**Frontend integration tests (Playwright):**
```bash
cd frontend && npm run test:integration
```

Note: `tests/integration/` release-audio cases require a physical audio setup
(microphone + speaker) and a live Linux-host orchestrator. These are gated behind
`--release-audio` and do not run in PR CI. The standard integration test suite
(`npm run test:integration`) runs the 8 text-bypass AI-merchant scenarios and
does not require physical hardware.

---

## Troubleshooting

**Backend won't start:**
- Check that the venv is activated: `source .venv/bin/activate`
- Check for a missing `OPENAI_API_KEY`: the app will start but Layer 1 will fail on
  the first task set
- Check for missing `VOCALIZE_WS_BASE_URL` in non-localhost mode: startup raises
  `RuntimeError` (D-11 guard) — set it or switch to `VOCALIZE_HOST=127.0.0.1`

**Frontend can't reach backend:**
- Check that `NEXT_PUBLIC_VOCALIZE_API_BASE_URL` in `.env` matches the backend port
  (default `http://127.0.0.1:8000`)
- Restart the frontend dev server after editing `.env` (Next.js bakes env vars at
  build time; hot-reload does NOT pick up `.env` changes)

**`scripts/smoke.sh` fails on the WS step:**
- Install `websocat`: `brew install websocat` (macOS) or download the binary from
  https://github.com/vi/websocat/releases
- Alternatively, ensure the Python `websockets` package is installed in the venv:
  it is already a declared dependency in `pyproject.toml`, so `pip install -e .` or
  `uv pip sync uv.lock` should cover it
- Check that the backend is actually running and the WS route is up:
  `curl -s http://127.0.0.1:8000/health` should return `{"ok": true, ...}`

**`/health` returns `gpu_reachable=false`:**
- This is expected when GPU services are not running. The LLM path works without
  GPU; only STT/TTS (audio pipeline) require the GPU host.
- To enable GPU: set `GPU_HOST` to your GPU node's Tailscale IP and ensure
  SenseVoice + CosyVoice are running on that host.
