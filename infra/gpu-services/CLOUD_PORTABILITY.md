# Cloud Portability Checklist

本文件是 GPU 推理节点从 Windows → 云 GPU 实例迁移前的审查工具。每条原则给出代码/配置位置作为证据；维护者修改服务时如果某条变红应同时更新本表。

| # | 状态 | 原则（来自 plan） | 证据 |
|---|---|---|---|
| 1 | ✅ | 基础镜像用 `nvidia/cuda:12.x-runtime-ubuntu22.04`，不依赖 Windows / WSL 特性 | [`sensevoice/Dockerfile:6`](./sensevoice/Dockerfile)、[`cosyvoice/Dockerfile:8`](./cosyvoice/Dockerfile) — 都是 `nvidia/cuda:12.4.0-runtime-ubuntu22.04`；apt 装的是标准 Ubuntu 22.04 包，无 wsl-utilities 之类 |
| 2 | ✅ | 配置全部 env vars（路径/端口/日志/并发），无硬编码 | [`sensevoice/server.py:67-77`](./sensevoice/server.py)、[`cosyvoice/server.py:84-99`](./cosyvoice/server.py) — 顶层全部 `os.getenv(...)`；[`docker-compose.yml`](./docker-compose.yml) 通过 `${VAR:-default}` 注入；[`.env.example`](./.env.example) 是 single source of truth |
| 3 | ✅ | 数据挂载：模型 → `/models`，日志 → `/var/log/vocalize` | [`docker-compose.yml:39-40,77-79`](./docker-compose.yml) — 两服务都挂 `./models:/models` 与 `./logs/<service>:/var/log/vocalize`；Dockerfile 内 `RUN mkdir -p /models /var/log/vocalize` 兜底；`HF_HOME` / `MODELSCOPE_CACHE` / `TORCH_HOME` 全指向 `/models` |
| 4 | ✅ | `/health` 暴露 GPU 状态、模型加载状态、queue depth | [`sensevoice/server.py:537-561`](./sensevoice/server.py)、[`cosyvoice/server.py:696-714`](./cosyvoice/server.py) — 返回 `model_loaded`、`gpu_available`、`active_sessions`、`queue_depth`、`shutting_down`；HTTP 200/503 与 docker HEALTHCHECK 协同 |
| 5 | ✅ | Prometheus `/metrics` 暴露请求数/延迟/错误率/GPU 显存 | [`sensevoice/server.py:140-159`](./sensevoice/server.py)、[`cosyvoice/server.py:131-163`](./cosyvoice/server.py) — Counters/Histograms/Gauges 完备：`*_inferences_total{outcome=ok\|error}`、`*_inference_latency_seconds`、`*_active_sessions`、`*_queue_depth`、`*_gpu_memory_allocated_bytes`；CosyVoice 多了 `cosyvoice_first_audio_latency_seconds` 作为 UX 关键指标 |
| 6 | ✅ | TLS-ready（服务自身纯 WS，由前置反向代理做 TLS termination） | 服务监听 `0.0.0.0` 的纯 ws://（无 TLS）— 见 [`sensevoice/server.py` `main()`](./sensevoice/server.py)、[`cosyvoice/server.py` `main()`](./cosyvoice/server.py)；Tailscale Funnel / Caddy / nginx / 云 LB 在前置做 TLS。镜像内不打包证书，迁云无证书重新签发的脏路径 |
| 7 | ✅ | 优雅停机（SIGTERM 排空进行中请求） | [`sensevoice/server.py:495-510, 519-525`](./sensevoice/server.py)、[`cosyvoice/server.py:665-688`](./cosyvoice/server.py) — lifespan 注册 SIGTERM/SIGINT handler → set `shutdown_event` → `srv.should_exit=True` → 拒绝新 WS（`/health` 503，新 ws 1013 close）→ 等 `active_session_count==0` 或 `GRACEFUL_TIMEOUT_SEC`；docker-compose `stop_grace_period: 90s` 给足时间 |
| 8 | ✅ | 资源限制可配（`deploy.resources.reservations.devices` 声明 GPU） | [`docker-compose.yml:54-60, 95-101`](./docker-compose.yml) — 用 `driver: nvidia, count: 1, capabilities: [gpu]`；这套 schema 在 Windows nvidia-container-toolkit 与 Linux nvidia-docker 上语义一致，无需修改 |
| 9 | ✅ | 日志结构化 JSON 输出到 stdout | [`sensevoice/server.py:101-129`](./sensevoice/server.py)、[`cosyvoice/server.py:99-126`](./cosyvoice/server.py) — `JsonFormatter` 把 `level/ts/logger/msg + extra` 序列化成单行 JSON 写 stdout；container runtime 抓走（docker logs / k8s logs / journald 都兼容） |
| 10 | ✅ | 本文件维护"从 Windows 搬到云 GPU"的步骤清单 | 即下方 [迁移步骤](#迁移步骤) 节 |

---

## 迁移步骤（Windows → 云 GPU）

> 假设：仓库已 push 到云 GPU 实例可访问的 git remote；Tailscale 加入同一 tailnet。

| 步 | 改什么 | 改在哪 | 备注 |
|---|---|---|---|
| 1 | clone 仓库 | 云实例 | `git clone <repo> && cd VocalizeAI/infra/gpu-services` |
| 2 | `cp .env.example .env` | 云实例 | 配置项不需要新增，仅调整既有项 |
| 3 | `GPU_HOST_BIND` | `.env` | 云上保持 `0.0.0.0`，由 SG/防火墙控制公网；**不要**绑公网 IP，太敏感 |
| 4 | `SENSEVOICE_DEVICE` / `COSYVOICE_DEVICE` | `.env` | 多卡云实例可分别绑 cuda:0 / cuda:1 |
| 5 | `*_MAX_SESSIONS` | `.env` | 显存更大的卡（A100 80GB / H100）放宽到 8 / 4 |
| 6 | （可选）`COSYVOICE_MODEL_DIR` | `.env` | 想把模型放到挂载的对象存储/EBS 卷上时改路径；默认 `/models` 即可 |
| 7 | （可选）模型预下载 | 云实例 | 提前 `aws s3 sync s3://your-bucket/models ./models`，跳过容器内首次下载耗时 |
| 8 | `docker compose up -d --build` | 云实例 | 与 Windows 完全一样的命令 |
| 9 | Pi 应用 `.env` 改 `GPU_HOST=<云实例 Tailscale 100.x 地址>` | Pi | 应用层零代码改动 |
| 10 | `systemctl restart vocalize` | Pi | 验证连接：`bash infra/gpu-services/healthcheck.sh --gpu-host <new-ip>` 全绿 |

**关键不变量**：以上每一步都只动配置（`.env` / SG / DNS），不动 Dockerfile、不动 server.py、不动 docker-compose.yml；如果哪步发现需要改代码，意味着可移植性已被破坏，**应回 PR 修复后再迁**。

---

## 已知限制 / Phase 1+ 改进项

这些不是可移植性破坏，但记录在此让未来上云前评估：

1. **CosyVoice prompt wav 默认值**：当前 Dockerfile 把 `/opt/CosyVoice/asset/zero_shot_prompt.wav` 复制到 `/app/prompts/default_zh.wav`。换语言（en）时建议挂卷覆盖，或 Phase 3 客户端发 `start` 时显式带 `prompt_wav` 路径。云上挂载 S3/OSS 卷做 prompt 库即可。
2. **真正的流式 SenseVoice partial**：当前 partial 是周期性整段重跑（best-effort）；Phase 1 评估 `funasr-onnx` 流式封装。切换不影响 WS 协议，是 server.py 内部的黑箱替换。
3. **多卡显存隔离**：当前 `SENSEVOICE_DEVICE` / `COSYVOICE_DEVICE` 是字符串，单值；多 GPU 实例要做"每卡跑一份服务"的水平扩展时建议用 docker compose `--scale` 配 `device_ids: [...]`。这是"云上才需要"的扩展，不是 Phase 0.5 范围。
4. **模型权重外置**：5GB 模型每次重建容器都要重下载，云上按秒计费很心疼。建议把 `./models` 挂到持久卷（云盘 / S3FS / EFS），或者 bake 到自定义 base image。
