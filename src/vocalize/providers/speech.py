"""Vocalize Provider API speech clients."""
from __future__ import annotations

import asyncio
import json
import logging
import sys
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse, urlunparse

import websockets
from websockets.asyncio.client import ClientConnection, connect

from vocalize.config import Config
from vocalize.errors import VoiceServiceError
from vocalize.stt.base import Transcript, TranscriptSegment
from vocalize.transports.base import AudioEncoding
from vocalize.tts.base import TextChunk

log = logging.getLogger(__name__)

PROVIDER_API_VERSION = "1.0"


class SpeechProviderError(VoiceServiceError):
    """Provider API connection, protocol, or fatal upstream error."""


def _ws_url(base_url: str, path: str) -> str:
    parsed = urlparse(base_url)
    if parsed.scheme not in {"http", "https", "ws", "wss"}:
        raise SpeechProviderError(
            "provider URL scheme must be http, https, ws, or wss"
        )
    scheme = {"http": "ws", "https": "wss"}.get(parsed.scheme, parsed.scheme)
    clean_path = "/" + path.lstrip("/")
    return urlunparse((scheme, parsed.netloc, clean_path, "", "", ""))


def _segment_from_payload(payload: dict[str, Any]) -> TranscriptSegment:
    return TranscriptSegment(
        text=str(payload.get("text", "")),
        language=str(payload.get("language", "")),
        start_time=float(payload.get("start_time", 0.0)),
        end_time=float(payload.get("end_time", 0.0)),
    )


def _transcript_from_payload(payload: dict[str, Any]) -> Transcript:
    segments_raw = payload.get("segments")
    segments = None
    if isinstance(segments_raw, list):
        segments = [
            _segment_from_payload(item)
            for item in segments_raw
            if isinstance(item, dict)
        ]
    language = payload.get("language")
    return Transcript(
        text=str(payload.get("text", "")),
        is_final=bool(payload.get("is_final")),
        confidence=float(payload.get("confidence", 0.0)),
        start_time=float(payload.get("start_time", 0.0)),
        end_time=float(payload.get("end_time", 0.0)),
        utterance_id=int(payload.get("utterance_id", 0)),
        language=str(language) if language is not None else None,
        segments=segments,
    )


async def _safe_close(ws: ClientConnection) -> None:
    try:
        await ws.close()
    except Exception:
        log.debug("provider websocket close failed", exc_info=True)


@dataclass
class ProviderSTTClient:
    """STT client for the Vocalize Provider API."""

    base_url: str
    path: str = "/v1/stt/stream"
    language_hint: str = "auto"
    session_id: str | None = None
    connect_timeout_s: float = 5.0
    open_timeout_s: float = 5.0
    ping_interval_s: float = 20.0
    last_eos_wall_clock: float | None = field(default=None, init=False)

    @classmethod
    def from_app_config(cls, cfg: Config) -> "ProviderSTTClient":
        return cls(
            base_url=cfg.stt_provider_url,
            language_hint=cfg.default_language,
            connect_timeout_s=cfg.provider_connect_timeout_s,
            open_timeout_s=cfg.provider_connect_timeout_s,
        )

    @property
    def ws_url(self) -> str:
        return _ws_url(self.base_url, self.path)

    async def stream_transcribe(
        self,
        audio_chunks: AsyncIterator[bytes],
        *,
        transport: Any = None,
    ) -> AsyncIterator[Transcript]:
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
            raise SpeechProviderError(
                f"failed to connect to STT provider {self.ws_url}: {exc}"
            ) from exc

        async def _handle_eos() -> None:
            self.last_eos_wall_clock = time.monotonic()
            try:
                await ws.send(json.dumps({"type": "end_of_utterance"}))
            except websockets.exceptions.ConnectionClosed:
                log.debug("provider EOS send dropped: ws already closed")

        if transport is not None and hasattr(transport, "_on_eos"):
            transport._on_eos = _handle_eos

        async for transcript in self._run_session(ws, audio_chunks):
            yield transcript

    async def _run_session(
        self,
        ws: ClientConnection,
        audio_chunks: AsyncIterator[bytes],
    ) -> AsyncIterator[Transcript]:
        start_msg: dict[str, object] = {
            "type": "start",
            "provider_api_version": PROVIDER_API_VERSION,
            "language": self.language_hint,
        }
        if self.session_id is not None:
            start_msg["session_id"] = self.session_id
        await ws.send(json.dumps(start_msg))

        sender_done = asyncio.Event()
        close_initiated_by_us = False
        sender_task = asyncio.create_task(
            self._send_audio(ws, audio_chunks, sender_done)
        )

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
                    continue
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    log.warning("ignoring non-JSON STT provider frame")
                    continue

                msg_type = msg.get("type")
                if msg_type == "error" or "error" in msg:
                    fatal = bool(msg.get("fatal", True))
                    message = str(msg.get("message") or msg.get("error") or "error")
                    if fatal:
                        raise SpeechProviderError(message)
                    log.warning("STT provider non-fatal error: %s", message)
                    continue
                if msg_type == "transcript":
                    yield _transcript_from_payload(msg)

            if not sender_done.is_set() and not close_initiated_by_us:
                raise SpeechProviderError(
                    "STT provider closed before audio sender finished"
                )
        except websockets.exceptions.ConnectionClosed as exc:
            if not sender_done.is_set() and not close_initiated_by_us:
                raise SpeechProviderError(
                    f"STT provider connection closed mid-stream: {exc}"
                ) from exc
        finally:
            if not sender_task.done():
                sender_task.cancel()
            sender_exc: BaseException | None = None
            try:
                await sender_task
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                sender_exc = exc

            await _safe_close(ws)

            if sender_exc is not None:
                if sys.exc_info()[1] is None:
                    raise SpeechProviderError(
                        f"STT provider audio sender failed: {sender_exc}"
                    ) from sender_exc
                log.warning("STT sender also failed: %s", sender_exc)

    async def _send_audio(
        self,
        ws: ClientConnection,
        audio_chunks: AsyncIterator[bytes],
        sender_done: asyncio.Event,
    ) -> None:
        try:
            async for chunk in audio_chunks:
                await ws.send(chunk)
            await ws.send(json.dumps({"type": "end_of_utterance"}))
            await ws.send(json.dumps({"type": "stop"}))
        finally:
            sender_done.set()


@dataclass
class ProviderTTSClient:
    """TTS client for the Vocalize Provider API."""

    base_url: str
    path: str = "/v1/tts/stream"
    default_language: str = "zh"
    session_id: str | None = None
    connect_timeout_s: float = 5.0
    open_timeout_s: float = 5.0
    ping_interval_s: float = 20.0
    output_sample_rate: int = 24_000
    output_encoding: AudioEncoding = field(default="pcm_s16le")

    @classmethod
    def from_app_config(cls, cfg: Config) -> "ProviderTTSClient":
        return cls(
            base_url=cfg.tts_provider_url,
            default_language=cfg.default_language,
            connect_timeout_s=cfg.provider_connect_timeout_s,
            open_timeout_s=cfg.provider_connect_timeout_s,
        )

    @property
    def ws_url(self) -> str:
        return _ws_url(self.base_url, self.path)

    async def stream_synthesize(
        self,
        text_chunks: AsyncIterator[TextChunk],
    ) -> AsyncIterator[bytes]:
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
            raise SpeechProviderError(
                f"failed to connect to TTS provider {self.ws_url}: {exc}"
            ) from exc

        async for audio in self._run_session(ws, text_chunks):
            yield audio

    async def health_check(self) -> bool:
        try:
            ws = await asyncio.wait_for(
                connect(
                    self.ws_url,
                    open_timeout=self.open_timeout_s,
                    ping_interval=self.ping_interval_s,
                ),
                timeout=self.connect_timeout_s,
            )
        except (TimeoutError, OSError, websockets.exceptions.WebSocketException):
            return False
        await _safe_close(ws)
        return True

    async def _run_session(
        self,
        ws: ClientConnection,
        text_chunks: AsyncIterator[TextChunk],
    ) -> AsyncIterator[bytes]:
        start_msg: dict[str, object] = {
            "type": "start",
            "provider_api_version": PROVIDER_API_VERSION,
            "language": self.default_language,
        }
        if self.session_id is not None:
            start_msg["session_id"] = self.session_id
        await ws.send(json.dumps(start_msg))

        sender_done = asyncio.Event()
        close_initiated_by_us = False
        sender_task = asyncio.create_task(
            self._send_text(ws, text_chunks, sender_done)
        )

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
                    yield raw
                    continue
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    log.warning("ignoring non-JSON TTS provider frame")
                    continue

                msg_type = msg.get("type")
                if msg_type == "error" or "error" in msg:
                    fatal = bool(msg.get("fatal", True))
                    message = str(msg.get("message") or msg.get("error") or "error")
                    if fatal:
                        raise SpeechProviderError(message)
                    log.warning("TTS provider non-fatal error: %s", message)
                    continue
                if msg_type == "audio_start":
                    self._warn_if_audio_mismatch(msg)
                    continue
                if msg_type == "audio_end":
                    continue

            if not sender_done.is_set() and not close_initiated_by_us:
                raise SpeechProviderError(
                    "TTS provider closed before text sender finished"
                )
        except websockets.exceptions.ConnectionClosed as exc:
            if not sender_done.is_set() and not close_initiated_by_us:
                raise SpeechProviderError(
                    f"TTS provider connection closed mid-stream: {exc}"
                ) from exc
        finally:
            if not sender_task.done():
                sender_task.cancel()
            sender_exc: BaseException | None = None
            try:
                await sender_task
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                sender_exc = exc

            await _safe_close(ws)

            if sender_exc is not None:
                if sys.exc_info()[1] is None:
                    raise SpeechProviderError(
                        f"TTS provider text sender failed: {sender_exc}"
                    ) from sender_exc
                log.warning("TTS sender also failed: %s", sender_exc)

    async def _send_text(
        self,
        ws: ClientConnection,
        text_chunks: AsyncIterator[TextChunk],
        sender_done: asyncio.Event,
    ) -> None:
        try:
            async for chunk in text_chunks:
                await ws.send(
                    json.dumps(
                        {
                            "type": "text",
                            "text": chunk.text,
                            "language": chunk.language,
                            "is_final_segment": chunk.is_final_segment,
                        }
                    )
                )
            await ws.send(json.dumps({"type": "stop"}))
        finally:
            sender_done.set()

    def _warn_if_audio_mismatch(self, msg: dict[str, Any]) -> None:
        sample_rate = msg.get("sample_rate")
        encoding = msg.get("encoding")
        if sample_rate is not None and int(sample_rate) != self.output_sample_rate:
            log.warning(
                "TTS provider sample_rate=%s differs from configured %s",
                sample_rate, self.output_sample_rate,
            )
        if encoding is not None and encoding != self.output_encoding:
            log.warning(
                "TTS provider encoding=%s differs from configured %s",
                encoding, self.output_encoding,
            )
