# CosyVoice 2 (FunAudioLLM) TTS 推理服务 — 多语流式 WebSocket
#
# CosyVoice **不是 pip 包**：必须从 GitHub 仓库 clone（含 git submodules）后把整个
# 项目目录加到 PYTHONPATH。模型权重通过 modelscope/huggingface_hub 在首次运行时
# 下载到 /models（外挂卷）。
#
# 基础镜像：与 sensevoice 服务保持一致（云可移植性原则 #1）。
FROM nvidia/cuda:12.4.0-runtime-ubuntu22.04

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# 系统依赖：除 sensevoice 那一套外多两个：
# - sox / libsox-dev：CosyVoice 上游 README 显式要求（torchaudio sox backend）
# - build-essential：requirements.txt 里有需要本地编译的依赖（如 pyworld、gdown 间接）
# - cmake / pkg-config：保险起见为某些 wheel fallback 编译留空间
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        software-properties-common \
        ca-certificates \
        curl \
        git \
        ffmpeg \
        libsndfile1 \
        sox \
        libsox-dev \
        build-essential \
        cmake \
        pkg-config \
    && add-apt-repository -y ppa:deadsnakes/ppa \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
        python3.11 \
        python3.11-venv \
        python3.11-dev \
        python3.11-distutils \
    && curl -sS https://bootstrap.pypa.io/get-pip.py | python3.11 \
    && ln -sf /usr/bin/python3.11 /usr/local/bin/python \
    && ln -sf /usr/bin/python3.11 /usr/local/bin/python3 \
    && rm -rf /var/lib/apt/lists/*

# PyTorch CUDA 12.8 wheel（与 sensevoice 一致）
# 2.7.1+cu128 包含 sm_120 (Blackwell) 编译目标，5070 Ti 等 RTX 50 系卡运行 GPU kernel
# 必需。早期 cu124 wheel 只到 sm_90，PyTorch 会警告并 fallback CPU。
RUN pip install --no-cache-dir \
        --index-url https://download.pytorch.org/whl/cu128 \
        torch==2.7.1 torchaudio==2.7.1

# Clone CosyVoice 仓库（含 submodules）。
# 锁 commit：CosyVoice 上游不打 PyPI tag；用 commit SHA 钉死，以便后续可复现 build
# 与定向升级。`main` 是浮动引用——会让重建拿到不同上游代码，违反复现性。
# 当前 SHA 来自 FunAudioLLM/CosyVoice main 分支（2026-04-30 验证通过）。
# 升级流程：本地 `git ls-remote https://github.com/FunAudioLLM/CosyVoice main` 取新
# SHA；改 ARG 默认值 + 验证 inference_zero_shot/inference_cross_lingual API 未变。
ARG COSYVOICE_REF=ace7c47f41bbd303aa6bf1ea80e6f9fbd595cd40
RUN git clone --recursive https://github.com/FunAudioLLM/CosyVoice.git /opt/CosyVoice \
    && cd /opt/CosyVoice \
    && git checkout "${COSYVOICE_REF}" \
    && git submodule update --init --recursive

# CosyVoice 的 requirements.txt 是 Conda 用的，里面挑出 pip-only 的。但实际官方流程
# 推荐 pip install -r。为了健壮性：先升级 pip 工具，然后装 requirements，但禁用
# 它里面的 torch（我们已经装好 CUDA wheel，避免被覆盖成 CPU 版）。
# 做法：先 grep 掉 torch/torchaudio 行再装。
#
# 过滤 requirements.txt 里几个会触发问题的包，下一行单独处理：
# - torch / torchaudio：上一行已装 cu124 wheel，避免 PyPI 索引覆盖。
# - openai-whisper：requirements 里钉的 20231117 锁了 triton<3，跟 torch 2.4.1 要求的
#   triton==3.0.0 互斥。注意：CosyVoice 2 frontend.py 在 module-level `import whisper`，
#   inference 路径必须有此模块。下一个 RUN 里 --no-deps 装回。
# - deepspeed：训练加速用。它 import 时调用 nvcc 检查 CUDA 版本，cuda:runtime base
#   image 没有 nvcc（只 devel 有）。装上会让 transformers/modelscope 链式失败。
#
# PIP_CONSTRAINT 钉死：
# - setuptools<70：保留 pkg_resources 给 wheel build 阶段（setuptools 78+ 已移除）。
# - torch==2.4.1 / torchaudio==2.4.1：防止 transitive deps 触发 pip backtracking。
RUN cd /opt/CosyVoice \
    && grep -Ev '^(torch|torchaudio|openai-whisper|deepspeed)([=<>]|$)' requirements.txt > /tmp/cosyvoice-reqs.txt \
    && printf 'setuptools<70\ntorch==2.7.1\ntorchaudio==2.7.1\n' > /tmp/build-constraints.txt \
    && PIP_CONSTRAINT=/tmp/build-constraints.txt pip install --no-cache-dir -r /tmp/cosyvoice-reqs.txt

# openai-whisper 单装：CosyVoice frontend module-level import 它，inference 必需。
# 用 20250625（最新）：早期版本（如 20231117）锁了 triton<3，跟我们 torch 2.4.1 要求的
# triton==3.0.0 互斥；20250625 已放宽到 triton>=2.0.0，兼容 triton 3。
# 必须带依赖装，否则 tiktoken / numba 等运行时依赖缺失。
# PIP_CONSTRAINT setuptools<70：openai-whisper 的 setup.py 在 build wheel 阶段 import
# pkg_resources，setuptools 78+ 已移除。
RUN printf 'setuptools<70\n' > /tmp/whisper-constraints.txt \
    && PIP_CONSTRAINT=/tmp/whisper-constraints.txt pip install --no-cache-dir openai-whisper==20250625

# 服务层依赖（与 sensevoice 同一 stack）
RUN pip install --no-cache-dir \
        modelscope==1.18.1 \
        "huggingface_hub>=0.24,<1.0" \
        websockets==12.0 \
        "fastapi>=0.110,<1.0" \
        "uvicorn[standard]>=0.27,<1.0" \
        prometheus-client==0.20.0

# CosyVoice 项目目录加到 PYTHONPATH，让 ``from cosyvoice.cli.cosyvoice import CosyVoice2``
# 可以 import 到。CosyVoice 的 third_party submodule（Matcha-TTS 等）也要在 path 上。
ENV PYTHONPATH="/opt/CosyVoice:/opt/CosyVoice/third_party/Matcha-TTS:${PYTHONPATH}" \
    MODEL_CACHE_DIR=/models \
    HF_HOME=/models/huggingface \
    HUGGINGFACE_HUB_CACHE=/models/huggingface \
    MODELSCOPE_CACHE=/models/modelscope \
    TORCH_HOME=/models/torch \
    PORT_WS=8001 \
    PORT_HTTP=8081 \
    LOG_LEVEL=INFO \
    MAX_CONCURRENT_SESSIONS=2 \
    COSYVOICE_MODEL_ID=iic/CosyVoice2-0.5B \
    COSYVOICE_MODEL_DIR=/models/cosyvoice/CosyVoice2-0.5B \
    COSYVOICE_DEVICE=cuda:0 \
    COSYVOICE_OUTPUT_SAMPLE_RATE=24000

WORKDIR /app
COPY server.py /app/server.py
# 默认 prompt 音频（zero-shot 需要一个 3-30s 的参考音）；用户可挂卷覆盖
# /opt/CosyVoice/asset/zero_shot_prompt.wav 是 upstream 自带的中文示例
RUN mkdir -p /models /var/log/vocalize /app/prompts \
    && cp /opt/CosyVoice/asset/zero_shot_prompt.wav /app/prompts/default_zh.wav 2>/dev/null \
       || echo "default prompt asset not present in this CosyVoice ref; user must mount /app/prompts"

EXPOSE 8001 8081

HEALTHCHECK --interval=30s --timeout=10s --start-period=600s --retries=3 \
    CMD curl -fsS http://localhost:${PORT_HTTP}/health || exit 1

CMD ["python", "/app/server.py"]
