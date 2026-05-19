# VocalizeAI GPU 推理节点

两个 Docker 服务编排在一起：

| 服务 | 模型 | WS 端口（默认） | HTTP 端口（默认） | 显存（每 session 估算） |
|---|---|---|---|---|
| `sensevoice` | `iic/SenseVoiceSmall` (FunAudioLLM) | 8000 | 8080 | ~1.5 GB |
| `cosyvoice` | `iic/CosyVoice2-0.5B` (FunAudioLLM) | 8001 | 8081 | ~6 GB |

设计原则：
- **纯 Linux 容器**（基础镜像 `nvidia/cuda:12.4.0-runtime-ubuntu22.04`），不依赖任何 Windows / WSL2 特性
- **配置全 env-driven**（见 `.env.example`），代码无硬编码路径
- **协议层完整**：WebSocket + 健康端点 + Prometheus metrics + 优雅停机 + 结构化 JSON 日志
- **同一份 docker-compose 跑 Windows 与云 GPU**——见下方 Section B

---

## Section A: Windows 主机部署

> 目标：拿到一台 Windows 11 + RTX 5070 Ti 的主机，按本节走完后，从 Mac/Pi 通过 Tailscale 直连两个服务。

### 前置硬件软件

- Windows 10/11（64-bit），管理员权限
- NVIDIA GPU 一块（5070 Ti / 4090 / 3090 等 12GB+ 显存推荐）
- ≥ 32 GB 系统内存（首次模型下载 + 编译会吃内存）
- ≥ 50 GB 可用磁盘（容器 image + 模型权重 ~10 GB；留 build cache 头寸）
- 稳定有线网络（首次下模型 5-8 GB，无线易断）

### 步骤 1：装 NVIDIA 驱动

到 [nvidia.com/Download](https://www.nvidia.com/Download/index.aspx) 选 RTX 5070 Ti + Windows 11，下 Game Ready 或 Studio 驱动。装完重启。

验证（PowerShell）：

```powershell
nvidia-smi
# 应输出 GPU 表格 + Driver Version + CUDA Version (>= 12.4)
```

> 如果 CUDA Version 显示 < 12.4，升级驱动到最新——CUDA runtime 12.4 需要驱动 ≥ 550.x。

### 步骤 2：启用 WSL2 + 装 Ubuntu 22.04

PowerShell 管理员：

```powershell
wsl --install -d Ubuntu-22.04
wsl --set-default-version 2
```

重启后，第一次启动 Ubuntu 22.04 会让你设 Linux 用户名 + 密码。

验证：

```powershell
wsl --list --verbose
# NAME            STATE           VERSION
# Ubuntu-22.04    Running         2
```

### 步骤 3：装 Docker Desktop（WSL2 backend）

到 [docker.com/products/docker-desktop](https://www.docker.com/products/docker-desktop/) 下 Windows 版，装。

装完打开 Docker Desktop → Settings：

1. **General** → 勾选 **Start Docker Desktop when you sign in to your computer**（开机自启）
2. **Resources → WSL Integration** → 启用 Ubuntu-22.04 的集成
3. **Resources → Advanced** → CPU/Memory 给 Docker 至少 16 GB（CosyVoice build 时编译吃内存）

应用并重启 Docker。

### 步骤 4：装 NVIDIA Container Toolkit（在 WSL2 Ubuntu 内）

打开 WSL2 终端（`wsl -d Ubuntu-22.04`）：

```bash
distribution=$(. /etc/os-release; echo $ID$VERSION_ID)
curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey \
  | sudo gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
curl -s -L https://nvidia.github.io/libnvidia-container/$distribution/libnvidia-container.list \
  | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' \
  | sudo tee /etc/apt/sources.list.d/nvidia-container-toolkit.list
sudo apt-get update
sudo apt-get install -y nvidia-container-toolkit
```

回 Windows，重启 Docker Desktop（任务栏右键 → Restart）。

### 步骤 5：验证 GPU passthrough

WSL2 Ubuntu 终端：

```bash
docker run --rm --gpus all nvidia/cuda:12.4.0-runtime-ubuntu22.04 nvidia-smi
```

应该看到与 Windows `nvidia-smi` 一致的 GPU 表格。**这一步必须通过才能继续**。

> 卡在这步的常见原因：(a) Docker Desktop WSL Integration 没启用 Ubuntu-22.04；(b) NVIDIA 驱动版本太老；(c) Docker Desktop 没重启。

### 步骤 6：装 Tailscale（在 Windows 宿主，不在 WSL2 里）

到 [tailscale.com/download/windows](https://tailscale.com/download/windows) 下载安装。登录到你的 tailnet，把这台机器命名为类似 `windows-gpu`。

确认从 Mac/Pi 上能 ping 通：

```bash
# 在 Mac 上
tailscale ip -4 windows-gpu  # 应输出 100.x.x.x
ping 100.x.x.x
```

> 为何不在 WSL2 里装 Tailscale？因为 WSL2 用的是 Hyper-V 创建的虚拟网卡，跨 NAT 后 Tailscale 直连体验差。装在 Windows 宿主上，Docker Desktop 默认会把容器端口转发到 Windows 主机的 0.0.0.0，自动经过 Tailscale。

### 步骤 7：clone 仓库到 WSL2 home（不要放 Windows 文件系统）

WSL2 Ubuntu 终端：

```bash
cd ~
git clone https://github.com/DGPisces/VocalizeAI.git
cd VocalizeAI/infra/gpu-services
```

> **不要** `cd /mnt/c/Users/...`！跨文件系统 IO 在 WSL2 上慢 5-10 倍，docker build 时会卡很久。

### 步骤 8：配置 .env

```bash
cp .env.example .env
nano .env  # 或用其他编辑器
```

按需调整：
- 共卡时 `SENSEVOICE_MAX_SESSIONS=4`、`COSYVOICE_MAX_SESSIONS=2`（默认值合 5070 Ti 16GB）
- 多卡可设 `SENSEVOICE_DEVICE=cuda:0`、`COSYVOICE_DEVICE=cuda:1` 分卡
- 公网风险敏感的话先保持 `GPU_HOST_BIND=0.0.0.0`，等 Tailscale 装好后改成 Tailscale 接口 IP

### 步骤 9：启动服务

```bash
docker compose up -d --build
docker compose logs -f
```

首次启动：
- `docker build` 5-15 分钟（apt + pip + git clone CosyVoice）
- 模型下载：SenseVoice ~1 GB（30-60 秒），CosyVoice2-0.5B ~5 GB（5-15 分钟，看网络）
- 模型加载到 GPU：30-90 秒

期间 `/health` 会返回 `503 status=degraded model_loaded=false`；加载完后转 `200 status=ok`。

### 步骤 10：从 Mac 跑 healthcheck

Mac 终端（仓库根目录）：

```bash
GPU_HOST=$(tailscale ip -4 windows-gpu) bash infra/gpu-services/healthcheck.sh
```

期望输出：

```
probing GPU host: 100.x.x.x

OK   sensevoice health: status=ok model_loaded=true
OK   sensevoice metrics: 80+ lines (Prometheus format)

OK   cosyvoice  health: status=ok model_loaded=true
OK   cosyvoice  metrics: 80+ lines (Prometheus format)

all probes green
```

### 步骤 11：禁用睡眠 + 自启

PowerShell 管理员：

```powershell
# AC（接电源）下永不待机
powercfg /change standby-timeout-ac 0
# AC 下永不休眠
powercfg /change hibernate-timeout-ac 0
# AC 下显示器 30 分钟可关（节能但不停止 GPU）
powercfg /change monitor-timeout-ac 30
```

Docker Desktop 已在步骤 3 设为开机自启；启动后 docker compose 中带 `restart: unless-stopped` 的服务会自动起。

### 步骤 12：故障排查

```bash
# 查容器状态
docker compose ps

# 查实时日志（JSON 格式，可 jq 过滤）
docker compose logs -f sensevoice | jq .
docker compose logs -f cosyvoice | jq .

# 查 GPU 占用
docker exec vocalize-sensevoice nvidia-smi
docker exec vocalize-cosyvoice nvidia-smi

# 重启单个服务
docker compose restart sensevoice

# 全量重建（依赖/Dockerfile 改了之后）
docker compose up -d --build --force-recreate

# 优雅停（90s grace period）
docker compose down
```

常见问题：
- **CUDA out of memory**：`SENSEVOICE_MAX_SESSIONS` / `COSYVOICE_MAX_SESSIONS` 调小
- **CosyVoice 启动 import 失败**：检查 `/opt/CosyVoice` 在容器里的 submodules 是否完整。重建：`docker compose build --no-cache cosyvoice`
- **CosyVoice build 时 pip install 失败**：上游 requirements.txt 经常引入需要本地编译的依赖；本镜像已装 `build-essential cmake`。如仍失败，检查具体哪个 pip 包，视情况固定版本

---

## Section B: 云 GPU 实例部署

同一份 `docker-compose.yml` 可直接搬到云。差异只在环境配置，**不需要改任何代码**。

### 推荐实例类型

| 云 | 实例族 | 卡 | 价格档位 | 备注 |
|---|---|---|---|---|
| **Aliyun** | GN6 (T4) / GN7 (V100/A100) / GN8I (H800) | T4 16GB / V100 32GB | $/小时 | 国内访问 ModelScope 快 |
| **AWS** | g5.xlarge / g5.2xlarge | A10G 24GB | $1-2/h | 全球可用 |
| **Lambda Labs** | gpu_1x_a10 / gpu_1x_a100 | A10 / A100 | $0.6-1.5/h | 性价比最高，按秒计费 |
| **RunPod** | 4090 / A6000 | 24-48GB | $0.4-0.8/h | 社区 GPU，体验最像本地 |

最低规格：单卡 16GB 显存（同时跑 sensevoice + cosyvoice 各 2 路并发）。

### 通用步骤

1. 起一台 Ubuntu 22.04 LTS 实例 + GPU
2. 装 NVIDIA driver + Docker + NVIDIA Container Toolkit（云镜像通常预装）
3. 装 Tailscale，加入同一 tailnet
   ```bash
   curl -fsSL https://tailscale.com/install.sh | sh
   sudo tailscale up
   ```
4. clone 仓库
   ```bash
   git clone https://github.com/DGPisces/VocalizeAI.git
   cd VocalizeAI/infra/gpu-services
   cp .env.example .env
   ```
5. 调整 `.env`：
   - `GPU_HOST_BIND=0.0.0.0`（云上由 SG/firewall 控制暴露）
   - 国内云（Aliyun/腾讯）建议保持 ModelScope 默认下模型；海外云用 HuggingFace fallback
6. `docker compose up -d --build`
7. Pi 上的应用 `.env` 改 `GPU_HOST=<云实例的 Tailscale 100.x 地址>`，`systemctl restart vocalize`
8. **零代码改动完成迁移**

### Aliyun GN6 特定提示

- 选 **CentOS 7.x** 镜像反而可能踩坑，强烈建议 **Ubuntu 22.04 LTS**
- 系统盘 ≥ 100 GB；模型 + image 共占 ~15 GB
- 第一次 `docker pull nvidia/cuda:...` 可能慢，配阿里 Docker 镜像加速器

### Lambda Labs / RunPod 特定提示

- 实例销毁后磁盘清空，所以模型权重要存到持久卷或 S3，下次启动 `aws s3 sync` 拉回 `./models`
- 或者：把模型权重 bake 到一个 base image 里（与本仓库 image 解耦），按需 build

详细迁移检查清单见 [`CLOUD_PORTABILITY.md`](./CLOUD_PORTABILITY.md)。

---

## 协议参考

### `/ws/transcribe` (SenseVoice)

```
client → server
  Binary frame: PCM int16 LE @ AUDIO_SAMPLE_RATE Hz, mono
  Text frame:
    {"event":"start","language":"auto","session_id":"<opt>"}
    {"event":"end_of_utterance"}    # 触发 final 推理
    {"event":"stop"}                # 关会话

server → client (text frames):
  {"text":"...","is_final":true,"confidence":1.0,
   "start_time":0.0,"end_time":1.5,"utterance_id":0,"language":"zh"}
  {"error":"...","fatal":false}
```

### `/ws/synthesize` (CosyVoice)

```
client → server (text frames only):
  {"event":"start","language":"zh","speed":1.0,
   "prompt_wav":"<opt>","prompt_text":"<opt>"}
  {"event":"text","text":"你好","language":"zh","is_final_segment":false}
  {"event":"text","text":"。","language":"zh","is_final_segment":true}
  {"event":"stop"}

server → client:
  Text:  {"event":"audio_start","sample_rate":24000,"encoding":"pcm_s16le","channels":1,...}
  Binary frames: PCM int16 LE @ sample_rate Hz, mono
  Text:  {"event":"audio_end","utterance_id":0}
  Text:  {"error":"...","fatal":false}
```

### `/health` 字段

两个服务结构一致：

```json
{
  "status": "ok" | "degraded",
  "model_loaded": true,
  "model_id": "iic/SenseVoiceSmall",
  "gpu_available": true,
  "active_sessions": 0,
  "queue_depth": 0,
  "max_concurrent_sessions": 4,
  "shutting_down": false
}
```

`status="ok"` ⇔ `model_loaded=true && shutting_down=false`，HTTP 200。否则 503。

### `/metrics` 关键指标

- `sensevoice_inference_latency_seconds_bucket{kind="final"}` / `{kind="partial"}` — 延迟分位
- `sensevoice_active_sessions` / `cosyvoice_active_sessions` — 当前 WS 数
- `sensevoice_queue_depth` / `cosyvoice_queue_depth` — 等信号量的请求数
- `sensevoice_gpu_memory_allocated_bytes` / `cosyvoice_gpu_memory_allocated_bytes` — GPU 显存
- `cosyvoice_first_audio_latency_seconds_bucket` — 首音延迟（核心 UX 指标）
