# Deploying VocalizeAI on a Linux Host

This runbook covers end-to-end production deployment of VocalizeAI on any
modern Linux host with systemd: the orchestrator runs on this host, GPU
services (SenseVoice STT + CosyVoice TTS) run on a separate machine reachable
over Tailscale, and a Cloudflare Tunnel fronts the orchestrator host to the
public internet.

Tested on **Debian 12**, **Ubuntu 22.04 / 24.04**, and **Raspberry Pi OS
(Bookworm)**. A Raspberry Pi was the original reference target — see
["Hardware example: Raspberry Pi"](#hardware-example-raspberry-pi) below for
the BOM, OS imaging, and SSH bootstrap steps for that specific target.

---

## Bill of Materials

**Orchestrator host:**
- Any 64-bit Linux box with systemd, ≥ 2 GB RAM, ≥ 16 GB free disk.
- Python 3.11 (installed by step 1 of `install/install.sh`).
- Persistent internet connection (Cloudflare Tunnel requires outbound HTTPS).

**GPU node (separate machine):**
- NVIDIA RTX-class GPU (GTX 1080 or better; RTX 30/40 series recommended).
- Windows + WSL2 or Linux (PyTorch 2.7.1+cu128).
- Reachable from the orchestrator host over Tailscale on the configured
  `GPU_HOST` IP/hostname.

**Network:**
- Tailscale account (free tier is sufficient) with both the orchestrator host
  and the GPU node enrolled.
- Cloudflare account with a domain pointed at Cloudflare DNS (free tier is
  sufficient).

---

## OS Preparation

```bash
# On the orchestrator host (any modern 64-bit Linux with systemd):
ssh <user>@<host>
sudo apt-get update && sudo apt-get upgrade -y

# Ensure git and curl are present:
sudo apt-get install -y git curl
```

For the Raspberry Pi-specific imaging / first-boot steps, see
["Hardware example: Raspberry Pi"](#hardware-example-raspberry-pi).

---

## Tailscale Setup

Tailscale provides the encrypted overlay network between the orchestrator
host and the GPU node.

```bash
# Install Tailscale on the orchestrator host:
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up

# Verify the GPU node is visible:
tailscale status
# You should see your GPU node listed with its Tailscale IP.

# Test reachability (replace <tailscale-ip> with your GPU node's Tailscale IP):
# nc -zv <tailscale-ip> 8000   # SenseVoice STT
# nc -zv <tailscale-ip> 8001   # CosyVoice TTS
```

Set `GPU_HOST` in `/opt/vocalize/.env` to the GPU node's Tailscale IP.

If the GPU services are not yet running, use `install/install.sh --skip-gpu`
to proceed with installation without the GPU-reachability check.

---

## Clone and Install

```bash
# Clone the repository to the orchestrator host:
git clone https://github.com/DGPisces/VocalizeAI.git /opt/vocalize
cd /opt/vocalize

# Dry-run first to preview all 7 steps:
bash install/install.sh --dry-run

# Run the full installer:
bash install/install.sh

# Or run selectively:
bash install/install.sh --steps "1,2,6"   # apt + venv + systemd only
bash install/install.sh --skip-gpu         # skip GPU-reachability check in step 7
bash install/install.sh --skip-tunnel      # skip step 5 (Cloudflare Tunnel info)
```

**Installer steps:**

| Step | Action |
|------|--------|
| 1 | `apt-get install` python3.11 python3.11-venv python3-pip build-essential rsync |
| 2 | Create `.venv` in `/opt/vocalize`, `pip install -e .` |
| 3 | GPU services note (GPU lives on a separate host; no on-orchestrator install) |
| 4 | Tailscale presence check (warns if absent) |
| 5 | Cloudflare Tunnel token-install instructions |
| 6 | Copy `vocalize.service` to `/etc/systemd/system/`, copy `.env.template` to `/opt/vocalize/.env` if absent, `systemctl enable vocalize` |
| 7 | `systemctl restart vocalize` + `bash scripts/smoke.sh` |

The installer is idempotent — every step is safe to re-run on an existing deployment.

---

## Environment Configuration

After step 6 copies `.env.template` to `/opt/vocalize/.env`, edit it:

```bash
sudo nano /opt/vocalize/.env
```

**Full env-var reference** (17 keys from `.env.example`):

| Key | Required? | Purpose |
|-----|-----------|---------|
| `OPENAI_API_KEY` | **yes** | LLM authentication (any OpenAI-compatible provider) |
| `OPENAI_BASE_URL` | default ok | LLM endpoint; default `https://api.deepseek.com/v1` |
| `OPENAI_MODEL` | default ok | Model name; default `deepseek-chat` |
| `GPU_HOST` | yes (if using GPU) | STT/TTS host — Tailscale IP of your GPU node |
| `SENSEVOICE_WS_PORT` | default ok | STT port; default `8000` |
| `COSYVOICE_WS_PORT` | default ok | TTS port; default `8001` |
| `VOCALIZE_HOST` | default ok | uvicorn bind host; set to `0.0.0.0` for production |
| `VOCALIZE_PORT` | default ok | uvicorn bind port; default `8080` |
| `ORCHESTRATOR_LISTEN_PORT` | default ok | Orchestrator service port; default `8080` (legacy compatibility) |
| `VOCALIZE_WS_BASE_URL` | **yes** | Public WS base URL; e.g. `wss://api.<your-domain>` — startup raises if missing in non-localhost mode |
| `VOCALIZE_CORS_ORIGINS` | default ok | Comma-separated allowed CORS origins; default auto-picked from VOCALIZE_HOST |
| `DEFAULT_LANGUAGE` | default ok | `zh` or `en`; default `zh` |
| `LOG_DIR` | default ok | Log directory; default `logs` |
| `NEXT_PUBLIC_VOCALIZE_API_BASE_URL` | yes (for frontend) | Frontend API base URL; baked into JS bundle at build time |
| `NEXT_PUBLIC_VOCALIZE_WS_BASE_URL` | optional | Frontend WS base; derived from API base if absent |

**Example production `.env` (use your own values for all `<...>` placeholders):**

```bash
OPENAI_API_KEY=<your-openai-compatible-api-key>
OPENAI_BASE_URL=https://api.deepseek.com/v1
OPENAI_MODEL=deepseek-chat
GPU_HOST=<tailscale-ip-of-gpu-node>
SENSEVOICE_WS_PORT=8000
COSYVOICE_WS_PORT=8001
VOCALIZE_HOST=0.0.0.0
VOCALIZE_PORT=8080
VOCALIZE_WS_BASE_URL=wss://api.<your-domain>
VOCALIZE_CORS_ORIGINS=https://<your-domain>
```

---

## Cloudflare Tunnel

The Cloudflare Tunnel connects the orchestrator host to the public internet without exposing SSH
or opening firewall ports.

**Token-based install (recommended):**

```bash
# Get your tunnel token from the Cloudflare dashboard:
# Zero Trust -> Networks -> Tunnels -> [your tunnel] -> Configure
# -> "Install and run a connector" -> Copy the displayed token

# Install the tunnel service on the orchestrator host:
sudo cloudflared service install <TUNNEL_TOKEN>

# Verify the service is running:
sudo systemctl status cloudflared
```

The reference ingress shape for this project is documented in
`infra/orchestrator/cloudflared-config.yml` (maps
`vocalize-api.<your-tunnel-name>` → `http://localhost:8080` and
`vocalize.<your-tunnel-name>` → `http://localhost:3000`). Configure
the actual public hostname routing in the Cloudflare dashboard under
your tunnel's Public Hostnames tab.

Use `<your-tunnel-name>` as the tunnel name — do not use another person's
tunnel name; tunnels are account-specific.

---

## systemd Units

### vocalize.service

The `vocalize.service` unit file is at `infra/orchestrator/vocalize.service`
and is copied to `/etc/systemd/system/vocalize.service` by step 6 of the installer.

```ini
[Unit]
Description=VocalizeAI Orchestrator
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=vocalize
WorkingDirectory=/opt/vocalize
ExecStart=/opt/vocalize/.venv/bin/python -m vocalize.main
Restart=always
RestartSec=5
EnvironmentFile=/opt/vocalize/.env

[Install]
WantedBy=multi-user.target
```

**Note:** The unit runs as the `vocalize` system user. Create it if it does not
exist: `sudo useradd --system --home /opt/vocalize --shell /bin/false vocalize`
then `sudo chown -R vocalize:vocalize /opt/vocalize`.

### cloudflared.service

`cloudflared service install <TOKEN>` installs its own systemd unit automatically.
No manual unit file is needed.

**Useful commands:**

```bash
# Check status of both services:
sudo systemctl status vocalize cloudflared

# Tail orchestrator logs:
sudo journalctl -u vocalize -f

# Tail tunnel logs:
sudo journalctl -u cloudflared -n 50

# Restart orchestrator after a config change:
sudo systemctl restart vocalize
```

---

## Smoke Verification

After the installer completes (step 7 runs this automatically), verify the deployment:

```bash
# Smoke test against the orchestrator's local port:
VOCALIZE_API_BASE=http://127.0.0.1:8080 bash scripts/smoke.sh
# Exit 0 = working deployment

# Smoke test against the public Cloudflare Tunnel URL:
VOCALIZE_API_BASE=https://api.<your-domain> bash scripts/smoke.sh
```

The smoke script exercises 6 round-trips: `GET /health`, `POST /api/sessions`,
`POST /api/sessions/{id}/task`, WS upgrade + send/recv, `DELETE /api/sessions/{id}`.

Note: the local smoke uses port 8080 (production port), not 8000 (dev
port). Make sure `VOCALIZE_API_BASE` is set accordingly.

---

## 24-Hour Stability Check

After the deployment has been running for 24 hours, verify stability:

```bash
# Check orchestrator status and uptime:
sudo systemctl status vocalize
# Expected: active (running) with significant uptime

# Check restart count (should be 0 on a stable deployment):
systemctl show vocalize -p NRestarts

# Run a fresh smoke to confirm still operational:
VOCALIZE_API_BASE=http://127.0.0.1:8080 bash scripts/smoke.sh
```

The maintainer-side `scripts/stability-24h-driver.py` script is the rehearsal
harness used to generate the v1.0 stability evidence run. External operators do
not need to run it; the `systemctl`/`smoke.sh` checks above are the operator-facing
stability gate.

---

## Troubleshooting

**Tunnel not connecting:**
```bash
sudo journalctl -u cloudflared -n 50
```
Check the Cloudflare dashboard for connector status. Ensure the tunnel token
matches the connector the dashboard expects.

**GPU services unreachable (`gpu_reachable=false` in `/health`):**
```bash
# Confirm Tailscale is up and the GPU node is reachable:
tailscale status

# Test TCP connectivity to each GPU service port:
nc -zv $GPU_HOST $SENSEVOICE_WS_PORT    # e.g. nc -zv 100.x.y.z 8000
nc -zv $GPU_HOST $COSYVOICE_WS_PORT     # e.g. nc -zv 100.x.y.z 8001
```
Check that SenseVoice and CosyVoice are running on the GPU host.

**Orchestrator fails to start:**
```bash
sudo journalctl -u vocalize -n 50
```
Common causes:
- `OPENAI_API_KEY` missing or invalid — Layer 1 will fail on the first task
- `VOCALIZE_WS_BASE_URL` unset in production mode — raises `RuntimeError` at startup (D-11 guard)
- `.env` file not at `/opt/vocalize/.env` — check `EnvironmentFile=` in the unit

**Port conflicts:**
- `VOCALIZE_PORT` defaults to 8080. If another service occupies that port, change
  `VOCALIZE_PORT` in `.env` and update the Cloudflare Tunnel ingress rule accordingly.

---

## Hardware example: Raspberry Pi

The Raspberry Pi was the original reference target for this runbook. None of
the steps above are Pi-specific; this section just captures the bits that
differ when the orchestrator host happens to be a Pi.

### BOM

- Raspberry Pi 4 or Pi 5, **8 GB RAM recommended** (4 GB works for the
  orchestrator alone but is tight if other services run alongside).
- 32 GB+ microSD card or USB SSD (SSD strongly recommended for production).
- Reliable internet connection (Cloudflare Tunnel requires outbound HTTPS).

### Imaging and first boot

```bash
# Flash Raspberry Pi OS Lite (64-bit) to the SD card / SSD using Raspberry
# Pi Imager. In Imager, pre-configure:
#   - hostname
#   - SSH enabled
#   - SSH public key (paste your ~/.ssh/id_ed25519.pub or generate one first)
#   - Wi-Fi credentials (if not using Ethernet)

# After first boot, SSH in and update the system:
ssh pi@<pi-hostname>
sudo apt-get update && sudo apt-get upgrade -y

# Ensure git and curl are present:
sudo apt-get install -y git curl
```

From here on, the rest of this runbook (Tailscale, install, Cloudflare
Tunnel, smoke) applies unchanged.
