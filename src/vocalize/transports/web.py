"""WebUserTransport — bridges a per-session WebSocket to the AudioTransport
Protocol.

One instance is created per ``WS /ws/sessions/{id}`` connection in
``server/ws.py``. The same instance is shared between the user-pipeline and
merchant-pipeline of a single ``DialogueOrchestrator`` because at any moment
only one is active (spec §4.1 state machine).

Inbound path (client→server, "user" role for STT):
    server/ws.py decodes a binary frame into raw PCM, pushes it onto
    ``inbound_queue``. ``input_stream()`` async-yields PCM blocks for the
    STT consumer. Closing the queue (sentinel ``None``) ends the iterator.

Outbound path (server→client, "ai_to_user" or "ai_to_merchant" role for
TTS playback):
    ``output_stream(audio_iter)`` reads PCM bytes from ``audio_iter``, calls
    the injected ``outbound_send(role, pcm)`` for each block. Role-specific
    callers use ``output_stream_for_role`` so overlapping TTS turns cannot
    retag each other's chunks.

Half-duplex / barge-in (Phase 4 D-04):
    ``pause_outbound()`` and ``resume_outbound()`` are gates that ``output_stream``
    consults; while paused, blocks are dropped on the floor (the orchestrator
    is asking us to stop talking). This matches MicrophoneTransport's
    pause_outbound semantics — log + skip, not buffer.

Tasks 5/6/7 fill in the methods. This task lays the class shape only.
"""
from __future__ import annotations

import asyncio
import collections
import logging
import time
from collections.abc import AsyncIterator, Awaitable
from typing import Callable, Literal

import webrtcvad

from vocalize.transports.base import AudioEncoding

log = logging.getLogger(__name__)

DEFAULT_INPUT_SAMPLE_RATE = 16_000
DEFAULT_OUTPUT_SAMPLE_RATE = 24_000
VAD_FRAME_BYTES = 960  # 30 ms @ 16 kHz mono int16

OutboundSend = Callable[[Literal["ai_to_user", "ai_to_merchant"], bytes], Awaitable[None]]


class WebUserTransport:
    """``AudioTransport`` impl over a per-session WebSocket pair of queues.

    Construction parameters:
        inbound_queue: ``asyncio.Queue[bytes | None]`` — server/ws.py pushes
            decoded PCM blocks here; ``None`` is the EOF sentinel.
        outbound_send: async callable invoked once per outbound PCM block; it
            owns the role-tagged binary WS frame send (see
            ``encode_outbound_audio_chunk``).
        sample_rate: input PCM sample rate (default 16 kHz, matches STT
            client expectation).
    """

    sample_rate: int
    channels: int
    encoding: AudioEncoding

    def __init__(
        self,
        inbound_queue: asyncio.Queue,
        outbound_send: OutboundSend,
        sample_rate: int = DEFAULT_INPUT_SAMPLE_RATE,
    ) -> None:
        self._inbound: asyncio.Queue = inbound_queue
        self._outbound_send: OutboundSend = outbound_send
        self.sample_rate = sample_rate
        self.channels = 1
        self.encoding = "pcm_s16le"
        self._outbound_paused = False
        self._drop_inbound = False
        self._dropped_inbound_blocks = 0
        self._closed = False
        self._outbound_role: Literal["ai_to_user", "ai_to_merchant"] = "ai_to_user"
        self._vad = webrtcvad.Vad(mode=2)
        self._vad_buffer: collections.deque[bool] = collections.deque(maxlen=10)
        self._vad_state: Literal["NOTTRIGGERED", "TRIGGERED"] = "NOTTRIGGERED"
        self._vad_pcm_buffer = bytearray()
        self._on_eos: Callable[[], Awaitable[None]] | None = None
        self._last_speech_end_ts: float | None = None

    def set_outbound_role(self, role: Literal["ai_to_user", "ai_to_merchant"]) -> None:
        """Server/ws.py calls this before each TTS turn to set the role byte
        prefix used by ``output_stream``.
        """
        self._outbound_role = role

    @property
    def dropped_inbound_blocks(self) -> int:
        return self._dropped_inbound_blocks

    def set_drop_inbound(self, drop: bool) -> None:
        self._drop_inbound = drop

    def push_inbound(self, pcm: bytes) -> bool:
        if self._drop_inbound:
            self._dropped_inbound_blocks += 1
            return False
        self._inbound.put_nowait(pcm)
        return True

    # The four Protocol methods are stubbed for now; Tasks 5–7 implement them.
    async def input_stream(self) -> AsyncIterator[bytes]:
        """Yield inbound PCM blocks until ``None`` sentinel arrives.

        The queue is filled by ``server/ws.py`` for each binary WS frame
        decoded via ``decode_inbound_audio_chunk``. Empty bytes are dropped
        defensively.
        """
        while True:
            block = await self._inbound.get()
            if block is None:
                return
            if not block:
                continue
            await self._observe_vad(block)
            yield block

    async def _observe_vad(self, block: bytes) -> None:
        """Run client-side EOS detection over browser PCM blocks.

        Browser ScriptProcessor callbacks do not always align to 30 ms / 960
        byte VAD frames, so buffer and re-frame before calling webrtcvad.
        """
        self._vad_pcm_buffer.extend(block)
        while len(self._vad_pcm_buffer) >= VAD_FRAME_BYTES:
            frame = bytes(self._vad_pcm_buffer[:VAD_FRAME_BYTES])
            del self._vad_pcm_buffer[:VAD_FRAME_BYTES]
            is_voiced = self._vad.is_speech(frame, self.sample_rate)
            self._vad_buffer.append(is_voiced)

            if self._vad_state == "NOTTRIGGERED":
                if sum(self._vad_buffer) >= 9:
                    self._vad_state = "TRIGGERED"
            elif sum(1 for voiced in self._vad_buffer if not voiced) >= 9:
                self._vad_state = "NOTTRIGGERED"
                self._last_speech_end_ts = time.monotonic()
                if self._on_eos is not None:
                    await self._on_eos()
                self._vad_buffer.clear()

    def pop_speech_end_ts(self) -> float | None:
        ts = self._last_speech_end_ts
        self._last_speech_end_ts = None
        return ts

    def drain_inbound(self) -> int:
        """Drop currently buffered inbound PCM blocks and preserve EOF."""
        drained = 0
        while True:
            try:
                block = self._inbound.get_nowait()
            except asyncio.QueueEmpty:
                return drained
            if block is None:
                self._inbound.put_nowait(None)
                return drained
            if block:
                drained += 1

    async def output_stream_for_role(
        self,
        role: Literal["ai_to_user", "ai_to_merchant"],
        audio: AsyncIterator[bytes],
    ) -> None:
        """Forward each PCM block from ``audio`` to ``outbound_send`` tagged
        with ``role`` for this specific stream.

        While ``self._outbound_paused`` is True, blocks are dropped on the
        floor — this is the half-duplex / barge-in contract from
        ``MicrophoneTransport``.

        Returns only after ``audio`` is fully drained, mirroring
        ``MicrophoneTransport.output_stream`` (TTS sentence end must not be
        cut). Cancellation propagates naturally — the caller awaits this
        coroutine and asyncio cancellation interrupts the for-loop.
        """
        async for block in audio:
            if not block:
                continue
            if self._outbound_paused:
                continue
            await self._outbound_send(role, block)

    async def output_stream(self, audio: AsyncIterator[bytes]) -> None:
        """Forward each PCM block using the current legacy outbound role."""
        async for block in audio:
            if not block:
                continue
            if self._outbound_paused:
                continue
            await self._outbound_send(self._outbound_role, block)

    async def output_stream_force_for_role(
        self,
        role: Literal["ai_to_user", "ai_to_merchant"],
        audio: AsyncIterator[bytes],
    ) -> None:
        """Forward PCM blocks even while the normal outbound gate is paused.

        Clarification hold/filler lines are explicit TTS for the merchant,
        not merchant-agent chatter. They must still reach the speaker while
        the regular merchant pipeline is held.
        """
        async for block in audio:
            if not block:
                continue
            await self._outbound_send(role, block)

    async def output_stream_force(self, audio: AsyncIterator[bytes]) -> None:
        """Forward PCM blocks using the current legacy outbound role."""
        async for block in audio:
            if not block:
                continue
            await self._outbound_send(self._outbound_role, block)

    async def pause_outbound(self) -> None:
        """Half-duplex / barge-in gate — output_stream drops blocks while paused."""
        self._outbound_paused = True

    async def resume_outbound(self) -> None:
        self._outbound_paused = False

    async def close(self) -> None:
        """Push the EOF sentinel to the inbound queue so any pending
        ``input_stream()`` consumer unblocks. Idempotent: subsequent calls
        are no-ops.
        """
        if self._closed:
            return
        self._closed = True
        await self._inbound.put(None)


__all__ = ["WebUserTransport", "OutboundSend"]
