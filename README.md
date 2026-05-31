# VocalizeAI

> Chinese version: [README.zh-CN.md](README.zh-CN.md)

VocalizeAI is a Mac-first AI phone-task assistant. You describe what the call
should accomplish, the app collects any missing details, then drives the task
through a local web console with live transcript, clarification, takeover, and
post-call review.

The public `v0.1.0` path is intentionally simple: install the macOS artifact,
configure your OpenAI-compatible LLM endpoint, and use the built-in macOS
speech provider. STT and TTS are exposed through the Vocalize Provider API, so
advanced users can replace speech services later without changing the task
engine.

## Install on macOS

Download the release zip and `SHA256SUMS` from the GitHub Release page, then run
the installer from the folder where you want `VocalizeAI/` to be created.

```bash
bash install/install.sh \
  --artifact VocalizeAI-0.1.0-macos-arm64.zip \
  --checksums SHA256SUMS
```

Then configure and start:

```bash
cd VocalizeAI
./vocalize setup
./vocalize doctor
./vocalize start
```

`setup` asks for:

- LLM base URL
- LLM API key
- LLM model
- whether to enable or disable LLM thinking mode
- local web port
- whether to add an optional global `vocalize` command
- whether `start` should open the browser automatically

You do not choose a speech model in the default macOS install. VocalizeAI starts
the bundled macOS speech helper and connects to it through the Provider API.
When browser auto-open is enabled, `start` waits for the local server health
endpoint before opening the page.

## Update or Uninstall

```bash
# update from a newer release artifact while preserving config/logs/cache
./vocalize update --artifact ../VocalizeAI-0.1.1-macos-arm64.zip --checksums ../SHA256SUMS

# remove this local install and the optional recorded global symlink
./vocalize uninstall
# or
bash uninstall.sh
```

The installer is local by default. It does not install global Python packages,
Node packages, launch agents, system services, or shell modifications. The
optional global command is a removable symlink recorded in the install config.

## What the App Provides

- Mac-first local install under `VocalizeAI/`
- LLM-only setup for ordinary users
- Native macOS STT/TTS through a bundled helper
- Provider API boundary for custom STT/TTS services
- React + Vite web console served by the packaged backend
- Task creation, readiness, live transcript, clarification, manual takeover,
  hangup/end, diagnostics, settings, and post-call review
- Chinese and English UI

## Provider API

The speech boundary is documented in [docs/provider-api.md](docs/provider-api.md).
The default helper implements the same API that custom providers use:

- health and capability discovery
- realtime STT partial/final transcript events
- streaming TTS events
- cancellation and structured errors

For `v0.1.0`, macOS is the supported public platform. Other platforms can be
added later by implementing the same Provider API.

## Development

Source development still uses a normal local toolchain.

```bash
bash install/dev-install.sh
$EDITOR .env
source .venv/bin/activate
uvicorn vocalize.main:app --host 127.0.0.1 --port 8000 --reload
```

In another terminal:

```bash
cd frontend
npm ci
npm run dev -- --host 127.0.0.1 --port 3000
```

Useful checks:

```bash
.venv/bin/python -m pytest
cd frontend && npm run lint && npm run build && npm test
bash -n install/install.sh install/uninstall.sh scripts/build-macos-release.sh
```

## Repository Layout

```text
VocalizeAI/
├── src/vocalize/              # backend package and task engine
│   ├── providers/             # STT/TTS Provider API clients
│   ├── llm/                   # OpenAI-compatible streaming client
│   ├── dialogue/              # planner, preflight, merchant agent, relay
│   ├── server/                # FastAPI app and WebSocket frames
│   └── config.py              # env and install config loading
├── macos/                     # native macOS speech provider helper
├── frontend/                  # React + Vite web console
├── install/                   # artifact installer and uninstaller
├── packaging/                 # PyInstaller packaging config
├── tools/                     # release and CI helpers
├── tests/                     # pytest suite
├── docs/                      # provider, architecture, release docs
├── pyproject.toml             # backend package metadata
├── uv.lock                    # pinned Python dependency lock
└── .env.example               # development config template
```

## Release Gates

Before a public release, CI must pass backend, Provider API, macOS helper,
frontend, packaging/installer, and public-tree audit checks. The final artifact
also requires signed/notarized macOS packaging and human clean-install testing.

## License

Apache 2.0 — see [LICENSE](LICENSE).
