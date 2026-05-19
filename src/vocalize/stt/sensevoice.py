"""SenseVoice WebSocket 客户端 — 流式 STT (Phase 1)。

连远端 SenseVoice 推理服务（``infra/gpu-services/sensevoice/server.py``），
按其协议发送音频 + 控制帧，把服务端 partial/final 结果转换成 ``Transcript``。

协议要点（详见 server 模块 docstring）：
- 服务端 endpoint：``ws://<host>:<port>/ws/transcribe``
- 客户端 → 服务端：
  - 二进制：原始 PCM int16 LE，16 kHz mono
  - 文本（JSON）：``{"event":"start"|"end_of_utterance"|"stop", ...}``
- 服务端 → 客户端：JSON 文本帧，要么是 transcript 要么是 ``{"error":..., "fatal":bool}``

设计取舍：
- Phase 1 内部不做 VAD：调用方负责通过 ``end_of_utterance`` 控制 utterance 边界；
  若没有外部 VAD，本客户端会在音频流结束（AsyncIterator 耗尽）时自动发送一次
  ``end_of_utterance`` + ``stop``，让服务端 flush 最后的 final。
- 错误帧（``fatal=False``）转化为 yield 一条空 ``Transcript`` 并设 ``confidence=0``，
  让上层选择忽略或回退；``fatal=True`` 则抛 ``SenseVoiceError`` 终止流。
- cancellation：调用方对返回的 AsyncIterator 调 ``aclose()``，本客户端在 finally
  里发送 ``stop`` 并关闭 socket，避免 GPU 端继续占用。
"""
from __future__ import annotations

import asyncio
import json
import logging
import sys
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import websockets
from websockets.asyncio.client import ClientConnection, connect

from vocalize.config import Config
from vocalize.stt.base import Transcript

log = logging.getLogger(__name__)


class SenseVoiceError(RuntimeError):
    """Fatal 服务端错误（``error.fatal=True``）或协议级故障。"""


@dataclass
class SenseVoiceClient:
    """SenseVoice WebSocket 流式客户端。

    Args:
        host: GPU 节点主机名 / IP（来自 ``Config.gpu_host``）。
        port: WebSocket 端口（``Config.sensevoice_ws_port``，默认 8000）。
        path: WebSocket 路径，与服务端 endpoint 对齐。
        language_hint: ``"auto"`` / ``"zh"`` / ``"en"`` / ...；``auto`` 让模型自检。
        session_id: 可选会话 ID（透传给服务端，便于日志关联）。
        connect_timeout_s: TCP/WS 握手超时。
        open_timeout_s: ``websockets`` 库 open_timeout。
        ping_interval_s: 心跳间隔；与服务端 ``ws_ping_interval=20`` 对齐。
    """

    host: str
    port: int = 8000
    path: str = "/ws/transcribe"
    language_hint: str = "auto"
    session_id: str | None = None
    connect_timeout_s: float = 5.0
    open_timeout_s: float = 5.0
    ping_interval_s: float = 20.0
    # Phase 4 Plan 04-04: stamped the moment the client sends the
    # client-side VAD EOS frame ({"event": "end_of_utterance"}) over WS.
    # Pipeline reads this in TurnTiming.last_speech_end_real to bypass the
    # ~1.5s server-side fsmn-vad fallback latency.
    last_eos_wall_clock: float | None = field(default=None, init=False)

    @property
    def ws_url(self) -> str:
        return f"ws://{self.host}:{self.port}{self.path}"

    @classmethod
    def from_app_config(cls, cfg: Config) -> "SenseVoiceClient":
        """Build from global app config; require ``GPU_HOST``."""
        missing = cfg.validate_for_phase("gpu")
        if missing:
            raise SenseVoiceError(
                f"missing required env vars: {', '.join(missing)}"
            )
        return cls(
            host=cfg.gpu_host,
            port=cfg.sensevoice_ws_port,
            language_hint=cfg.default_language,
        )

    async def stream_transcribe(
        self, audio_chunks: AsyncIterator[bytes], *, transport: Any = None,
    ) -> AsyncIterator[Transcript]:
        """流式转写。

        发送顺序：``start`` → 二进制 PCM 帧 * N → ``end_of_utterance`` → ``stop``。
        服务端在 buffer 跨过 partial 阈值时主动推 partial，``end_of_utterance``
        / ``stop`` 触发 final。
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
            raise SenseVoiceError(
                f"failed to connect to {self.ws_url}: {exc}"
            ) from exc

        # Phase 4 Plan 04-04 — register client-side VAD EOS handler on the
        # transport (if it exposes the ``_on_eos`` slot). On a VAD-detected
        # 9-of-10 unvoiced ring, MicrophoneTransport's input_stream consumer
        # awaits this handler, which sends ``{"event": "end_of_utterance"}``
        # over WS. This preempts the ~1.5s server-side fsmn-vad finalize
        # fallback, closing the second-largest dominant latency gap from
        # CONCERNS.md.
        #
        # Race-tolerance: handler is registered AFTER ws-open but BEFORE we
        # iterate audio_chunks. MicrophoneTransport guards the call with
        # ``if self._on_eos is not None``, so any frames consumed before this
        # registration completes simply fall through to the normal yield path
        # (no crash). Once registered, the next TRIGGERED→NOTTRIGGERED
        # transition fires the handler.
        async def _handle_eos() -> None:
            self.last_eos_wall_clock = time.monotonic()
            try:
                await ws.send(json.dumps({"event": "end_of_utterance"}))
                log.debug("client VAD EOS sent over WS")
            except websockets.exceptions.ConnectionClosed:
                # Server closed before we could push EOS — sender path will
                # surface the error via the existing close-mid-stream gate.
                log.debug("EOS send dropped: ws already closed")

        if transport is not None and hasattr(transport, "_on_eos"):
            transport._on_eos = _handle_eos

        async for transcript in self._run_session(ws, audio_chunks):
            yield transcript

    async def _run_session(
        self, ws: ClientConnection, audio_chunks: AsyncIterator[bytes]
    ) -> AsyncIterator[Transcript]:
        """已建连的会话循环：起 sender task，主协程读响应。"""
        start_msg: dict[str, object] = {
            "event": "start",
            "language": self.language_hint,
        }
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
            self._send_audio(ws, audio_chunks, sender_done)
        )

        # 如果 sender 因异常提前结束（比如上游音频源 raise），主动关掉 ws，
        # 否则下面的 `async for raw in ws` 会永远等服务端不会到的帧。
        # （sender 正常完成时也会 schedule 关闭 → 服务端回应 ConnectionClosedOK，
        # 主接收 loop 自然退出。）
        def _close_ws_on_sender_failure(task: asyncio.Task[None]) -> None:
            nonlocal close_initiated_by_us
            if task.cancelled():
                return
            exc = task.exception()
            if exc is None:
                return
            # 调度 ws.close()；不能在 done callback 里 await
            close_initiated_by_us = True
            asyncio.create_task(_safe_close(ws))

        sender_task.add_done_callback(_close_ws_on_sender_failure)

        try:
            async for raw in ws:
                # ws 既可能 yield str 也可能 yield bytes；服务端只发文本
                if isinstance(raw, bytes):
                    raw = raw.decode("utf-8", errors="replace")
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    log.warning("ignoring non-JSON frame: %r", raw[:200])
                    continue

                if "error" in msg:
                    fatal = bool(msg.get("fatal"))
                    err_text = str(msg.get("error", "unknown server error"))
                    if fatal:
                        raise SenseVoiceError(err_text)
                    log.warning("sensevoice non-fatal error: %s", err_text)
                    continue

                if "text" in msg and "is_final" in msg:
                    yield _msg_to_transcript(msg)
                    # 单 utterance 模式：收到 final 后让 sender 结束并关闭
                    # 注意：流式模式下服务端可能继续推下一个 utterance 的 partial，
                    # 不能在这里 break。是否结束由音频流耗尽决定。

            # 受到 graceful close（code=1000/1001）时 websockets 的 async-for 迭代器
            # 静默退出而不抛异常。只在 sender 还未干净完成 AND 关闭不是我们自己发起的
            # 情况下才认定为 server-initiated close mid-stream 故障。
            # （close_initiated_by_us=True 意味着 sender 失败 → done-callback 触发了
            #   _safe_close；sender 的异常会在 finally 里通过 sender_exc 表达。）
            if not sender_done.is_set() and not close_initiated_by_us:
                raise SenseVoiceError(
                    "connection closed mid-stream: server closed before sender finished"
                )
        except websockets.exceptions.ConnectionClosed as exc:
            # 非 graceful close（code≠1000/1001）由此路径捕获。ConnectionClosedOK
            # 是 ConnectionClosed 的子类；在 async-for 之外调用 recv() 时才出现，
            # 此处作为防御性兜底，统一用 sender_done + close_initiated_by_us gate。
            if not sender_done.is_set() and not close_initiated_by_us:
                raise SenseVoiceError(
                    f"connection closed mid-stream: {exc}"
                ) from exc
        finally:
            # 必须永远 await sender_task 来回收它的异常，避免：
            #  1) "Task exception was never retrieved" warning;
            #  2) 上游音频源（麦克风/文件）失败被静默吞掉，调用方收不到任何信号，
            #     主接收 loop 还在等永远不会到的服务端帧。
            if not sender_task.done():
                sender_task.cancel()
            sender_exc: BaseException | None = None
            try:
                await sender_task
            except asyncio.CancelledError:
                # 我们自己 cancel 的清理路径，预期内
                pass
            except Exception as exc:
                sender_exc = exc

            try:
                await ws.close()
            except Exception:
                log.exception("error closing websocket")

            # sender 失败时只在没有 in-flight 异常的情况下抛出，否则会盖掉
            # finally 之外正在传播的真正错误（比如 fatal server error）。
            if sender_exc is not None:
                if sys.exc_info()[1] is None:
                    raise SenseVoiceError(
                        f"audio sender failed: {sender_exc}"
                    ) from sender_exc
                log.warning(
                    "sender_task also failed (suppressed in favor of in-flight "
                    "exception): %s", sender_exc,
                )

    async def _send_audio(
        self,
        ws: ClientConnection,
        audio_chunks: AsyncIterator[bytes],
        sender_done: asyncio.Event,
    ) -> None:
        """把上游音频 chunk 推到 ws，结束时发 ``end_of_utterance`` + ``stop``。

        ``sender_done`` 仅在 *正常* 完成路径（送完 stop 之后）置位；
        ``ConnectionClosed`` 等被 swallow 的失败路径下保持 unset，让接收侧把
        提前关连接判为故障并抛 ``SenseVoiceError``，而不是误判为 clean close。
        """
        sender_clean = False
        try:
            async for chunk in audio_chunks:
                if not chunk:
                    continue
                await ws.send(chunk)
            # 输入流自然结束 → flush 最后一个 utterance 并关闭会话
            await ws.send(json.dumps({"event": "end_of_utterance"}))
            await ws.send(json.dumps({"event": "stop"}))
            sender_clean = True
        except asyncio.CancelledError:
            # cancellation 路径：尽量发 stop，避免 GPU 端继续占用。
            # 不是 "clean close"，保持 sender_done unset。
            try:
                await ws.send(json.dumps({"event": "stop"}))
            except Exception:
                pass
            raise
        except websockets.exceptions.ConnectionClosed:
            # 服务端先关了；不 set sender_done，让接收侧（同样会看到
            # ConnectionClosed）抛 SenseVoiceError 而不是误判为 clean close。
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


def _msg_to_transcript(msg: dict[str, object]) -> Transcript:
    """服务端 transcript JSON → ``Transcript`` dataclass。"""
    return Transcript(
        text=str(msg.get("text", "")),
        is_final=bool(msg.get("is_final", False)),
        confidence=float(msg.get("confidence", 0.0) or 0.0),
        start_time=float(msg.get("start_time", 0.0) or 0.0),
        end_time=float(msg.get("end_time", 0.0) or 0.0),
        utterance_id=int(msg.get("utterance_id", 0) or 0),
        language=msg.get("language") if isinstance(msg.get("language"), str) else None,
    )
