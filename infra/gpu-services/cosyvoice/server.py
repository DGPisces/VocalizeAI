"""CosyVoice 2 TTS 服务 — WebSocket 流式合成 + HTTP 健康/指标端点。

云端可移植：纯 Python，配置全部 env，不依赖 Windows / WSL2 任何特性。

WebSocket 协议 (`/ws/synthesize`)
---------------------------------
客户端 → 服务端（JSON 文本帧）：
- ``{"event": "start", "session_id": "<opt>", "language": "zh"|"en"|...,
     "speed": 1.0, "prompt_wav": "<opt path inside container>",
     "prompt_text": "<opt>"}``
  开始一段合成会话；可指定参考声纹 wav（zero-shot 克隆）；不传走默认 prompt。
- ``{"event": "text", "text": "...", "language": "zh"|"en", "is_final_segment": bool}``
  追加一段文本进合成队列。``is_final_segment=True`` 提示模型当前句末——本服务实现里
  我们就在收到该帧后把内部 generator 关掉触发 flush。
- ``{"event": "stop"}`` 结束当前会话。

服务端 → 客户端：
- 二进制帧：PCM int16 LE，单声道，采样率 = ``COSYVOICE_OUTPUT_SAMPLE_RATE``（默认 24kHz）
- JSON 文本帧（仅控制信号 / 错误）：
  - ``{"event": "audio_start", "sample_rate": 24000, "encoding": "pcm_s16le"}``
  - ``{"event": "audio_end", "utterance_id": int, "text": "<合成的全文回显>"}``
  - ``{"error": "<msg>", "fatal": bool}``

设计取舍（best-effort，Phase 3 客户端再补）
-------------------------------------------
CosyVoice 上游 ``inference_zero_shot`` 的 "text-streaming" 模式要求 ``tts_text`` 必须
是真正的 ``typing.Generator`` 对象（``def ... yield ...`` 函数返回值）。上游用
``isinstance(text, typing.Generator)`` gate 流式分支；自定义 ``__iter__/__next__`` 的
Iterator 类不通过该检查（``typing.Generator`` 只匹配 generator function 产物 / generator
expression）。Gate 失败 fallthrough 到 ``text.strip()`` 即崩。
因此本服务用 ``queue + 真 generator function`` 把 async 事件桥接成 sync generator：
- ``_TextStreamBridge`` 持有 ``queue.Queue``；async 侧 ``push_text()`` / ``close()`` 投递
- ``_iter_bridge_text(bridge)`` 是真 generator function，喂给 inference 调用（满足
  ``isinstance(_, typing.Generator) is True``，触发 bistream 分支）
- 单独 thread 跑 ``inference_zero_shot``，generator 阻塞 ``queue.get()`` 等下一帧
- 推理产出的 audio chunk 通过另一个 ``asyncio.Queue`` 回 event loop，由 WS 写出

并发由 ``asyncio.Semaphore(MAX_CONCURRENT_SESSIONS)`` 控制；MAX 默认 2（CosyVoice2-0.5B
显存 ~6GB / inference，5070 Ti 16GB 同时跑 2 路安全）。

跨语：CosyVoice 提供 ``inference_cross_lingual``——传不带 prompt_text 的 wav，模型自动
跨语。本服务规则：
- ``language`` 与 prompt 隐含语言匹配 → ``inference_zero_shot``（带 prompt_text）
- ``language`` ≠ prompt 语言 → ``inference_cross_lingual``（不传 prompt_text）

TODO(phase-3)：客户端可在 ``start`` 帧里通过 ``prompt_lang`` 显式声明 prompt 语言；
当前实现简化处理为"如果没传 prompt_text 就走 cross_lingual"。

优雅停机、日志、Prometheus 指标设计与 sensevoice/server.py 对齐——这两个服务运维上同形态。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import queue
import signal
import sys
import threading
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, Iterator

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
# 配置
# ---------------------------------------------------------------------------
PORT_WS = int(os.getenv("PORT_WS", "8001"))
PORT_HTTP = int(os.getenv("PORT_HTTP", "8081"))
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
MAX_CONCURRENT_SESSIONS = int(os.getenv("MAX_CONCURRENT_SESSIONS", "2"))
COSYVOICE_MODEL_ID = os.getenv("COSYVOICE_MODEL_ID", "iic/CosyVoice2-0.5B")
COSYVOICE_MODEL_DIR = os.getenv(
    "COSYVOICE_MODEL_DIR", "/models/cosyvoice/CosyVoice2-0.5B"
)
COSYVOICE_DEVICE = os.getenv("COSYVOICE_DEVICE", "cuda:0")
COSYVOICE_OUTPUT_SAMPLE_RATE = int(os.getenv("COSYVOICE_OUTPUT_SAMPLE_RATE", "24000"))
DEFAULT_PROMPT_WAV = os.getenv("DEFAULT_PROMPT_WAV", "/app/prompts/default_zh.wav")
DEFAULT_PROMPT_TEXT = os.getenv(
    "DEFAULT_PROMPT_TEXT", "希望你以后能够做的比我还好呦。"
)
GRACEFUL_TIMEOUT_SEC = float(os.getenv("GRACEFUL_TIMEOUT_SEC", "60"))


# ---------------------------------------------------------------------------
# JSON 日志（与 sensevoice 同形态；保持运维一致）
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
    return logging.getLogger("cosyvoice")


log = _setup_logging()


# ---------------------------------------------------------------------------
# Prometheus 指标
# ---------------------------------------------------------------------------
SESSIONS_OPENED = Counter("cosyvoice_sessions_opened_total", "WS sessions opened")
SESSIONS_REJECTED = Counter(
    "cosyvoice_sessions_rejected_total",
    "WS sessions rejected (saturation / shutdown)",
    ["reason"],
)
SYNTH_TOTAL = Counter(
    "cosyvoice_syntheses_total", "Synthesis utterances", ["mode", "outcome"]
)
FIRST_AUDIO_LATENCY = Histogram(
    "cosyvoice_first_audio_latency_seconds",
    "Time from synthesize call to first audio chunk emitted",
    ["mode"],
    buckets=(0.1, 0.2, 0.4, 0.8, 1.5, 3.0, 6.0, 12.0),
)
# Phase 4 Wave 1 instrumentation (CONCERNS.md "Phase 3 Demo Findings" hyp #3):
# Leading-silence detection on the first emitted PCM chunk. CosyVoice 2 has
# been observed to emit ~hundreds of milliseconds of near-zero samples at the
# head of the first chunk, contributing to the instrumentation gap between
# server-side first_audio_latency and client-perceived t_first_audible. This
# histogram quantifies that gap so Wave 2 can validate any fix against it.
LEADING_SILENCE_MS = Histogram(
    "cosyvoice_first_chunk_leading_silence_ms",
    "Leading near-zero PCM samples at the head of the first emitted chunk",
    ["mode"],
    buckets=(0, 50, 100, 200, 400, 800, 1600),
)
TOTAL_SYNTH_LATENCY = Histogram(
    "cosyvoice_total_synthesis_latency_seconds",
    "End-to-end synthesis duration",
    ["mode"],
    buckets=(0.2, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0),
)
ACTIVE_SESSIONS = Gauge("cosyvoice_active_sessions", "Currently open WS sessions")
QUEUE_DEPTH = Gauge(
    "cosyvoice_queue_depth", "Synthesis requests waiting on the GPU semaphore"
)
GPU_MEM_BYTES = Gauge(
    "cosyvoice_gpu_memory_allocated_bytes", "torch.cuda.memory_allocated() snapshot"
)
AUDIO_BYTES_OUT = Counter(
    "cosyvoice_audio_bytes_total", "Total PCM bytes streamed to clients"
)


# ---------------------------------------------------------------------------
# 应用状态
# ---------------------------------------------------------------------------
@dataclass
class AppState:
    model: Any = None
    model_loaded: bool = False
    gpu_available: bool = False
    shutdown_event: asyncio.Event = field(default_factory=asyncio.Event)
    inference_sem: asyncio.Semaphore = field(
        default_factory=lambda: asyncio.Semaphore(MAX_CONCURRENT_SESSIONS)
    )
    active_session_count: int = 0
    queue_depth: int = 0
    servers: list[Any] = field(default_factory=list)
    sample_rate: int = COSYVOICE_OUTPUT_SAMPLE_RATE


state = AppState()


def _check_gpu() -> bool:
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


def _ensure_model_dir() -> str:
    """确保模型目录在 ``COSYVOICE_MODEL_DIR``；不存在则用 modelscope 下载。

    返回最终路径。这里不在镜像 build 阶段下，因为：
    1. 模型 ~5GB，包进 image 太大且无法多版本复用
    2. 用户挂卷 -v ./models:/models 后下载持久化，重启零成本

    fallback：如果 modelscope 下载失败（如内网受限），尝试 huggingface_hub 镜像。
    """
    if os.path.isdir(COSYVOICE_MODEL_DIR) and os.listdir(COSYVOICE_MODEL_DIR):
        log.info("model dir present; skipping download",
                 extra={"path": COSYVOICE_MODEL_DIR})
        return COSYVOICE_MODEL_DIR

    log.info("downloading model via modelscope",
             extra={"model_id": COSYVOICE_MODEL_ID, "dest": COSYVOICE_MODEL_DIR})
    try:
        from modelscope import snapshot_download

        snapshot_download(COSYVOICE_MODEL_ID, local_dir=COSYVOICE_MODEL_DIR)
        return COSYVOICE_MODEL_DIR
    except Exception as exc:
        log.warning("modelscope download failed; trying huggingface",
                    extra={"err": str(exc)})

    # HF fallback：FunAudioLLM 在 HF 上也镜像了
    try:
        from huggingface_hub import snapshot_download as hf_snapshot

        hf_id = COSYVOICE_MODEL_ID.replace("iic/", "FunAudioLLM/")
        hf_snapshot(hf_id, local_dir=COSYVOICE_MODEL_DIR)
        return COSYVOICE_MODEL_DIR
    except Exception as exc:
        log.exception("model download failed", extra={"err": str(exc)})
        raise


def _load_model() -> Any:
    """同步加载 CosyVoice2 模型（启动时一次）。

    CosyVoice 上游入口：``cosyvoice.cli.cosyvoice.CosyVoice2``。它接受 ``model_dir``
    指向已下载的本地路径。``load_jit`` / ``load_trt`` / ``fp16`` 等优化在 best-effort
    实现里先关掉，跑通再说；Phase 3 评估开启 fp16 看是否提速。
    """
    model_dir = _ensure_model_dir()

    log.info("loading CosyVoice2 model", extra={"path": model_dir})
    t0 = time.perf_counter()
    # 上游导出路径在不同 ref 下略有差异：CosyVoice2 直接走 cli.cosyvoice
    from cosyvoice.cli.cosyvoice import CosyVoice2

    model = CosyVoice2(
        model_dir,
        load_jit=False,
        load_trt=False,
        fp16=True,
    )
    log.info("CosyVoice2 model loaded", extra={
        "load_seconds": round(time.perf_counter() - t0, 2),
        "sample_rate": getattr(model, "sample_rate", COSYVOICE_OUTPUT_SAMPLE_RATE),
    })
    # 用模型暴露的 sample_rate 校准 state（万一与 env 不一致以模型为准，避免误传给客户端）
    actual_sr = int(getattr(model, "sample_rate", COSYVOICE_OUTPUT_SAMPLE_RATE))
    if actual_sr != state.sample_rate:
        log.warning("output sample rate mismatch; using model value",
                    extra={"env": state.sample_rate, "model": actual_sr})
        state.sample_rate = actual_sr

    # Phase 4 Wave 2 Fix #3: cache the default zero-shot speaker so subsequent
    # inference_zero_shot calls with zero_shot_spk_id="default" skip frontend
    # tensor extraction (load_wav x3 + speech_token + spk_embedding +
    # speech_feat). Per upstream cosyvoice/cli/cosyvoice.py:69-76 the API
    # precomputes & stores in self.frontend.spk2info[spk_id]; subsequent
    # inferences just spread the dict (frontend.py:166).
    #
    # Fault-tolerant: if the installed CosyVoice2 build doesn't expose
    # add_zero_shot_spk (older fork / refactor), we log + continue. The
    # branched _run_synth_thread call still works via the per-call fallback
    # path (passing the full prompt_text + prompt_wav).
    try:
        log.info("caching default zero-shot speaker",
                 extra={"prompt_text": DEFAULT_PROMPT_TEXT,
                        "prompt_wav": DEFAULT_PROMPT_WAV})
        if not os.path.isfile(DEFAULT_PROMPT_WAV):
            log.warning("default prompt wav missing; skipping speaker cache",
                        extra={"path": DEFAULT_PROMPT_WAV})
        elif not hasattr(model, "add_zero_shot_spk"):
            log.warning("model has no add_zero_shot_spk; skipping speaker cache")
        else:
            model.add_zero_shot_spk(
                DEFAULT_PROMPT_TEXT, DEFAULT_PROMPT_WAV, "default"
            )
            log.info("default speaker cached")
    except Exception as exc:
        log.exception("failed to cache default speaker; falling back to per-call",
                      extra={"err": str(exc)})
    return model


# ---------------------------------------------------------------------------
# 桥接 generator：把异步 WS 事件映射成 sync generator 喂给 CosyVoice 推理
# ---------------------------------------------------------------------------
_SENTINEL_END = object()


class _TextStreamBridge:
    """文本桥队列。``push_text()`` / ``close()`` 由 async WS 侧调用；同步侧通过
    ``_iter_bridge_text(bridge)`` 真 generator 函数消费队列。

    NOTE: 不直接实现 ``__iter__/__next__``。CosyVoice 上游
    (``cosyvoice/cli/frontend.py:text_normalize`` 与 ``cli/model.py:llm_job``) 用
    ``isinstance(text, typing.Generator)`` 判别流式输入，自定义 Iterator 类无法通过
    该检查；必须传真正的 generator 函数返回值。
    """

    def __init__(self) -> None:
        self._q: queue.Queue[Any] = queue.Queue()

    def push_text(self, text: str) -> None:
        self._q.put(text)

    def close(self) -> None:
        self._q.put(_SENTINEL_END)


def _iter_bridge_text(bridge: _TextStreamBridge) -> Iterator[str]:
    """真 generator function：阻塞 ``queue.get()`` 直到 close 哨兵。

    返回值是 ``typing.Generator`` 实例（``isinstance(_, typing.Generator) is True``），
    满足 CosyVoice 上游对流式文本输入的类型契约。
    """
    while True:
        item = bridge._q.get()
        if item is _SENTINEL_END:
            return
        assert isinstance(item, str)
        yield item


# ---------------------------------------------------------------------------
# 单次合成：在 worker thread 里跑 CosyVoice 推理，把音频 chunk 推回 asyncio.Queue
# ---------------------------------------------------------------------------
def _audio_tensor_to_pcm_bytes(t: Any) -> bytes:
    """torch.Tensor float32 → int16 LE bytes（mono）。

    CosyVoice 输出 ``tts_speech`` 是 shape (1, N) 或 (N,) 的 float32 [-1, 1]。
    """
    arr = t.detach().cpu().numpy() if hasattr(t, "detach") else np.asarray(t)
    if arr.ndim > 1:
        arr = arr.reshape(-1)
    arr = np.clip(arr, -1.0, 1.0)
    pcm = (arr * 32767.0).astype(np.int16)
    return pcm.tobytes()


def _run_synth_thread(
    bridge: _TextStreamBridge,
    audio_q: "asyncio.Queue[Any]",
    loop: asyncio.AbstractEventLoop,
    *,
    mode: str,
    prompt_wav: str,
    prompt_text: str,
    speed: float,
    session_id: str = "",
    utterance_id: int = 0,
) -> None:
    """同步 worker：跑 CosyVoice，把每个 chunk 通过 audio_q 投回 event loop。

    任一异常 → 把异常对象 put 进 audio_q；async 侧识别后回错误帧。
    完成后 put None 作为结束哨兵。

    Phase 4 Wave 1: ``session_id`` and ``utterance_id`` are passed in only for
    log context — they let the leading-silence log line be correlated with the
    matching audio_end frame on the client side without changing the worker's
    runtime behavior.
    """
    def _put(item: Any) -> None:
        # call_soon_threadsafe 不带 future；改用 run_coroutine_threadsafe + 完成后丢弃
        # 这里 audio_q.put 是 awaitable，要走 coroutine 路径
        fut = asyncio.run_coroutine_threadsafe(audio_q.put(item), loop)
        try:
            fut.result(timeout=10.0)
        except Exception:  # pragma: no cover - 防止 loop 已关闭时阻塞
            pass

    try:
        if mode == "cross_lingual":
            # 不带 prompt_text；inference_cross_lingual(tts_text_or_gen, prompt_wav)
            # 必须传真 generator (_iter_bridge_text)，不能直接传 bridge 实例 ——
            # CosyVoice 用 isinstance(text, typing.Generator) gate 流式分支。
            iterator = state.model.inference_cross_lingual(
                _iter_bridge_text(bridge),
                _load_prompt_wav(prompt_wav),
                stream=True,
                speed=speed,
            )
        else:
            # Phase 4 Wave 2 Fix #3: when prompt matches the cached default,
            # call inference_zero_shot with zero_shot_spk_id="default" to skip
            # frontend tensor extraction (~0.1 s warm savings per RESEARCH).
            if (
                prompt_wav == DEFAULT_PROMPT_WAV
                and prompt_text == DEFAULT_PROMPT_TEXT
            ):
                iterator = state.model.inference_zero_shot(
                    _iter_bridge_text(bridge),
                    "",
                    "",
                    zero_shot_spk_id="default",
                    stream=True,
                    speed=speed,
                )
            else:
                iterator = state.model.inference_zero_shot(
                    _iter_bridge_text(bridge),
                    prompt_text,
                    _load_prompt_wav(prompt_wav),
                    stream=True,
                    speed=speed,
                )
        first = True
        t0 = time.perf_counter()
        for output in iterator:
            chunk = output.get("tts_speech") if isinstance(output, dict) else None
            if chunk is None:
                continue
            pcm = _audio_tensor_to_pcm_bytes(chunk)
            if first:
                FIRST_AUDIO_LATENCY.labels(mode=mode).observe(time.perf_counter() - t0)
                # Phase 4 Wave 1: leading-silence probe (CONCERNS.md hyp #3).
                # Count near-zero int16 samples at the head of the first chunk
                # (|s| < 32 ≈ 0.1% of int16 max — covers DC bias / model
                # warmup noise without false positives on quiet speech).
                # Duration is computed against state.sample_rate (model-reported
                # SR set in _load_model — falls back to env default if missing).
                try:
                    samples = np.frombuffer(pcm, dtype=np.int16)
                    leading = 0
                    for s in samples:
                        if abs(int(s)) < 32:
                            leading += 1
                        else:
                            break
                    sr_hz = state.sample_rate or COSYVOICE_OUTPUT_SAMPLE_RATE
                    leading_silence_ms = (leading * 1000.0) / sr_hz
                    log.info(
                        "first_chunk_leading_silence",
                        extra={
                            "mode": mode,
                            "session_id": session_id,
                            "utterance_id": utterance_id,
                            "leading_samples": leading,
                            "leading_silence_ms": round(leading_silence_ms, 1),
                        },
                    )
                    LEADING_SILENCE_MS.labels(mode=mode).observe(leading_silence_ms)
                except Exception:  # pragma: no cover - probe must never abort synthesis
                    log.exception("leading-silence probe failed; continuing")
                first = False
            _put(pcm)
        TOTAL_SYNTH_LATENCY.labels(mode=mode).observe(time.perf_counter() - t0)
        SYNTH_TOTAL.labels(mode=mode, outcome="ok").inc()
    except Exception as exc:
        SYNTH_TOTAL.labels(mode=mode, outcome="error").inc()
        log.exception("synthesis worker failed", extra={"err": str(exc), "mode": mode})
        msg = str(exc).lower()
        if "out of memory" in msg or "cuda" in msg:
            try:
                import torch

                torch.cuda.empty_cache()
            except Exception:
                pass
        _put(exc)
    finally:
        _put(None)


_VERIFIED_PROMPT_PATHS: set[str] = set()


def _load_prompt_wav(path: str) -> str:
    """返回 prompt wav 文件路径（不预加载）。

    设计修正（2026-05-03）：早期版本预加载为 tensor 缓存以省 IO。但 CosyVoice
    upstream（pin ace7c47）的 frontend_zero_shot / frontend_cross_lingual
    会在 _extract_speech_token / _extract_spk_embedding /
    _extract_speech_feat 三处分别调用 cosyvoice.utils.file_utils.load_wav，
    内部直接 torchaudio.load(wav, backend='soundfile')——不接受 tensor，
    soundfile 会抛 TypeError("Invalid file: tensor([[...]])")。

    所以正确契约是：传**文件路径字符串**，让上游自己 torchaudio.load + resample。
    我们这里只做一次存在性验证，不做缓存（CosyVoice 内部会重复读，省不掉）。
    """
    if path in _VERIFIED_PROMPT_PATHS:
        return path
    if not os.path.isfile(path):
        raise FileNotFoundError(f"prompt wav not found: {path}")
    _VERIFIED_PROMPT_PATHS.add(path)
    return path


# ---------------------------------------------------------------------------
# WebSocket 处理
# ---------------------------------------------------------------------------
@dataclass
class Session:
    session_id: str
    language: str = "zh"
    prompt_wav: str = DEFAULT_PROMPT_WAV
    prompt_text: str = DEFAULT_PROMPT_TEXT
    speed: float = 1.0
    utterance_id: int = 0


async def _emit_json(ws: WebSocket, payload: dict[str, Any]) -> None:
    if ws.client_state != WebSocketState.CONNECTED:
        return
    await ws.send_text(json.dumps(payload, ensure_ascii=False))


async def _emit_bytes(ws: WebSocket, data: bytes) -> None:
    if ws.client_state != WebSocketState.CONNECTED:
        return
    AUDIO_BYTES_OUT.inc(len(data))
    await ws.send_bytes(data)


def _select_mode(sess: Session) -> str:
    """决定走 zero_shot 还是 cross_lingual。

    简化规则：
    - 有 prompt_text → zero_shot（要求 prompt_lang 与目标 language 同语种；客户端负责）
    - 无 prompt_text → cross_lingual（CosyVoice 自动跨语，prompt 只提供声纹）
    Phase 3 客户端可以更精细化。
    """
    return "zero_shot" if sess.prompt_text else "cross_lingual"


async def _run_synth_session(
    ws: WebSocket, sess: Session, bridge: _TextStreamBridge
) -> None:
    """协调 worker thread 与 WS：
    - 启 worker（在信号量保护下）
    - 监听 audio_q → 写 bytes / 错误处理
    - 退出条件：worker put None；或 ws 断开
    """
    audio_q: asyncio.Queue[Any] = asyncio.Queue(maxsize=64)
    loop = asyncio.get_running_loop()
    mode = _select_mode(sess)

    state.queue_depth += 1
    QUEUE_DEPTH.set(state.queue_depth)
    decremented = False
    try:
        async with state.inference_sem:
            state.queue_depth -= 1
            QUEUE_DEPTH.set(state.queue_depth)
            decremented = True

            await _emit_json(ws, {
                "event": "audio_start",
                "sample_rate": state.sample_rate,
                "encoding": "pcm_s16le",
                "channels": 1,
                "utterance_id": sess.utterance_id,
                "mode": mode,
            })

            worker = threading.Thread(
                target=_run_synth_thread,
                kwargs={
                    "bridge": bridge,
                    "audio_q": audio_q,
                    "loop": loop,
                    "mode": mode,
                    "prompt_wav": sess.prompt_wav,
                    "prompt_text": sess.prompt_text,
                    "speed": sess.speed,
                    # Phase 4 Wave 1: log context for leading-silence probe.
                    "session_id": sess.session_id,
                    "utterance_id": sess.utterance_id,
                },
                daemon=True,
            )
            worker.start()

            while True:
                item = await audio_q.get()
                if item is None:
                    break
                if isinstance(item, BaseException):
                    await _emit_json(ws, {
                        "error": f"synthesis failed: {item}",
                        "fatal": False,
                    })
                    break
                if isinstance(item, (bytes, bytearray)):
                    await _emit_bytes(ws, bytes(item))

            await _emit_json(ws, {
                "event": "audio_end",
                "utterance_id": sess.utterance_id,
            })
            sess.utterance_id += 1
            _update_gpu_metric()
    finally:
        if not decremented:
            # Cancelled / failed before semaphore acquire returned; ensure the
            # queue counter doesn't drift upward across the lifetime of the
            # process. Covers BaseException (CancelledError) which the bare
            # try/except above could not catch.
            state.queue_depth -= 1
            QUEUE_DEPTH.set(state.queue_depth)


# ---------------------------------------------------------------------------
# Phase 4 Wave 2 Fix #1: single-segment batch synthesis path
# ---------------------------------------------------------------------------
# Per .planning/debug/cosyvoice-ttft-too-slow.md: when DeepSeek emits a short
# reply (1-2 sentences, 20-30 chars) the bistream LLM stalls in
# "not enough text token" busy-loops because text arrives slower than the
# bistream interleave ratio (5 text → 15 speech tokens) demands. The entire
# LLM stream finishes before flow+hift can decode a single chunk → bistream
# degenerates to batch + adds bistream stall overhead. Bench delta vs true
# batch path: ~1.5 s warm.
#
# Solution: detect single-segment WS sessions (one event=="text" frame with
# is_final_segment=True and no prior text frames) and short-circuit to
# inference_zero_shot(text=str, stream=False). Multi-segment / streaming
# sessions still hit the existing _run_synth_session bistream path
# (preserved as fallback per Pitfall 5 in RESEARCH).
def _run_batch_synth_thread(
    text: str,
    audio_q: "asyncio.Queue[Any]",
    loop: asyncio.AbstractEventLoop,
    *,
    mode: str,
    prompt_wav: str,
    prompt_text: str,
    speed: float,
    session_id: str = "",
    utterance_id: int = 0,
) -> None:
    """同步 worker — Fix #1 batch path.

    与 ``_run_synth_thread`` 同形态（信号量 / 异常 / 哨兵 / leading-silence
    probe），区别在于：
    - ``text`` 是一段完整字符串（不是 generator）。CosyVoice 上游
      ``isinstance(text, typing.Generator)`` gate fail → 走 batch 分支。
    - ``stream=False`` 让上游一次返回所有 chunk（典型 1-2 个 chunk for
      短回复）；流式 stall 不会发生。
    - mode 标签后缀 ``_batch``，便于 Prometheus 区分两路。
    """
    def _put(item: Any) -> None:
        fut = asyncio.run_coroutine_threadsafe(audio_q.put(item), loop)
        try:
            fut.result(timeout=10.0)
        except Exception:  # pragma: no cover
            pass

    metric_mode = f"{mode}_batch"
    try:
        if mode == "cross_lingual":
            iterator = state.model.inference_cross_lingual(
                text,
                _load_prompt_wav(prompt_wav),
                stream=False,
                speed=speed,
            )
        else:
            # Same Fix #3 default-cache short-circuit as _run_synth_thread.
            if (
                prompt_wav == DEFAULT_PROMPT_WAV
                and prompt_text == DEFAULT_PROMPT_TEXT
            ):
                iterator = state.model.inference_zero_shot(
                    text,
                    "",
                    "",
                    zero_shot_spk_id="default",
                    stream=False,
                    speed=speed,
                )
            else:
                iterator = state.model.inference_zero_shot(
                    text,
                    prompt_text,
                    _load_prompt_wav(prompt_wav),
                    stream=False,
                    speed=speed,
                )
        first = True
        t0 = time.perf_counter()
        for output in iterator:
            chunk = output.get("tts_speech") if isinstance(output, dict) else None
            if chunk is None:
                continue
            pcm = _audio_tensor_to_pcm_bytes(chunk)
            if first:
                FIRST_AUDIO_LATENCY.labels(mode=metric_mode).observe(
                    time.perf_counter() - t0
                )
                # Phase 4 Wave 1 leading-silence probe (mirrors _run_synth_thread).
                # The batch path should produce ~zero leading silence (no
                # bistream stall lead-in) — this metric will validate that.
                try:
                    samples = np.frombuffer(pcm, dtype=np.int16)
                    leading = 0
                    for s in samples:
                        if abs(int(s)) < 32:
                            leading += 1
                        else:
                            break
                    sr_hz = state.sample_rate or COSYVOICE_OUTPUT_SAMPLE_RATE
                    leading_silence_ms = (leading * 1000.0) / sr_hz
                    log.info(
                        "first_chunk_leading_silence",
                        extra={
                            "mode": metric_mode,
                            "session_id": session_id,
                            "utterance_id": utterance_id,
                            "leading_samples": leading,
                            "leading_silence_ms": round(leading_silence_ms, 1),
                        },
                    )
                    LEADING_SILENCE_MS.labels(mode=metric_mode).observe(
                        leading_silence_ms
                    )
                except Exception:  # pragma: no cover
                    log.exception("leading-silence probe failed; continuing")
                first = False
            _put(pcm)
        TOTAL_SYNTH_LATENCY.labels(mode=metric_mode).observe(
            time.perf_counter() - t0
        )
        SYNTH_TOTAL.labels(mode=metric_mode, outcome="ok").inc()
    except Exception as exc:
        SYNTH_TOTAL.labels(mode=metric_mode, outcome="error").inc()
        log.exception("batch synthesis worker failed",
                      extra={"err": str(exc), "mode": metric_mode})
        msg = str(exc).lower()
        if "out of memory" in msg or "cuda" in msg:
            try:
                import torch

                torch.cuda.empty_cache()
            except Exception:
                pass
        _put(exc)
    finally:
        _put(None)


async def _run_batch_synth_session(
    ws: WebSocket, sess: Session, text: str
) -> None:
    """Sibling of ``_run_synth_session`` for the batch (single-segment) path.

    No bridge — ``text`` is the full utterance. Reuses the existing
    ``state.inference_sem`` + ``QUEUE_DEPTH`` accounting so concurrency caps
    are preserved (T-04-03 mitigation per plan threat model).
    """
    audio_q: asyncio.Queue[Any] = asyncio.Queue(maxsize=64)
    loop = asyncio.get_running_loop()
    mode = _select_mode(sess)

    state.queue_depth += 1
    QUEUE_DEPTH.set(state.queue_depth)
    decremented = False
    try:
        async with state.inference_sem:
            state.queue_depth -= 1
            QUEUE_DEPTH.set(state.queue_depth)
            decremented = True

            await _emit_json(ws, {
                "event": "audio_start",
                "sample_rate": state.sample_rate,
                "encoding": "pcm_s16le",
                "channels": 1,
                "utterance_id": sess.utterance_id,
                "mode": f"{mode}_batch",
            })

            worker = threading.Thread(
                target=_run_batch_synth_thread,
                kwargs={
                    "text": text,
                    "audio_q": audio_q,
                    "loop": loop,
                    "mode": mode,
                    "prompt_wav": sess.prompt_wav,
                    "prompt_text": sess.prompt_text,
                    "speed": sess.speed,
                    "session_id": sess.session_id,
                    "utterance_id": sess.utterance_id,
                },
                daemon=True,
            )
            worker.start()

            while True:
                item = await audio_q.get()
                if item is None:
                    break
                if isinstance(item, BaseException):
                    await _emit_json(ws, {
                        "error": f"synthesis failed: {item}",
                        "fatal": False,
                    })
                    break
                if isinstance(item, (bytes, bytearray)):
                    await _emit_bytes(ws, bytes(item))

            await _emit_json(ws, {
                "event": "audio_end",
                "utterance_id": sess.utterance_id,
            })
            sess.utterance_id += 1
            _update_gpu_metric()
    finally:
        if not decremented:
            state.queue_depth -= 1
            QUEUE_DEPTH.set(state.queue_depth)


async def _handle_ws(ws: WebSocket) -> None:
    if state.shutdown_event.is_set():
        SESSIONS_REJECTED.labels(reason="shutdown").inc()
        await ws.close(code=1013, reason="server shutting down")
        return
    if state.active_session_count >= MAX_CONCURRENT_SESSIONS * 2:
        SESSIONS_REJECTED.labels(reason="saturation").inc()
        await ws.close(code=1013, reason="server saturated")
        return

    await ws.accept()
    SESSIONS_OPENED.inc()
    state.active_session_count += 1
    ACTIVE_SESSIONS.set(state.active_session_count)
    sess = Session(session_id=str(uuid.uuid4()))
    log.info("ws session opened", extra={"session_id": sess.session_id})

    # 当前活动的合成 task / bridge；客户端可在一个 WS 内连续合成多句话
    current_task: asyncio.Task[None] | None = None
    current_bridge: _TextStreamBridge | None = None
    # Phase 4 Wave 2 Fix #1: per-utterance dispatch counters. Reset on each
    # event=="start". The first event=="text" frame consults these to choose
    # between batch path (single-segment short-circuit) and bistream path.
    text_frame_count_for_session = 0
    batch_path_decided = False
    started = False  # whether the client sent event=="start" yet

    try:
        while True:
            try:
                msg = await ws.receive()
            except WebSocketDisconnect:
                break

            if msg.get("type") != "websocket.receive":
                break

            if "text" not in msg or msg["text"] is None:
                await _emit_json(ws, {
                    "error": "binary frames not accepted by /ws/synthesize",
                    "fatal": False,
                })
                continue

            try:
                cmd = json.loads(msg["text"])
            except json.JSONDecodeError:
                await _emit_json(ws, {"error": "invalid JSON", "fatal": False})
                continue

            event = cmd.get("event")
            if event == "start":
                # 中止上一段合成（如果还在跑）；客户端要新开就要先 stop 上一段或我们这里强制
                if current_bridge is not None:
                    current_bridge.close()
                if current_task is not None and not current_task.done():
                    try:
                        await asyncio.wait_for(current_task, timeout=5)
                    except (TimeoutError, asyncio.TimeoutError):
                        log.warning("previous synth did not finish in time; cancelling")
                        current_task.cancel()
                        try:
                            await current_task
                        except (asyncio.CancelledError, Exception):
                            pass
                sess.language = str(cmd.get("language", "zh"))
                sess.speed = float(cmd.get("speed", 1.0))
                if "prompt_wav" in cmd and cmd["prompt_wav"]:
                    sess.prompt_wav = str(cmd["prompt_wav"])
                if "prompt_text" in cmd:
                    sess.prompt_text = str(cmd.get("prompt_text") or "")
                if cmd.get("session_id"):
                    sess.session_id = str(cmd["session_id"])

                # Fix #1: defer eager bridge/task creation. The first
                # event=="text" frame decides whether to take batch (single-
                # segment short-circuit) or bistream (lazy-init bridge) path.
                current_bridge = None
                current_task = None
                text_frame_count_for_session = 0
                batch_path_decided = False
                started = True
            elif event == "text":
                if not started:
                    await _emit_json(ws, {
                        "error": "must send {event:'start'} before text frames",
                        "fatal": False,
                    })
                    continue
                text = str(cmd.get("text", ""))
                is_final = bool(cmd.get("is_final_segment"))

                # Fix #1 short-path: first AND final text frame in a session
                # → batch synthesize (skip _TextStreamBridge bistream entirely).
                if (
                    not batch_path_decided
                    and is_final
                    and text_frame_count_for_session == 0
                    and current_bridge is None
                ):
                    batch_path_decided = True
                    current_task = asyncio.create_task(
                        _run_batch_synth_session(ws, sess, text)
                    )
                    try:
                        await asyncio.wait_for(current_task, timeout=120)
                    except (TimeoutError, asyncio.TimeoutError):
                        await _emit_json(ws, {
                            "error": "synthesis timeout",
                            "fatal": False,
                        })
                    current_task = None
                    continue

                # Otherwise: bistream path. Lazy-init bridge + task on first
                # text frame; subsequent text frames push into the bridge.
                batch_path_decided = True
                text_frame_count_for_session += 1
                if current_bridge is None:
                    current_bridge = _TextStreamBridge()
                    current_task = asyncio.create_task(
                        _run_synth_session(ws, sess, current_bridge)
                    )
                if text:
                    current_bridge.push_text(text)
                if is_final:
                    # 句末 → 关 generator 触发 inference flush
                    current_bridge.close()
                    current_bridge = None
                    if current_task is not None:
                        try:
                            await asyncio.wait_for(current_task, timeout=120)
                        except (TimeoutError, asyncio.TimeoutError):
                            await _emit_json(ws, {
                                "error": "synthesis timeout",
                                "fatal": False,
                            })
                        current_task = None
            elif event == "stop":
                if current_bridge is not None:
                    current_bridge.close()
                    current_bridge = None
                if current_task is not None:
                    try:
                        await asyncio.wait_for(current_task, timeout=30)
                    except (TimeoutError, asyncio.TimeoutError):
                        log.warning("synth did not drain on stop")
                    current_task = None
                break
            else:
                await _emit_json(ws, {
                    "error": f"unknown event: {event!r}",
                    "fatal": False,
                })
    except Exception as exc:
        log.exception("ws session crashed", extra={
            "session_id": sess.session_id, "err": str(exc),
        })
    finally:
        # 兜底：把还没结束的 worker 推到结束
        if current_bridge is not None:
            current_bridge.close()
        if current_task is not None and not current_task.done():
            try:
                await asyncio.wait_for(current_task, timeout=10)
            except (TimeoutError, asyncio.TimeoutError):
                current_task.cancel()
        state.active_session_count -= 1
        ACTIVE_SESSIONS.set(state.active_session_count)
        log.info("ws session closed", extra={"session_id": sess.session_id})
        if ws.client_state != WebSocketState.DISCONNECTED:
            try:
                await ws.close()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# FastAPI / lifespan
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    state.gpu_available = _check_gpu()
    if not state.gpu_available:
        log.warning("CUDA not available; CosyVoice will run on CPU (very slow)")
    try:
        state.model = await asyncio.to_thread(_load_model)
        state.model_loaded = True
    except Exception as exc:
        log.exception("model load failed", extra={"err": str(exc)})
        state.model_loaded = False

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
            pass

    yield

    log.info("shutdown initiated; draining sessions",
             extra={"active": state.active_session_count})
    deadline = time.monotonic() + GRACEFUL_TIMEOUT_SEC
    while state.active_session_count > 0 and time.monotonic() < deadline:
        await asyncio.sleep(0.5)
    if state.active_session_count > 0:
        log.warning("graceful timeout; forcing exit",
                    extra={"remaining": state.active_session_count})


app_ws = FastAPI(lifespan=lifespan)
app_http = FastAPI()


@app_http.get("/health")
async def health() -> JSONResponse:
    is_shutting = state.shutdown_event.is_set()
    ok = state.model_loaded and not is_shutting
    payload = {
        "status": "ok" if ok else "degraded",
        "model_loaded": state.model_loaded,
        "model_id": COSYVOICE_MODEL_ID,
        "gpu_available": state.gpu_available,
        "active_sessions": state.active_session_count,
        "queue_depth": state.queue_depth,
        "max_concurrent_sessions": MAX_CONCURRENT_SESSIONS,
        "shutting_down": is_shutting,
        "output_sample_rate": state.sample_rate,
        "output_encoding": "pcm_s16le",
        "output_channels": 1,
    }
    return JSONResponse(payload, status_code=200 if ok else 503)


@app_http.get("/metrics")
async def metrics() -> Response:
    _update_gpu_metric()
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app_ws.websocket("/ws/synthesize")
async def synthesize(ws: WebSocket) -> None:
    await _handle_ws(ws)


def main() -> None:
    config_http = uvicorn.Config(
        app_http,
        host="0.0.0.0",  # noqa: S104
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
    server_ws.install_signal_handlers = lambda: None  # type: ignore[method-assign]
    server_http.install_signal_handlers = lambda: None  # type: ignore[method-assign]

    async def _run() -> None:
        ws_task = asyncio.create_task(server_ws.serve())
        http_task = asyncio.create_task(server_http.serve())
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
