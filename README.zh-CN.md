# VocalizeAI

> English version: [README.md](README.md)

VocalizeAI 是双语（中/英）AI 电话代理。v1 把它从只能订餐厅的 bot 改造成
通用电话任务引擎：用自然语言描述任何电话任务，AI 会自动规划槽位结构、
向你收集必需信息、然后跟商家通话 —— 必要时跨语言传译。

## 当前状态

**v1 已发布**：通用电话任务引擎、Web 控制台、树莓派编排器部署全部就绪。
后端 5 层 prompt 架构（task_planner / preflight / merchant_agent /
clarification_collector / relay）可处理任何电话任务 —— 餐厅订位、预约服务、
查询余额、状态查询等。OSS 镜像发布于
[github.com/DGPisces/VocalizeAI](https://github.com/DGPisces/VocalizeAI)，
协议 Apache 2.0。

## 快速开始

**前提条件：** Python 3.11+、Node 20+、git、curl。可选：`uv`（安装脚本会自动安装）。

```bash
# 1. 一键安装所有依赖
bash install/dev-install.sh

# 2. 编辑 .env，至少设置 OPENAI_API_KEY
#    （如果 .env 不存在，安装脚本已自动从 .env.example 复制）
$EDITOR .env

# 3. 启动后端
source .venv/bin/activate
uvicorn vocalize.main:app --host 127.0.0.1 --port 8000 --reload

# 4. 启动前端（另开一个终端）
cd frontend && npm run dev

# 5. 验证安装
bash scripts/smoke.sh
# 退出码 0 = 开发环境正常（从克隆到 smoke 通过 ≤15 分钟）
```

**确定性安装：** 激活 venv 后运行 `uv pip sync uv.lock`，可使用提交的锁文件做确定性 Python 依赖安装。

完整的 Mac/Linux 安装手册（含环境变量说明和故障排查），见
[docs/deploy/local.md](docs/deploy/local.md)。

## v1 — 通用电话代理（CLI）

```bash
# (in project venv)
export OPENAI_BASE_URL="https://api.deepseek.com"
export OPENAI_API_KEY="..."
python -m demos.phase5_universal_agent_cli
```

该 demo 在无界面模式下运行完整通用电话代理引擎：

1. 你用自然语言描述一个任务（"帮我订 Joy Sushi 今晚 7 点 4 个人的位子"）。
2. Layer 1（`task_planner`）输出一个 `TaskSchema` —— 要收集的槽位、
   readiness 判定标准、relay 翻译策略。
3. Layer 2（`preflight`）跟用户对话，直到所有高关键性槽位都填满。
4. Layer 3（`merchant_agent`）拨打并主导通话。
5. Layer 4–5（`clarification_collector`、`relay`）处理通话中追加澄清和
   跨语言翻译。

## 仓库结构

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
├── install/                   # 一键安装脚本
│   ├── dev-install.sh         # Mac/Linux 本地开发环境安装
│   └── pi-install.sh          # 树莓派生产部署安装
├── docs/                      # 架构文档、部署指南、发布记录
├── scripts/                   # smoke 测试和工具脚本
│   └── smoke.sh               # 安装后端到端验证脚本
├── pyproject.toml             # 后端依赖单一来源
├── uv.lock                    # Python 依赖固定锁文件
└── .env.example               # 环境变量模板（17 个 key）
```

## 自托管快速上手

### 非 localhost 部署必需环境变量

| 变量 | 用途 |
|------|------|
| `VOCALIZE_WS_BASE_URL` | 返回给客户端的 WebSocket 基地址（如 `wss://api.example.com`）；非 localhost 模式必填,防止 Host 头欺骗 |
| `VOCALIZE_CORS_ORIGINS` | 允许的 CORS 来源（逗号分隔）;非 localhost 模式**必填**(无默认值) |

完整环境变量清单（含 LLM、GPU 服务、前端构建变量）见 `.env.example`。

完整的树莓派生产部署手册，见 [docs/deploy/pi.md](docs/deploy/pi.md)。

### GPU 节点要求

SenseVoice（STT）和 CosyVoice（TTS）作为独立 GPU 服务运行，通过 Tailscale
与树莓派编排器连接。本地开发不需要 GPU（只需 LLM 路径即可运行）。GPU 节点配置见
[docs/deploy/pi.md](docs/deploy/pi.md)。

## 跑开发服务器

```bash
source .venv/bin/activate

# optional: configure GPU services so /health reports gpu_reachable=true
export GPU_HOST=100.x.y.z            # GPU 节点的 Tailscale IP
export SENSEVOICE_WS_PORT=8000       # STT 服务
export COSYVOICE_WS_PORT=8001        # TTS 服务

uvicorn vocalize.main:app --host 127.0.0.1 --port 8000 --reload
```

另开一个终端：

```bash
curl -s http://127.0.0.1:8000/health
# → {"ok": true, "gpu_reachable": true}

SESSION=$(curl -s -X POST http://127.0.0.1:8000/api/sessions | python3 -c \
  'import sys,json; print(json.load(sys.stdin)["session_id"])')

curl -s -X POST "http://127.0.0.1:8000/api/sessions/$SESSION/task" \
  -H 'Content-Type: application/json' \
  -d '{"task":"帮我订海底捞"}'

# brew install websocat（macOS）或 apt install websocat（Linux）
websocat ws://127.0.0.1:8000/ws/sessions/$SESSION
# → server emits state_update / transcript_update / readiness_change frames

# 或者运行完整 smoke 测试：
bash scripts/smoke.sh
```

系统架构说明（5 层对话流水线、TaskPhase 状态机、WS 帧目录、REST 接口），见
[docs/architecture.md](docs/architecture.md)。

## 跑 Web 控制台

终端 1：

```bash
source .venv/bin/activate
uvicorn vocalize.main:app --host 127.0.0.1 --port 8000 --reload
```

终端 2：

```bash
cd frontend
export NEXT_PUBLIC_VOCALIZE_API_BASE_URL=http://127.0.0.1:8000
npm run dev -- --hostname 127.0.0.1 --port 3000
```

打开 `http://127.0.0.1:3000`。

前端通过 `NEXT_PUBLIC_VOCALIZE_API_BASE_URL` 直接调 FastAPI，不走 Next.js 代理。
若后端将 `VOCALIZE_WS_BASE_URL` 配置在其他主机，也要相应设置
`NEXT_PUBLIC_VOCALIZE_WS_BASE_URL`。

## 贡献

如何提 issue、运行测试、遵守代码风格、提交贡献，见 [CONTRIBUTING.md](CONTRIBUTING.md)。
Issue 模板和 PR 模板位于 `.github/` 目录。

## 安全

漏洞上报渠道、威胁模型摘要和紧急回滚流程，见 [SECURITY.md](SECURITY.md)。

## 许可证

Apache 2.0 —— 见 [LICENSE](LICENSE)。
