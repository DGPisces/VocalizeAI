# infra/

Deployable services and their configuration. Each subdirectory is the canonical
home of one runtime artifact:

- `gpu-services/` — Docker Compose stack for SenseVoice (STT) and CosyVoice
  (TTS) inference servers (`docker-compose.yml`, `healthcheck.sh`, model dirs).
- `pi-orchestrator/` — Raspberry Pi deployment for the FastAPI orchestrator
  (`vocalize.service`, `cloudflared-config.yml`, `deploy.sh`, `setup.sh`).

Contrast with `scripts/` (maintainer-run utilities, not deployed) and `tools/`
(release tooling, currently empty).
