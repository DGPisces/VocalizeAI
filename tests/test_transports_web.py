"""WebUserTransport unit tests.

Each test drives the transport synthetically — no real WebSocket. The
transport's collaborators are an inbound audio queue (we push bytes) and an
outbound send callable (we record what is sent). This mirrors the shape that
``server/ws.py`` will compose at runtime.
"""
from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator

import pytest

from vocalize.transports.base import AudioTransport
from vocalize.transports.web import WebUserTransport


def test_protocol_conformance() -> None:
    """``WebUserTransport`` must satisfy ``runtime_checkable`` Protocol so
    ``DialogueOrchestrator.__init__`` accepts it without further adapters.
    """
    transport = WebUserTransport(
        inbound_queue=asyncio.Queue(),
        outbound_send=_noop_send,
    )
    assert isinstance(transport, AudioTransport)


def test_protocol_attributes_present() -> None:
    transport = WebUserTransport(
        inbound_queue=asyncio.Queue(),
        outbound_send=_noop_send,
    )
    assert transport.sample_rate == 16_000
    assert transport.channels == 1
    assert transport.encoding == "pcm_s16le"


async def _noop_send(role: str, pcm: bytes) -> None:
    return None


# -- Task 5: input_stream tests ------------------------------------------------


async def test_input_stream_yields_queued_pcm() -> None:
    queue: asyncio.Queue = asyncio.Queue()
    transport = WebUserTransport(inbound_queue=queue, outbound_send=_noop_send)
    await queue.put(b"\x00\x01\x02\x03")
    await queue.put(b"\x04\x05")
    await queue.put(None)  # EOF

    received: list[bytes] = []
    async for block in transport.input_stream():
        received.append(block)
    assert received == [b"\x00\x01\x02\x03", b"\x04\x05"]


async def test_input_stream_skips_empty_blocks() -> None:
    """Empty bytes are dropped silently — server/ws.py should never push
    them, but if a buggy client manages to we don't propagate junk to STT.
    """
    queue: asyncio.Queue = asyncio.Queue()
    transport = WebUserTransport(inbound_queue=queue, outbound_send=_noop_send)
    await queue.put(b"")
    await queue.put(b"\x01")
    await queue.put(None)

    received = [block async for block in transport.input_stream()]
    assert received == [b"\x01"]


async def test_input_stream_completes_on_eof_sentinel() -> None:
    queue: asyncio.Queue = asyncio.Queue()
    transport = WebUserTransport(inbound_queue=queue, outbound_send=_noop_send)
    await queue.put(None)

    received = [block async for block in transport.input_stream()]
    assert received == []


def test_push_inbound_queues_blocks_when_not_dropping() -> None:
    queue: asyncio.Queue = asyncio.Queue()
    transport = WebUserTransport(inbound_queue=queue, outbound_send=_noop_send)

    accepted = transport.push_inbound(b"\x01\x02")

    assert accepted is True
    assert queue.get_nowait() == b"\x01\x02"


def test_push_inbound_drops_blocks_without_queueing_when_enabled() -> None:
    queue: asyncio.Queue = asyncio.Queue()
    transport = WebUserTransport(inbound_queue=queue, outbound_send=_noop_send)
    transport.set_drop_inbound(True)

    accepted = transport.push_inbound(b"\x01\x02")

    assert accepted is False
    assert queue.empty()
    assert transport.dropped_inbound_blocks == 1


# -- Task 6: output_stream tests -----------------------------------------------


async def test_output_stream_sends_each_block_with_role() -> None:
    sent: list[tuple[str, bytes]] = []

    async def record(role: str, pcm: bytes) -> None:
        sent.append((role, pcm))

    transport = WebUserTransport(
        inbound_queue=asyncio.Queue(),
        outbound_send=record,
    )
    transport.set_outbound_role("ai_to_user")

    async def audio_gen() -> AsyncIterator[bytes]:
        yield b"\x10\x20"
        yield b"\x30\x40"

    await transport.output_stream(audio_gen())
    assert sent == [("ai_to_user", b"\x10\x20"), ("ai_to_user", b"\x30\x40")]


async def test_output_stream_uses_current_role_when_changed_mid_stream() -> None:
    """Role is read once per block — if the orchestrator switches role
    between blocks (rare, but the contract should be 'whatever role is set
    at send time'), later blocks pick up the new role.
    """
    sent: list[tuple[str, bytes]] = []

    async def record(role: str, pcm: bytes) -> None:
        sent.append((role, pcm))

    transport = WebUserTransport(
        inbound_queue=asyncio.Queue(),
        outbound_send=record,
    )
    transport.set_outbound_role("ai_to_user")

    async def audio_gen() -> AsyncIterator[bytes]:
        yield b"\x01"
        transport.set_outbound_role("ai_to_merchant")
        yield b"\x02"

    await transport.output_stream(audio_gen())
    assert sent == [("ai_to_user", b"\x01"), ("ai_to_merchant", b"\x02")]


async def test_output_stream_drops_empty_blocks() -> None:
    sent: list[tuple[str, bytes]] = []

    async def record(role: str, pcm: bytes) -> None:
        sent.append((role, pcm))

    transport = WebUserTransport(
        inbound_queue=asyncio.Queue(),
        outbound_send=record,
    )
    transport.set_outbound_role("ai_to_user")

    async def audio_gen() -> AsyncIterator[bytes]:
        yield b""
        yield b"\xff"

    await transport.output_stream(audio_gen())
    assert sent == [("ai_to_user", b"\xff")]


# -- Task 7: pause / resume / close tests -------------------------------------


async def test_pause_outbound_drops_blocks() -> None:
    sent: list[tuple[str, bytes]] = []

    async def record(role: str, pcm: bytes) -> None:
        sent.append((role, pcm))

    transport = WebUserTransport(
        inbound_queue=asyncio.Queue(),
        outbound_send=record,
    )
    transport.set_outbound_role("ai_to_user")

    await transport.pause_outbound()

    async def audio_gen() -> AsyncIterator[bytes]:
        yield b"\x01"
        yield b"\x02"

    await transport.output_stream(audio_gen())
    assert sent == []


async def test_force_output_stream_sends_blocks_while_paused() -> None:
    sent: list[tuple[str, bytes]] = []

    async def record(role: str, pcm: bytes) -> None:
        sent.append((role, pcm))

    transport = WebUserTransport(
        inbound_queue=asyncio.Queue(),
        outbound_send=record,
    )
    transport.set_outbound_role("ai_to_merchant")
    await transport.pause_outbound()

    async def audio_gen() -> AsyncIterator[bytes]:
        yield b"\x01"
        yield b"\x02"

    await transport.output_stream_force(audio_gen())

    assert sent == [
        ("ai_to_merchant", b"\x01"),
        ("ai_to_merchant", b"\x02"),
    ]


async def test_resume_outbound_restores_send() -> None:
    sent: list[tuple[str, bytes]] = []

    async def record(role: str, pcm: bytes) -> None:
        sent.append((role, pcm))

    transport = WebUserTransport(
        inbound_queue=asyncio.Queue(),
        outbound_send=record,
    )
    transport.set_outbound_role("ai_to_user")

    await transport.pause_outbound()
    await transport.resume_outbound()

    async def audio_gen() -> AsyncIterator[bytes]:
        yield b"\xa5"

    await transport.output_stream(audio_gen())
    assert sent == [("ai_to_user", b"\xa5")]


async def test_close_pushes_eof_sentinel_to_input_queue() -> None:
    """``close()`` must wake any pending ``input_stream()`` consumer. The
    sentinel-based contract matches MicrophoneTransport.close.
    """
    queue: asyncio.Queue = asyncio.Queue()
    transport = WebUserTransport(inbound_queue=queue, outbound_send=_noop_send)

    received: list[bytes] = []

    async def consume() -> None:
        async for block in transport.input_stream():
            received.append(block)

    consumer = asyncio.create_task(consume())
    await asyncio.sleep(0)  # let the consumer start awaiting on get()
    await transport.close()
    await asyncio.wait_for(consumer, timeout=1.0)
    assert received == []


async def test_close_is_idempotent() -> None:
    transport = WebUserTransport(
        inbound_queue=asyncio.Queue(),
        outbound_send=_noop_send,
    )
    await transport.close()
    await transport.close()  # MUST NOT raise


# -- Browser VAD / EOS tests --------------------------------------------------


async def test_web_vad_reframes_browser_pcm_chunks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = WebUserTransport(
        inbound_queue=asyncio.Queue(),
        outbound_send=_noop_send,
    )
    sizes_seen: list[int] = []

    def fake_is_speech(audio: bytes, sample_rate: int) -> bool:
        sizes_seen.append(len(audio))
        return False

    monkeypatch.setattr(transport._vad, "is_speech", fake_is_speech)

    async def consume() -> None:
        async for _ in transport.input_stream():
            pass

    consumer = asyncio.create_task(consume())
    for _ in range(3):
        transport.push_inbound(b"\x00" * 742)
    await transport.close()
    await asyncio.wait_for(consumer, timeout=1.0)

    assert sizes_seen == [960, 960]


async def test_web_vad_eos_fires_callback_after_silence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = WebUserTransport(
        inbound_queue=asyncio.Queue(),
        outbound_send=_noop_send,
    )
    eos_calls: list[float] = []

    async def on_eos() -> None:
        eos_calls.append(time.monotonic())

    transport._on_eos = on_eos

    voiced_pattern = [True] * 10 + [False] * 10

    def fake_is_speech(audio: bytes, sample_rate: int) -> bool:
        if voiced_pattern:
            return voiced_pattern.pop(0)
        return False

    monkeypatch.setattr(transport._vad, "is_speech", fake_is_speech)

    async def consume() -> None:
        async for _ in transport.input_stream():
            pass

    consumer = asyncio.create_task(consume())
    for _ in range(20):
        transport.push_inbound(b"\x01" * 960)
    for _ in range(80):
        await asyncio.sleep(0.01)
        if eos_calls:
            break
    await transport.close()
    await asyncio.wait_for(consumer, timeout=1.0)

    assert len(eos_calls) == 1
    assert transport._vad_state == "NOTTRIGGERED"
    assert transport.pop_speech_end_ts() is not None
    assert transport.pop_speech_end_ts() is None
