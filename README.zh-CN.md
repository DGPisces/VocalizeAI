# VocalizeAI

> English version: [README.md](README.md)

VocalizeAI 是 Mac-first 的 AI 电话任务助手。你描述要完成的电话任务，应用会收集
缺失信息，然后通过本地网页控制台完成任务流程，包括实时转写、澄清、人工接管、
挂断/结束、诊断和通话后复盘。

公开版 `v0.1.0` 的默认路径只有三步：安装 macOS artifact，配置
OpenAI-compatible LLM 接口，使用内置 macOS 原生语音 provider。STT 和 TTS 都通过
Vocalize Provider API 接入；高级用户以后可以替换语音服务，但普通用户不需要选择
任何语音模型。

## macOS 安装

从 GitHub Release 下载 release zip 和 `SHA256SUMS`，然后在你希望创建
`VocalizeAI/` 的文件夹里运行：

```bash
bash install/install.sh \
  --artifact VocalizeAI-0.1.0-macos-arm64.zip \
  --checksums SHA256SUMS
```

进入安装目录并启动：

```bash
cd VocalizeAI
./vocalize setup
./vocalize doctor
./vocalize start
```

`setup` 会询问：

- LLM base URL
- LLM API key
- LLM model
- 是否添加可选的全局 `vocalize` 命令
- `start` 是否自动打开浏览器

默认 macOS 安装不会让用户选择语音模型。VocalizeAI 会启动打包好的 macOS speech
helper，并通过 Provider API 与它通信。

## 更新或卸载

```bash
# 使用新 release artifact 更新，保留 config/logs/cache
./vocalize update --artifact ../VocalizeAI-0.1.1-macos-arm64.zip --checksums ../SHA256SUMS

# 删除本地安装和可选的全局 symlink
./vocalize uninstall
# 或
bash uninstall.sh
```

默认安装只写入当前文件夹下的 `VocalizeAI/`。它不会默认安装全局 Python 包、Node
包、launch agent、system service，也不会修改 shell 配置。可选全局命令只是一个可
移除 symlink，并记录在安装配置里。

## 功能范围

- 本地 `VocalizeAI/` 安装目录
- 普通用户只配置 LLM
- 内置 macOS 原生 STT/TTS helper
- 可扩展 Provider API，用于自定义 STT/TTS 服务
- 打包后端直接托管 React + Vite 控制台
- 创建任务、readiness、实时转写、澄清、人工接管、挂断/结束、诊断、设置和复盘
- 中文和英文界面

## Provider API

语音边界见 [docs/provider-api.md](docs/provider-api.md)。默认 macOS helper 和自定义
provider 使用同一套 API：

- health 和 capability discovery
- realtime STT partial/final transcript event
- streaming TTS event
- cancellation 和结构化错误

`v0.1.0` 的公开支持平台是 macOS。其他平台可以通过实现同一个 Provider API 扩展。

## 开发

源码开发仍然使用本地开发工具链。

```bash
bash install/dev-install.sh
$EDITOR .env
source .venv/bin/activate
uvicorn vocalize.main:app --host 127.0.0.1 --port 8000 --reload
```

另开一个终端：

```bash
cd frontend
npm ci
npm run dev -- --host 127.0.0.1 --port 3000
```

常用检查：

```bash
.venv/bin/python -m pytest
cd frontend && npm run lint && npm run build && npm test
bash -n install/install.sh install/uninstall.sh scripts/build-macos-release.sh
```

## 仓库结构

```text
VocalizeAI/
├── src/vocalize/              # 后端包和任务引擎
│   ├── providers/             # STT/TTS Provider API clients
│   ├── llm/                   # OpenAI-compatible streaming client
│   ├── dialogue/              # planner, preflight, merchant agent, relay
│   ├── server/                # FastAPI app 和 WebSocket frames
│   └── config.py              # env 和安装配置加载
├── macos/                     # macOS 原生语音 provider helper
├── frontend/                  # React + Vite 网页控制台
├── install/                   # artifact installer 和 uninstaller
├── packaging/                 # PyInstaller 打包配置
├── tools/                     # release 和 CI helper
├── tests/                     # pytest suite
├── docs/                      # provider、architecture、release docs
├── pyproject.toml             # 后端包元数据
├── uv.lock                    # Python 依赖锁文件
└── .env.example               # 开发配置模板
```

## 发布门槛

公开发布前，CI 必须通过后端、Provider API、macOS helper、前端、打包/安装和
public-tree audit。最终 artifact 还必须完成 macOS 签名/公证和人工 clean-install 测试。

## 许可证

Apache 2.0 — 见 [LICENSE](LICENSE)。
