"""CosyVoice2 WebSocket 客户端 — 流式 TTS (Phase 3)。

连远端 CosyVoice2 推理服务（``infra/gpu-services/cosyvoice/server.py``），把
``AsyncIterator[TextChunk]`` 翻译成服务端协议帧，并把服务端推回的 PCM 音频字节
原样作为 ``AsyncIterator[bytes]`` yield 给上层 transport。

协议要点（详见 server 模块 docstring）：
- 服务端 endpoint：``ws://<host>:<port>/ws/synthesize``
- 客户端 → 服务端（JSON 文本帧）：
  - ``{"event":"start","session_id":..,"language":..,"speed":..,
       "prompt_wav":<opt>,"prompt_text":<opt>}``
  - ``{"event":"text","text":..,"language":..,"is_final_segment":bool}`` * N
  - ``{"event":"stop"}``
- 服务端 → 客户端：
  - 二进制：PCM int16 LE，mono，sample_rate = ``audio_start.sample_rate``
  - JSON 文本：``audio_start`` / ``audio_end`` / ``{"error":..,"fatal":bool}``

设计取舍（与 ``stt.sensevoice`` 对齐）：
- 单 sender task 推 text 帧、主协程 receive 二进制 + JSON 控制帧。任一侧异常即收尾，
  避免 receive loop 永远等不会到的帧；sender 失败时通过 done-callback 主动关 ws。
- ``is_final_segment=True`` 直接转发给服务端：服务端用它触发 inference flush，
  这是流式 TTS 拿到完整尾音的硬性条件。
- ``output_sample_rate`` / ``output_encoding`` 是硬性客户端配置：服务端当前固定
  ``pcm_s16le`` @ 24 kHz（``infra/gpu-services/cosyvoice/server.py``）。若服务端
  ``audio_start`` 报告不一致只 log warning 不 mutate——下游 transport 已经按客户端
  配置开了 PortAudio output stream，运行时改 SR 会导致 pitch-shift。
- cancellation：caller 对返回的 AsyncIterator ``aclose()`` / ``break`` →
  ``finally`` 里 best-effort 发 ``stop`` + 关 socket。这是 Phase 5 barge-in 的硬性
  前置：用户打断时必须立刻让 GPU 端停止合成。
- 错误：``fatal=True`` → ``CosyVoiceError``；非 fatal → log + 继续（与 STT 对齐，
  让上层有机会忽略瞬时故障）。
"""
from __future__ import annotations

import asyncio
import json
import logging
import sys
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import websockets
from websockets.asyncio.client import ClientConnection, connect

from vocalize.config import Config
from vocalize.transports.base import AudioEncoding
from vocalize.tts.base import TextChunk

log = logging.getLogger(__name__)


# TODO(phase-4): introduce a common ``VoiceServiceError`` base in
# ``vocalize/errors.py`` so STT/LLM/TTS errors can be caught generically by
# pipeline glue. Currently ``SenseVoiceError``/``LLMServiceError``/
# ``CosyVoiceError`` are three independent ``RuntimeError`` subclasses with
# duplicated shape (message + optional ``upstream_status``).
class CosyVoiceError(RuntimeError):
    """Fatal 服务端错误（``error.fatal=True``）或协议级故障。

    ``upstream_status`` 镜像 ``LLMServiceError`` 的形状方便 pipeline 统一处理；
    CosyVoice 用 WS + JSON ``{error, fatal}`` 表达错误，没有 HTTP 状态码可填，
    所以当前所有调用点传 ``None``，字段为未来用（也方便测试断言）。
    """

    def __init__(self, message: str, upstream_status: int | None = None) -> None:
        super().__init__(message)
        self.upstream_status: int | None = upstream_status


@dataclass
class CosyVoiceClient:
    """CosyVoice2 WebSocket 流式客户端。

    实现 ``TTSService`` Protocol：``stream_synthesize`` + 暴露 ``output_sample_rate``
    / ``output_encoding`` 让上层 transport 知道字节流格式。

    Args:
        host: GPU 节点主机名 / IP（``Config.gpu_host``）。
        port: WebSocket 端口（``Config.cosyvoice_ws_port``，默认 8001）。
        path: WebSocket 路径，与服务端 endpoint 对齐。
        default_language: ``start`` 帧默认 language；后续 per-chunk 可覆盖。
        speed: 合成语速（CosyVoice 1.0=正常）。
        prompt_wav: 可选参考声纹 wav（容器内路径，由服务端解析）。
        prompt_text: 可选参考声纹对应的文本；与 ``prompt_wav`` 同属 zero-shot 克隆。
        session_id: 可选会话 ID（透传给服务端日志关联）。
        connect_timeout_s: TCP/WS 握手超时。
        open_timeout_s: ``websockets`` 库 open_timeout。
        ping_interval_s: 心跳间隔；与服务端 ``ws_ping_interval=20`` 对齐。
        output_sample_rate: 默认输出 SR；若服务端 ``audio_start`` 不一致则被覆盖。
        output_encoding: 默认输出编码；当前服务端固定 ``pcm_s16le``。
    """

    host: str
    port: int = 8001
    path: str = "/ws/synthesize"
    default_language: str = "zh"
    speed: float = 1.0
    prompt_wav: str | None = None
    prompt_text: str | None = None
    session_id: str | None = None
    connect_timeout_s: float = 5.0
    open_timeout_s: float = 5.0
    ping_interval_s: float = 20.0
    output_sample_rate: int = 24_000
    output_encoding: AudioEncoding = field(default="pcm_s16le")

    def __post_init__(self) -> None:
        if not (1 <= self.port <= 65535):
            raise CosyVoiceError(
                f"port must be in [1, 65535], got {self.port}"
            )
        if not self.path.startswith("/"):
            raise CosyVoiceError(
                f"path must start with '/', got {self.path!r}"
            )
        if self.speed <= 0:
            raise CosyVoiceError(f"speed must be > 0, got {self.speed}")
        if self.connect_timeout_s <= 0:
            raise CosyVoiceError(
                f"connect_timeout_s must be > 0, got {self.connect_timeout_s}"
            )
        if self.open_timeout_s <= 0:
            raise CosyVoiceError(
                f"open_timeout_s must be > 0, got {self.open_timeout_s}"
            )
        if self.ping_interval_s <= 0:
            raise CosyVoiceError(
                f"ping_interval_s must be > 0, got {self.ping_interval_s}"
            )
        if self.output_sample_rate <= 0:
            raise CosyVoiceError(
                f"output_sample_rate must be > 0, got {self.output_sample_rate}"
            )
        # zero-shot 声纹克隆需要 wav + 对应文本同时给出，缺一不可
        if (self.prompt_wav is None) != (self.prompt_text is None):
            raise CosyVoiceError(
                "prompt_wav and prompt_text must be set together "
                "(both or neither)"
            )

    @property
    def ws_url(self) -> str:
        return f"ws://{self.host}:{self.port}{self.path}"

    @classmethod
    def from_app_config(cls, cfg: Config) -> "CosyVoiceClient":
        """从全局 ``Config`` 构造；缺 ``GPU_HOST`` 时报错。"""
        missing = cfg.validate_for_phase("gpu")
        if missing:
            raise CosyVoiceError(
                f"missing required env vars: {', '.join(missing)}"
            )
        return cls(
            host=cfg.gpu_host,
            port=cfg.cosyvoice_ws_port,
            default_language=cfg.default_language,
        )

    async def stream_synthesize(
        self, text_chunks: AsyncIterator[TextChunk]
    ) -> AsyncIterator[bytes]:
        """流式合成。

        发送顺序：``start`` → ``text`` * N（每个 chunk 携带 language /
        is_final_segment）→ ``stop``。服务端在每个 ``is_final_segment=True``
        关 generator 触发 flush；二进制 PCM 帧按到达顺序透传。
        """
        try:
            ws = await asyncio.wait_for(
                connect(
                    self.ws_url,
                    open_timeout=self.open_timeout_s,
                    ping_interval=self.ping_interval_s,
                ),
                timeout=self.connect_timeout_s,
            )
        except (TimeoutError, OSError, websockets.exceptions.WebSocketException) as exc:
            raise CosyVoiceError(
                f"failed to connect to {self.ws_url}: {exc}"
            ) from exc

        async for audio in self._run_session(ws, text_chunks):
            yield audio

    async def health_check(self) -> bool:
        """轻量握手检查：能 connect + 立刻关闭即视为健康。

        与 SenseVoice 对齐：不发送 ``start`` 以免占用一个 GPU 推理 session。
        """
        try:
            ws = await asyncio.wait_for(
                connect(
                    self.ws_url,
                    open_timeout=self.open_timeout_s,
                    ping_interval=self.ping_interval_s,
                ),
                timeout=self.connect_timeout_s,
            )
        except (TimeoutError, OSError, websockets.exceptions.WebSocketException) as exc:
            log.warning("cosyvoice health_check failed: %s", exc)
            return False
        try:
            await ws.close()
        except Exception:  # pragma: no cover - defensive
            log.debug("error closing health-check ws", exc_info=True)
        return True

    async def _run_session(
        self,
        ws: ClientConnection,
        text_chunks: AsyncIterator[TextChunk],
    ) -> AsyncIterator[bytes]:
        """已建连的会话循环：起 sender task 推 text 帧，主协程读音频/控制帧。"""
        start_msg: dict[str, Any] = {
            "event": "start",
            "language": self.default_language,
            "speed": self.speed,
        }
        if self.prompt_wav is not None:
            start_msg["prompt_wav"] = self.prompt_wav
        if self.prompt_text is not None:
            start_msg["prompt_text"] = self.prompt_text
        if self.session_id is not None:
            start_msg["session_id"] = self.session_id
        await ws.send(json.dumps(start_msg))

        sender_done = asyncio.Event()
        # 标记是否是客户端侧主动发起关闭（sender 失败时 done-callback 触发）。
        # 为 True 时接收 loop 的正常退出不是"server-initiated graceful close mid-stream"
        # 而是客户端自己关掉 ws 的正常结果——后者的错误由 sender_exc 在 finally 里
        # 表达，不应再抛 "connection closed mid-stream"。
        close_initiated_by_us = False

        sender_task = asyncio.create_task(
            self._send_text(ws, text_chunks, sender_done)
        )

        # sender 异常提前结束 → 主动关 ws，避免接收 loop 永远等服务端帧。
        def _close_ws_on_sender_failure(task: asyncio.Task[None]) -> None:
            nonlocal close_initiated_by_us
            if task.cancelled():
                return
            exc = task.exception()
            if exc is None:
                return
            close_initiated_by_us = True
            asyncio.create_task(_safe_close(ws))

        sender_task.add_done_callback(_close_ws_on_sender_failure)

        try:
            async for raw in ws:
                if isinstance(raw, bytes):
                    # 二进制 = PCM 音频帧，原样透传
                    yield raw
                    continue

                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    log.warning("ignoring non-JSON text frame: %r", raw[:200])
                    continue

                if "error" in msg:
                    fatal = bool(msg.get("fatal"))
                    err_text = str(msg.get("error", "unknown server error"))
                    if fatal:
                        raise CosyVoiceError(err_text)
                    log.warning("cosyvoice non-fatal error: %s", err_text)
                    continue

                event = msg.get("event")
                if event == "audio_start":
                    # 不要在运行时 mutate output_sample_rate / output_encoding：
                    # 下游 transport 已经按客户端配置的 SR 打开了 PortAudio
                    # output stream，运行时改 SR 会导致 pitch-shift。服务端
                    # 当前固定 24 kHz pcm_s16le；不一致只 log warning，让客户端
                    # 配置说了算。Phase 4 若需要服务端动态选 SR，得在握手阶段
                    # 协商，不能在 audio_start 帧。
                    sr = msg.get("sample_rate")
                    if isinstance(sr, int) and sr > 0 and sr != self.output_sample_rate:
                        log.warning(
                            "server reports sample_rate=%d but client configured "
                            "%d; downstream transport may pitch-shift. "
                            "Trusting client config.",
                            sr, self.output_sample_rate,
                        )
                    enc = msg.get("encoding")
                    if isinstance(enc, str) and enc != self.output_encoding:
                        log.warning(
                            "server reports encoding=%r but client configured "
                            "%r; trusting client config.",
                            enc, self.output_encoding,
                        )
                # audio_end 当前不需要客户端动作；服务端会继续等下一个 text 段或 stop

            # 受到 graceful close（code=1000/1001）时 websockets 的 async-for 迭代器
            # 静默退出而不抛异常。只在 sender 还未干净完成 AND 关闭不是我们自己发起的
            # 情况下才认定为 server-initiated close mid-stream 故障。
            # （close_initiated_by_us=True 意味着 sender 失败 → done-callback 触发了
            #   _safe_close；sender 的异常会在 finally 里通过 sender_exc 表达。）
            if not sender_done.is_set() and not close_initiated_by_us:
                raise CosyVoiceError(
                    "connection closed mid-stream: server closed before sender finished"
                )
        except websockets.exceptions.ConnectionClosed as exc:
            # 非 graceful close（code≠1000/1001）由此路径捕获。ConnectionClosedOK
            # 是 ConnectionClosed 的子类；在 async-for 之外调用 recv() 时才出现，
            # 此处作为防御性兜底，统一用 sender_done + close_initiated_by_us gate。
            if not sender_done.is_set() and not close_initiated_by_us:
                raise CosyVoiceError(
                    f"connection closed mid-stream: {exc}"
                ) from exc
        finally:
            # 必须 await sender 回收异常（避免 "Task exception was never retrieved"
            # warning，并让上游 text iterator 失败时 caller 能感知）。
            if not sender_task.done():
                sender_task.cancel()
            sender_exc: BaseException | None = None
            try:
                await sender_task
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                sender_exc = exc

            try:
                await ws.close()
            except Exception:
                log.exception("error closing websocket")

            if sender_exc is not None:
                if sys.exc_info()[1] is None:
                    raise CosyVoiceError(
                        f"text sender failed: {sender_exc}"
                    ) from sender_exc
                log.warning(
                    "sender_task also failed (suppressed in favor of in-flight "
                    "exception): %s", sender_exc,
                )

    async def _send_text(
        self,
        ws: ClientConnection,
        text_chunks: AsyncIterator[TextChunk],
        sender_done: asyncio.Event,
    ) -> None:
        """把上游 ``TextChunk`` 推到 ws，结束时发 ``stop``。

        ``sender_done`` 仅在 *正常* 完成路径（送出 ``stop`` 之后）置位；
        ConnectionClosed 等被 swallow 的失败路径下保持 unset，让接收侧能把
        提前关连接判定为故障并抛 ``CosyVoiceError``，而不是误判为 clean close。
        """
        sender_clean = False
        try:
            async for chunk in text_chunks:
                # 空文本一般跳过，但 ``is_final_segment=True`` 的"哨兵 chunk"
                # 必须发出去——服务端用它触发 inference flush，不发就丢尾音。
                if not chunk.text and not chunk.is_final_segment:
                    continue
                await ws.send(json.dumps({
                    "event": "text",
                    "text": chunk.text,
                    "language": chunk.language,
                    "is_final_segment": chunk.is_final_segment,
                }, ensure_ascii=False))
            await ws.send(json.dumps({"event": "stop"}))
            sender_clean = True
        except asyncio.CancelledError:
            # cancellation 路径：尽量发 stop，让 GPU 端立刻停合成（barge-in）。
            # 注意：cancel 不算 "clean close from sender 视角"——保持 sender_done
            # unset 让接收侧也能感知。
            try:
                await ws.send(json.dumps({"event": "stop"}))
            except Exception:
                pass
            raise
        except websockets.exceptions.ConnectionClosed:
            # 服务端先关了；不 set sender_done，让接收侧（也会看到 ConnectionClosed）
            # 抛 CosyVoiceError 而不是当成正常 close。
            pass
        finally:
            if sender_clean:
                sender_done.set()


async def _safe_close(ws: ClientConnection) -> None:
    """Best-effort ws close used from a done-callback path."""
    try:
        await ws.close()
    except Exception:  # pragma: no cover - defensive
        log.debug("error in _safe_close", exc_info=True)
