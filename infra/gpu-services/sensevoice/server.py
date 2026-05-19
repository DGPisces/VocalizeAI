"""SenseVoice STT 服务 — WebSocket 流式 ASR + HTTP 健康/指标端点。

云端可移植：纯 Python，配置全部来自 env，不依赖 Windows / WSL2 任何特性。

WebSocket 协议 (`/ws/transcribe`)
-----------------------------------
客户端 → 服务端：
- 二进制帧：原始 PCM int16 LE，单声道，16kHz（由 env AUDIO_SAMPLE_RATE 暴露）
- 文本帧（JSON）：
  - ``{"event": "start", "session_id": "<opt>", "language": "auto"|"zh"|"en"|...}``
    会话开始；language 决定 SenseVoice 解码语言提示，默认 "auto"
  - ``{"event": "end_of_utterance"}`` 客户端 VAD 判定本句话说完，触发 final 推理
  - ``{"event": "stop"}`` 结束会话；服务端触发剩余 buffer 的最后一次 final 后关闭

服务端 → 客户端（JSON 文本帧）：
- ``{"text": "...", "is_final": bool, "confidence": float, "start_time": float,
     "end_time": float, "utterance_id": int, "language": "zh"|"en"|...}``
  其中 start_time/end_time 是相对会话开始的秒数。
- 错误：``{"error": "<msg>", "fatal": bool}``；fatal=True 时服务端会关闭连接。

设计取舍（best-effort，Phase 1 再补强）
---------------------------------------
SenseVoice native API 是非流式的——AutoModel.generate 会对一整段音频跑一次 forward。
真正的"低延迟流式 partial"需要 ONNX 块推理或 cache-mode（funasr paraformer-streaming
那一套），SenseVoiceSmall 上游目前没有 ready-to-use 的流式 API。本服务的取舍：

- final transcript：客户端发 ``end_of_utterance`` 时触发，对累积的 buffer 跑一次完整
  推理。准确率与离线一致。
- partial transcript：当 buffer 长度跨过 ``PARTIAL_INTERVAL_SEC`` 阈值（默认 1.5s）
  时跑一次 best-effort 推理，``is_final=False``。Phase 1 客户端可忽略 partial 直接
  靠 final，端到端延迟由 VAD 决定（典型 200-500ms）。
- partial 不参与 utterance_id 计数；同一 utterance 的所有 partial+final 共享同一
  ``utterance_id``。

TODO(phase-1)：评估 funasr-onnx 的 SenseVoiceSmall 流式包装；若延迟可接受则替换 partial
路径以拿到 token-level 增量输出。

并发与资源
----------
- 全局 ``asyncio.Semaphore(MAX_CONCURRENT_SESSIONS)`` 限制同时进行的 WS 会话数。
- 每次 inference 通过 ``asyncio.to_thread`` 调入 funasr（CPU/GPU 阻塞），避免阻塞 event loop。
- ``torch.cuda.OutOfMemoryError``：捕获 → 清空 cache → 回客户端 fatal error。

优雅停机
--------
SIGTERM → ``shutdown_event.set()``：
1. HTTP /health 返回 status="degraded"；编排器健康检查会移除流量
2. WS 服务拒绝新连接（直接 close 1013 try-again-later）
3. 已建立的 WS 会话被允许跑完当前 utterance 后正常关闭
4. 最长等待 ``GRACEFUL_TIMEOUT_SEC``（默认 60s），超时则强制退出

日志
----
JSON line 格式输出到 stdout：``{"ts":"...","level":"INFO","msg":"...","..."}``
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import sys
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import uvicorn
from fastapi import FastAPI, Response
from fastapi.responses import JSONResponse
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)
from starlette.websockets import WebSocket, WebSocketDisconnect, WebSocketState

# ---------------------------------------------------------------------------
# 配置（全部 env，无硬编码）
# ---------------------------------------------------------------------------
PORT_WS = int(os.getenv("PORT_WS", "8000"))
PORT_HTTP = int(os.getenv("PORT_HTTP", "8080"))
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
MAX_CONCURRENT_SESSIONS = int(os.getenv("MAX_CONCURRENT_SESSIONS", "4"))
SENSEVOICE_MODEL_ID = os.getenv("SENSEVOICE_MODEL_ID", "iic/SenseVoiceSmall")
SENSEVOICE_DEVICE = os.getenv("SENSEVOICE_DEVICE", "cuda:0")
AUDIO_SAMPLE_RATE = int(os.getenv("AUDIO_SAMPLE_RATE", "16000"))
PARTIAL_INTERVAL_SEC = float(os.getenv("PARTIAL_INTERVAL_SEC", "1.5"))
MAX_UTTERANCE_SEC = float(os.getenv("MAX_UTTERANCE_SEC", "30.0"))
GRACEFUL_TIMEOUT_SEC = float(os.getenv("GRACEFUL_TIMEOUT_SEC", "60"))


# ---------------------------------------------------------------------------
# 结构化 JSON 日志：写到 stdout，符合容器编排标准
# ---------------------------------------------------------------------------
class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S.%fZ"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        # 任何额外字段（如 session_id、duration_ms）通过 logger.info(..., extra={...}) 注入
        for k, v in record.__dict__.items():
            if k in payload or k.startswith("_") or k in (
                "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
                "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
                "created", "msecs", "relativeCreated", "thread", "threadName",
                "processName", "process", "message", "taskName",
            ):
                continue
            payload[k] = v
        return json.dumps(payload, ensure_ascii=False, default=str)


def _setup_logging() -> logging.Logger:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(LOG_LEVEL)
    return logging.getLogger("sensevoice")


log = _setup_logging()


# ---------------------------------------------------------------------------
# Prometheus 指标
# ---------------------------------------------------------------------------
SESSIONS_OPENED = Counter("sensevoice_sessions_opened_total", "WS sessions opened")
SESSIONS_REJECTED = Counter(
    "sensevoice_sessions_rejected_total",
    "WS sessions rejected (saturation / shutdown)",
    ["reason"],
)
INFERENCES_TOTAL = Counter(
    "sensevoice_inferences_total", "Inference calls", ["kind", "outcome"]
)
INFERENCE_LATENCY = Histogram(
    "sensevoice_inference_latency_seconds",
    "Inference latency (sec)",
    ["kind"],
    buckets=(0.05, 0.1, 0.2, 0.4, 0.8, 1.5, 3.0, 6.0),
)
ACTIVE_SESSIONS = Gauge("sensevoice_active_sessions", "Currently open WS sessions")
QUEUE_DEPTH = Gauge(
    "sensevoice_queue_depth", "Inference requests waiting on the GPU semaphore"
)
GPU_MEM_BYTES = Gauge(
    "sensevoice_gpu_memory_allocated_bytes", "torch.cuda.memory_allocated() snapshot"
)


# ---------------------------------------------------------------------------
# 模型与生命周期
# ---------------------------------------------------------------------------
@dataclass
class AppState:
    model: Any = None              # funasr.AutoModel；用 Any 因为 funasr 没暴露稳定类型
    model_loaded: bool = False
    gpu_available: bool = False
    shutdown_event: asyncio.Event = field(default_factory=asyncio.Event)
    inference_sem: asyncio.Semaphore = field(
        default_factory=lambda: asyncio.Semaphore(MAX_CONCURRENT_SESSIONS)
    )
    active_session_count: int = 0
    queue_depth: int = 0
    # 由 main() 注入；用于 SIGTERM handler 调用以触发 uvicorn graceful shutdown
    servers: list[Any] = field(default_factory=list)


state = AppState()


def _load_model() -> Any:
    """同步加载 SenseVoice 模型；只在启动调用一次。

    funasr.AutoModel(...) 内部会做 ModelScope/HF snapshot_download，首次约 ~1GB。
    设置 vad_model="fsmn-vad" 让 funasr 自带的 VAD 帮我们切长音频段（保险，
    本服务客户端协议自己也做了 end_of_utterance，但 fsmn-vad 兜底处理 30s+ 长 buffer）。
    """
    from funasr import AutoModel

    log.info("loading SenseVoice model", extra={
        "model_id": SENSEVOICE_MODEL_ID,
        "device": SENSEVOICE_DEVICE,
    })
    t0 = time.perf_counter()
    model = AutoModel(
        model=SENSEVOICE_MODEL_ID,
        vad_model="fsmn-vad",
        vad_kwargs={"max_single_segment_time": int(MAX_UTTERANCE_SEC * 1000)},
        device=SENSEVOICE_DEVICE,
        disable_update=True,
    )
    log.info("SenseVoice model loaded", extra={
        "model_id": SENSEVOICE_MODEL_ID,
        "load_seconds": round(time.perf_counter() - t0, 2),
    })
    return model


def _check_gpu() -> bool:
    """探测 torch.cuda；用 try/except 覆盖 CPU-only 镜像（开发场景）"""
    try:
        import torch

        return bool(torch.cuda.is_available())
    except Exception:
        return False


def _update_gpu_metric() -> None:
    try:
        import torch

        if torch.cuda.is_available():
            GPU_MEM_BYTES.set(float(torch.cuda.memory_allocated()))
    except Exception:
        pass


# ---------------------------------------------------------------------------
# 推理：funasr 返回 [{"key": ..., "text": "<|zh|><|EMO_NEUTRAL|>...实际文本..."}]
# 我们既要拿到检测到的语言（embedded tag），又要拿到 plain text。
# ---------------------------------------------------------------------------
LANG_TAG_RE = None  # 懒加载，import 不要花时间


def _parse_language_and_clean_text(raw_text: str) -> tuple[str | None, str]:
    """从 funasr 富标签输出里抽语言代码 + 干净文本。

    funasr 的 SenseVoice 输出形如：``<|zh|><|EMO_NEUTRAL|><|Speech|><|withitn|>实际文本``
    rich_transcription_postprocess 会去掉 tag。我们想保留语言信息，所以自己解析。
    """
    global LANG_TAG_RE
    if LANG_TAG_RE is None:
        import re
        LANG_TAG_RE = re.compile(r"<\|([^|]+)\|>")
    # 已知语言 tag 集（来自 SenseVoice 文档）
    lang_codes = {"zh", "en", "yue", "ja", "ko"}
    detected: str | None = None
    for m in LANG_TAG_RE.finditer(raw_text):
        tag = m.group(1).lower()
        if tag in lang_codes:
            detected = tag
            break
    # 去掉所有 <|...|> 标签
    clean = LANG_TAG_RE.sub("", raw_text).strip()
    return detected, clean


def _run_inference_sync(
    model: Any, audio_pcm_int16: np.ndarray, language_hint: str
) -> tuple[str | None, str]:
    """阻塞调 funasr；返回 (detected_language, plain_text)。

    funasr.AutoModel.generate 接受 numpy float32 或文件路径；我们传 float32 mono
    16kHz。语言 hint 走 ``language=`` 参数，"auto" 让模型自检。
    """
    audio_f32 = audio_pcm_int16.astype(np.float32) / 32768.0
    res = model.generate(
        input=audio_f32,
        cache={},
        language=language_hint or "auto",
        use_itn=True,
        batch_size_s=60,
    )
    if not res:
        return None, ""
    raw = res[0].get("text", "")
    return _parse_language_and_clean_text(raw)


async def _run_inference(
    audio_pcm_int16: np.ndarray, language_hint: str, kind: str
) -> tuple[str | None, str]:
    """获信号量 → 在线程里跑 funasr → 返回结果；记录 metrics。"""
    state.queue_depth += 1
    QUEUE_DEPTH.set(state.queue_depth)
    decremented = False
    try:
        async with state.inference_sem:
            state.queue_depth -= 1
            QUEUE_DEPTH.set(state.queue_depth)
            decremented = True
            t0 = time.perf_counter()
            try:
                lang, text = await asyncio.to_thread(
                    _run_inference_sync, state.model, audio_pcm_int16, language_hint
                )
                INFERENCES_TOTAL.labels(kind=kind, outcome="ok").inc()
                return lang, text
            except Exception as exc:
                INFERENCES_TOTAL.labels(kind=kind, outcome="error").inc()
                # GPU OOM：清显存让后续请求有机会恢复
                msg = str(exc).lower()
                if "out of memory" in msg or "cuda" in msg:
                    try:
                        import torch

                        torch.cuda.empty_cache()
                    except Exception:
                        pass
                log.error("inference failed", extra={"kind": kind, "err": str(exc)})
                raise
            finally:
                INFERENCE_LATENCY.labels(kind=kind).observe(time.perf_counter() - t0)
                _update_gpu_metric()
    finally:
        if not decremented:
            # Cancelled / failed before semaphore acquire returned. Cover
            # BaseException (asyncio.CancelledError) too — bare `except Exception`
            # would miss it and leak the counter forever.
            state.queue_depth -= 1
            QUEUE_DEPTH.set(state.queue_depth)


# ---------------------------------------------------------------------------
# WebSocket 处理
# ---------------------------------------------------------------------------
@dataclass
class Session:
    session_id: str
    language_hint: str = "auto"
    utterance_id: int = 0
    started_at: float = field(default_factory=time.monotonic)
    # 当前 utterance 累积 buffer：np.int16 一维数组拼接
    buffer: list[np.ndarray] = field(default_factory=list)
    buffer_samples: int = 0
    last_partial_at_samples: int = 0


def _utterance_window(sess: Session) -> tuple[float, float]:
    """返回当前 utterance 的 (start_time, end_time) 自会话开始秒数。"""
    end = time.monotonic() - sess.started_at
    duration = sess.buffer_samples / AUDIO_SAMPLE_RATE
    start = max(0.0, end - duration)
    return start, end


async def _emit(ws: WebSocket, payload: dict[str, Any]) -> None:
    if ws.client_state != WebSocketState.CONNECTED:
        return
    await ws.send_text(json.dumps(payload, ensure_ascii=False))


async def _emit_error(ws: WebSocket, msg: str, fatal: bool = False) -> None:
    await _emit(ws, {"error": msg, "fatal": fatal})


async def _flush_inference(
    ws: WebSocket, sess: Session, *, is_final: bool
) -> None:
    if sess.buffer_samples == 0:
        return
    audio = np.concatenate(sess.buffer) if len(sess.buffer) > 1 else sess.buffer[0]
    kind = "final" if is_final else "partial"
    try:
        lang, text = await _run_inference(audio, sess.language_hint, kind)
    except Exception as exc:
        await _emit_error(ws, f"inference failed: {exc}", fatal=False)
        return
    start_s, end_s = _utterance_window(sess)
    # confidence：funasr SenseVoice 当前未暴露 token-level 置信度；用占位 1.0
    # TODO(phase-1)：若切到 funasr-onnx 路径，可拿到真正的 logprob → 转 confidence
    await _emit(ws, {
        "text": text,
        "is_final": is_final,
        "confidence": 1.0,
        "start_time": round(start_s, 3),
        "end_time": round(end_s, 3),
        "utterance_id": sess.utterance_id,
        "language": lang,
    })
    if is_final:
        # 重置 buffer，递增 utterance_id
        sess.buffer.clear()
        sess.buffer_samples = 0
        sess.last_partial_at_samples = 0
        sess.utterance_id += 1
    else:
        sess.last_partial_at_samples = sess.buffer_samples


async def _handle_ws(ws: WebSocket) -> None:
    # 拒绝新连接：饱和或正在停机
    if state.shutdown_event.is_set():
        SESSIONS_REJECTED.labels(reason="shutdown").inc()
        await ws.close(code=1013, reason="server shutting down")
        return
    if state.active_session_count >= MAX_CONCURRENT_SESSIONS * 2:
        # 软上限：信号量限制 inference 并发，但 WS 连接数也设个上限避免资源耗尽
        SESSIONS_REJECTED.labels(reason="saturation").inc()
        await ws.close(code=1013, reason="server saturated")
        return

    await ws.accept()
    SESSIONS_OPENED.inc()
    state.active_session_count += 1
    ACTIVE_SESSIONS.set(state.active_session_count)
    sess = Session(session_id=str(uuid.uuid4()))
    log.info("ws session opened", extra={"session_id": sess.session_id})

    try:
        while True:
            try:
                msg = await ws.receive()
            except WebSocketDisconnect:
                break

            if msg.get("type") != "websocket.receive":
                # disconnect / close
                break

            if "text" in msg and msg["text"] is not None:
                # 控制帧（JSON）
                try:
                    cmd = json.loads(msg["text"])
                except json.JSONDecodeError:
                    await _emit_error(ws, "invalid JSON control frame")
                    continue
                event = cmd.get("event")
                if event == "start":
                    sess.language_hint = str(cmd.get("language", "auto"))
                    sid = cmd.get("session_id")
                    if sid:
                        sess.session_id = str(sid)
                elif event == "end_of_utterance":
                    await _flush_inference(ws, sess, is_final=True)
                elif event == "stop":
                    if sess.buffer_samples > 0:
                        await _flush_inference(ws, sess, is_final=True)
                    break
                else:
                    await _emit_error(ws, f"unknown event: {event!r}")
            elif "bytes" in msg and msg["bytes"] is not None:
                # 二进制 PCM 帧
                pcm = np.frombuffer(msg["bytes"], dtype=np.int16)
                if pcm.size == 0:
                    continue
                sess.buffer.append(pcm)
                sess.buffer_samples += pcm.size

                # 安全网：如果客户端没发 end_of_utterance 而 buffer 跨过 MAX，强制 flush
                if sess.buffer_samples >= int(MAX_UTTERANCE_SEC * AUDIO_SAMPLE_RATE):
                    await _flush_inference(ws, sess, is_final=True)
                    continue

                # 周期性 partial（自上次 partial 起，每 PARTIAL_INTERVAL_SEC 跑一次）
                advance = sess.buffer_samples - sess.last_partial_at_samples
                if advance >= int(PARTIAL_INTERVAL_SEC * AUDIO_SAMPLE_RATE):
                    # partial 不阻塞下一次接收：开 task；但要避免 partial 风暴，所以在
                    # 当前实现里直接 await（partial 间隔已是秒级，可接受的串行化）
                    await _flush_inference(ws, sess, is_final=False)
    except Exception as exc:
        log.exception("ws session crashed", extra={
            "session_id": sess.session_id, "err": str(exc),
        })
        try:
            await _emit_error(ws, "internal error", fatal=True)
        except Exception:
            pass
    finally:
        state.active_session_count -= 1
        ACTIVE_SESSIONS.set(state.active_session_count)
        log.info("ws session closed", extra={"session_id": sess.session_id})
        if ws.client_state != WebSocketState.DISCONNECTED:
            try:
                await ws.close()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# FastAPI 应用：lifespan 加载模型 + 注册信号 handler
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    state.gpu_available = _check_gpu()
    if not state.gpu_available:
        log.warning("CUDA not available; SenseVoice will run on CPU (slow)")
    try:
        state.model = await asyncio.to_thread(_load_model)
        state.model_loaded = True
    except Exception as exc:
        log.exception("model load failed", extra={"err": str(exc)})
        state.model_loaded = False
        # 让 /health 一直返 degraded；docker HEALTHCHECK 会标 unhealthy 触发重启

    # SIGTERM / SIGINT: 既触发 shutdown_event（拒绝新 WS、health 转 degraded），
    # 也要让 uvicorn 自己开始 graceful shutdown（停止 accept、等现有连接结束）
    def _handle_signal() -> None:
        if state.shutdown_event.is_set():
            return
        log.info("received signal; initiating graceful shutdown")
        state.shutdown_event.set()
        for srv in state.servers:
            srv.should_exit = True

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, _handle_signal)
        except NotImplementedError:
            # 仅 Windows / 非主线程会走到；我们的镜像是 Linux + main thread 不会触发
            pass

    yield

    # 关停阶段：等所有 active sessions 自然结束
    log.info("shutdown initiated; draining sessions",
             extra={"active": state.active_session_count})
    deadline = time.monotonic() + GRACEFUL_TIMEOUT_SEC
    while state.active_session_count > 0 and time.monotonic() < deadline:
        await asyncio.sleep(0.5)
    if state.active_session_count > 0:
        log.warning("graceful timeout; forcing exit",
                    extra={"remaining": state.active_session_count})


# 两个 FastAPI app 共享 state：
# - app_ws：监听 PORT_WS（默认 8000），暴露 /ws/transcribe；持有 lifespan（加载模型）
# - app_http：监听 PORT_HTTP（默认 8080），暴露 /health /metrics（无 lifespan，只读 state）
# 拆两端口是 plan 要求；逻辑层面共用一个进程一份模型一份 state。
app_ws = FastAPI(lifespan=lifespan)
app_http = FastAPI()


@app_http.get("/health")
async def health() -> JSONResponse:
    """健康端点：状态 + 模型/GPU + 队列深度。

    返回值约定：
    - status="ok"：模型已加载，未在停机
    - status="degraded"：模型未加载 或 正在停机（编排器应停发新流量）
    HTTP code：ok=200，degraded=503（Docker HEALTHCHECK 用此判定）
    """
    is_shutting = state.shutdown_event.is_set()
    ok = state.model_loaded and not is_shutting
    payload = {
        "status": "ok" if ok else "degraded",
        "model_loaded": state.model_loaded,
        "model_id": SENSEVOICE_MODEL_ID,
        "gpu_available": state.gpu_available,
        "active_sessions": state.active_session_count,
        "queue_depth": state.queue_depth,
        "max_concurrent_sessions": MAX_CONCURRENT_SESSIONS,
        "shutting_down": is_shutting,
        "audio_sample_rate": AUDIO_SAMPLE_RATE,
        "audio_encoding": "pcm_s16le",
    }
    return JSONResponse(payload, status_code=200 if ok else 503)


@app_http.get("/metrics")
async def metrics() -> Response:
    _update_gpu_metric()
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app_ws.websocket("/ws/transcribe")
async def transcribe(ws: WebSocket) -> None:
    await _handle_ws(ws)


# ---------------------------------------------------------------------------
# 入口：起两个 uvicorn server 共享 event loop。
# - PORT_WS（8000）跑 app_ws（lifespan 在此加载模型）
# - PORT_HTTP（8080）跑 app_http（健康/指标，立刻就绪不等模型加载）
#
# 这样 docker HEALTHCHECK 一启动就能拿到 503（model_loaded=False），加载完转 200。
# 信号：lifespan 在 app_ws 上注册 SIGTERM handler；app_http 跟随退出（gather 任一退出
# 即整体退）。
# ---------------------------------------------------------------------------
def main() -> None:
    config_http = uvicorn.Config(
        app_http,
        host="0.0.0.0",  # noqa: S104  容器内 bind all；宿主决定暴露面
        port=PORT_HTTP,
        log_config=None,
        log_level=LOG_LEVEL.lower(),
        access_log=False,
    )
    config_ws = uvicorn.Config(
        app_ws,
        host="0.0.0.0",  # noqa: S104
        port=PORT_WS,
        log_config=None,
        log_level=LOG_LEVEL.lower(),
        access_log=False,
        ws_ping_interval=20,
        ws_ping_timeout=20,
    )
    server_http = uvicorn.Server(config_http)
    server_ws = uvicorn.Server(config_ws)
    state.servers = [server_ws, server_http]

    # 关掉 uvicorn 自己的 signal handler 安装：我们在 lifespan 里统一接管
    server_ws.install_signal_handlers = lambda: None  # type: ignore[method-assign]
    server_http.install_signal_handlers = lambda: None  # type: ignore[method-assign]

    async def _run() -> None:
        ws_task = asyncio.create_task(server_ws.serve())
        http_task = asyncio.create_task(server_http.serve())
        # 任一 server 退出 → 标记另一 server 也退（保证整体进程退出）
        done, pending = await asyncio.wait(
            {ws_task, http_task}, return_when=asyncio.FIRST_COMPLETED
        )
        for srv in state.servers:
            srv.should_exit = True
        for t in pending:
            try:
                await t
            except Exception:
                log.exception("server task error during shutdown")

    asyncio.run(_run())


if __name__ == "__main__":
    main()
