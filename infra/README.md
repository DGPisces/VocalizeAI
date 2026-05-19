# infra/

Deployable services and their configuration. Each subdirectory is the canonical
home of one runtime artifact:

- `gpu-services/` — Docker Compose stack for SenseVoice (STT) and CosyVoice
  (TTS) inference servers (`docker-compose.yml`, `healthcheck.sh`, model dirs).
- `orchestrator/` — Linux-host deployment for the FastAPI orchestrator,
  with systemd + Cloudflare Tunnel wiring (`vocalize.service`,
  `cloudflared-config.yml`, `deploy.sh`, `setup.sh`). Tested on Debian /
  Ubuntu / Raspberry Pi OS; any modern Linux host with systemd works.

Contrast with `scripts/` (maintainer-run utilities, not deployed) and `tools/`
(release tooling, currently empty).
