# VocalizeAI

> Chinese version: [README.zh-CN.md](README.zh-CN.md)

VocalizeAI is a bilingual (zh/en) AI phone agent. v1 transforms it from a
restaurant-only booking bot into a universal phone-task engine: describe any
phone task in natural language, and the AI plans the schema, collects the
required info from you, and handles the call with the merchant — relaying
across languages when needed.

## Current Status

**v1 ships** the universal phone-task engine, Web console, and Raspberry Pi
orchestrator deployment. The backend 5-layer prompt architecture
(task_planner / preflight / merchant_agent / clarification_collector / relay)
handles any phone task — restaurant bookings, service appointments, balance
inquiries, status checks, and more. An OSS mirror is available at
[github.com/DGPisces/VocalizeAI](https://github.com/DGPisces/VocalizeAI)
under Apache 2.0.

## Quick Start

**Prerequisites:** Python 3.11+, Node 20+, git, curl. Optional: `uv` (auto-installed by the script).

```bash
# 1. Install all dependencies in one step
bash install/dev-install.sh

# 2. Edit .env and set at minimum: OPENAI_API_KEY
#    (dev-install.sh already copied .env.example -> .env if it was absent)
$EDITOR .env

# 3. Start the backend
source .venv/bin/activate
uvicorn vocalize.main:app --host 127.0.0.1 --port 8000 --reload

# 4. Start the frontend (second terminal)
cd frontend && npm run dev

# 5. Verify the installation
bash scripts/smoke.sh
# Exit 0 = working dev environment (≤15 min from clone to passing smoke)
```

**Reproducible install:** after activating the venv, run `uv pip sync uv.lock` for
deterministic Python dependency installation pinned to the committed lock file.

For the full Mac/Linux runbook with env-var descriptions and troubleshooting, see
[docs/deploy/local.md](docs/deploy/local.md).

## v1 — Universal Phone Agent (CLI)

```bash
# (in project venv)
export OPENAI_BASE_URL="https://api.deepseek.com"
export OPENAI_API_KEY="..."
python -m demos.phase5_universal_agent_cli
```

The demo runs the full universal phone-agent engine in headless mode:

1. You describe a task in natural language ("book a 7 pm table for 4 at
   Joy Sushi").
2. Layer 1 (`task_planner`) emits a `TaskSchema` — the slots to collect,
   the readiness criteria, and the relay strategy.
3. Layer 2 (`preflight`) drives a user-side conversation until all
   high-criticality slots are filled.
4. Layer 3 (`merchant_agent`) places and runs the call.
5. Layers 4–5 (`clarification_collector`, `relay`) handle mid-call
   clarifications and cross-lingual translation.

## Repository layout

```
VocalizeAI/
├── src/vocalize/              # main backend package (service-boundary modules)
│   ├── transports/            # audio I/O — local mic, speakerphone bridge
│   ├── stt/                   # speech-to-text — SenseVoice streaming
│   ├── llm/                   # LLM — OpenAI-compatible streaming + tool-calling
│   ├── tts/                   # text-to-speech — CosyVoice streaming
│   ├── dialogue/              # orchestrator, state machine, prompts, tools
│   ├── reflection/            # post-call review
│   ├── server/                # FastAPI app — REST sessions + WS frames
│   ├── pipeline.py            # asyncio main pipeline
│   ├── config.py              # env / .env loading
│   └── logger.py              # system + dialogue logging
├── frontend/                  # Next.js 14 web console
│   ├── app/                   # App Router routes
│   ├── components/            # BrowserAudioBridge, LiveConsole, etc.
│   ├── lib/                   # WS client, audio utils, REST client
│   ├── messages/              # next-intl zh/en bundles
│   └── tests/                 # vitest unit tests
├── demos/                     # runnable demos
├── infra/                     # deployment scripts (GPU node, Pi orchestrator)
├── tests/                     # pytest suite
│   └── integration/           # Playwright laptop-loopback + AI-merchant harness
├── install/                   # one-shot install scripts
│   ├── dev-install.sh         # Mac/Linux local dev setup
│   └── pi-install.sh          # Raspberry Pi production deploy
├── docs/                      # architecture, deploy guides, release evidence
├── scripts/                   # smoke test and utility scripts
│   └── smoke.sh               # post-install end-to-end verification
├── pyproject.toml             # single source of truth for backend dependencies
├── uv.lock                    # pinned Python dependency lock
└── .env.example               # env-var template (17 keys)
```

## Self-host quickstart

### Required env vars for non-localhost deployment

| Variable | Purpose |
|----------|---------|
| `VOCALIZE_WS_BASE_URL` | WebSocket base URL returned to clients (e.g., `wss://api.example.com`); required in non-localhost mode to prevent Host-header spoofing |
| `VOCALIZE_CORS_ORIGINS` | Comma-separated allowed CORS origins; **required** in non-localhost mode (no default) |

See `.env.example` for the full env-var inventory including LLM, GPU service,
and frontend build-time variables.

For the full production Pi deployment runbook, see [docs/deploy/pi.md](docs/deploy/pi.md).

### GPU node requirements

SenseVoice (STT) and CosyVoice (TTS) run as separate GPU services and connect
to the Pi orchestrator over Tailscale. GPU services are optional for local dev
(the LLM path works without them). See [docs/deploy/pi.md](docs/deploy/pi.md)
for the GPU node setup.

## Run the dev server

```bash
source .venv/bin/activate

# optional: configure GPU services so /health reports gpu_reachable=true
export GPU_HOST=100.x.y.z            # Tailscale IP of GPU node
export SENSEVOICE_WS_PORT=8000       # STT service
export COSYVOICE_WS_PORT=8001        # TTS service

uvicorn vocalize.main:app --host 127.0.0.1 --port 8000 --reload
```

In another terminal:

```bash
curl -s http://127.0.0.1:8000/health
# → {"ok": true, "gpu_reachable": true}

SESSION=$(curl -s -X POST http://127.0.0.1:8000/api/sessions | python3 -c \
  'import sys,json; print(json.load(sys.stdin)["session_id"])')

curl -s -X POST "http://127.0.0.1:8000/api/sessions/$SESSION/task" \
  -H 'Content-Type: application/json' \
  -d '{"task":"帮我订海底捞"}'

# brew install websocat  (macOS) or  apt install websocat  (Linux)
websocat ws://127.0.0.1:8000/ws/sessions/$SESSION
# → server emits state_update / transcript_update / readiness_change frames

# Or run the full smoke test:
bash scripts/smoke.sh
```

For the system architecture — 5-layer dialogue pipeline, TaskPhase state machine,
WS frame catalogue, and REST surface — see [docs/architecture.md](docs/architecture.md).

## Run the web console

Terminal 1:

```bash
source .venv/bin/activate
uvicorn vocalize.main:app --host 127.0.0.1 --port 8000 --reload
```

Terminal 2:

```bash
cd frontend
export NEXT_PUBLIC_VOCALIZE_API_BASE_URL=http://127.0.0.1:8000
npm run dev -- --hostname 127.0.0.1 --port 3000
```

Open `http://127.0.0.1:3000`.

The frontend calls FastAPI directly through `NEXT_PUBLIC_VOCALIZE_API_BASE_URL`;
it does not proxy `/api` through Next.js. If the backend is configured with
`VOCALIZE_WS_BASE_URL` on a different host, set the matching browser allowlist
with `NEXT_PUBLIC_VOCALIZE_WS_BASE_URL`.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for how to file issues, run tests,
follow code style, and submit contributions. Issue + PR templates live under `.github/`.

## Security

VocalizeAI is self-deploy: every operator runs their own backend on
their own infrastructure, and there is no centrally hosted instance to
defend. Report any security-relevant finding via GitHub Issues — same
as any other bug — so every operator can pick up the fix. Self-deploy
operators are responsible for restricting reachability at the network
or proxy layer (Cloudflare Access, VPN, reverse-proxy auth, etc.).
Per-user authentication is v1.x scope (requirement `AUTH-01`).

## License

Apache 2.0 — see [LICENSE](LICENSE).
