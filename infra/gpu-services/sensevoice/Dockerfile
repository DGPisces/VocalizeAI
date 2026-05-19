# SenseVoice (FunAudioLLM) STT 推理服务 — 多语流式 WebSocket
#
# 基础镜像：nvidia/cuda 12.4 runtime on Ubuntu 22.04（纯 Linux，无 Windows/WSL 依赖）
# 该镜像在 Windows WSL2 + NVIDIA Container Toolkit 下可跑，在 Aliyun GN6 / AWS g5 /
# Lambda Labs 等纯 Linux 云 GPU 实例上同样可跑——这是云端可移植性原则 #1。
FROM nvidia/cuda:12.4.0-runtime-ubuntu22.04

# 防止 apt 在 build 时阻塞交互式询问
ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# 系统依赖：
# - python3.11 + 工具链：funasr 与 torch wheel 都要 3.11
# - ffmpeg / libsndfile1：音频解码（funasr 的 example 输入会走 soundfile/torchaudio）
# - curl：HEALTHCHECK 用
# - git：modelscope/huggingface_hub 在 fallback 路径上可能 git-clone 模型仓库
# - ca-certificates：HTTPS 访问 ModelScope/HF
# 注意：版本固定到 22.04 默认源即可，不锁版本以便安全补丁滚动
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        software-properties-common \
        ca-certificates \
        curl \
        git \
        ffmpeg \
        libsndfile1 \
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

# PyTorch：必须从 CUDA 12.x index 装，否则会装到 CPU wheel——funasr 启动时会假装能跑
# 但 GPU device 拿不到，触发 `RuntimeError: CUDA not available`。
# torch 2.7.1+cu128：cu128 wheel 编译时包含 sm_120 (Blackwell)，5070 Ti 以及未来
# RTX 50 系卡需要。早期 cu124 wheel 只到 sm_90，会报 "CUDA capability sm_120 is not
# compatible" 警告并 fallback 到 CPU，inference 时严重降速或崩溃。
RUN pip install --no-cache-dir \
        --index-url https://download.pytorch.org/whl/cu128 \
        torch==2.7.1 torchaudio==2.7.1

# 应用依赖：
# - funasr：SenseVoice 的官方框架（modelscope/funasr）
# - modelscope：funasr 的默认模型 hub 客户端
# - websockets / fastapi / uvicorn：WS + HTTP 协议层
# - prometheus-client：/metrics 端点
# - numpy / scipy：funasr 间接依赖；显式列出避免不同 funasr 版本间的隐式漂移
RUN pip install --no-cache-dir \
        funasr==1.1.14 \
        modelscope==1.18.1 \
        "huggingface_hub>=0.24,<1.0" \
        websockets==12.0 \
        "fastapi>=0.110,<1.0" \
        "uvicorn[standard]>=0.27,<1.0" \
        prometheus-client==0.20.0 \
        "numpy>=1.26,<2.0" \
        "scipy>=1.12,<2.0"

# 模型缓存目录：所有 hub 客户端都重定向到 /models
# 容器外挂 -v ./models:/models 让模型只下一次，重启/重建 image 不重下
ENV MODEL_CACHE_DIR=/models \
    HF_HOME=/models/huggingface \
    HUGGINGFACE_HUB_CACHE=/models/huggingface \
    MODELSCOPE_CACHE=/models/modelscope \
    TORCH_HOME=/models/torch \
    PORT_WS=8000 \
    PORT_HTTP=8080 \
    LOG_LEVEL=INFO \
    MAX_CONCURRENT_SESSIONS=4 \
    SENSEVOICE_MODEL_ID=iic/SenseVoiceSmall \
    SENSEVOICE_DEVICE=cuda:0 \
    AUDIO_SAMPLE_RATE=16000

WORKDIR /app
COPY server.py /app/server.py

# /models 由宿主挂卷接管；如果没挂，启动时 funasr 会在容器内下载到此目录（容器删除后丢失）
RUN mkdir -p /models /var/log/vocalize

EXPOSE 8000 8080

# HEALTHCHECK：HTTP /health 必须 200；模型未加载完时返回 503，docker 会标 unhealthy
# start-period 给模型加载留 5 分钟（首次冷启会下 ~1GB 权重）
HEALTHCHECK --interval=30s --timeout=10s --start-period=300s --retries=3 \
    CMD curl -fsS http://localhost:${PORT_HTTP}/health || exit 1

# tini-style 信号转发：Python 进程作为 PID 1 接 SIGTERM；server.py 内 lifespan 处理 graceful drain
CMD ["python", "/app/server.py"]
