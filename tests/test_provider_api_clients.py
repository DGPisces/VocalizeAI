"""Provider API speech client protocol tests."""
from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import pytest
import websockets
from websockets.asyncio.server import ServerConnection, serve

from vocalize.config import Config
from vocalize.providers import ProviderSTTClient, ProviderTTSClient
from vocalize.stt.base import Transcript
from vocalize.tts.base import TextChunk


class _FakeProviderServer:
    def __init__(self, mode: str) -> None:
        self.mode = mode
        self.received_text: list[dict[str, Any]] = []
        self.received_audio: list[bytes] = []
        self._server: Any = None
        self.port = 0

    async def start(self) -> None:
        self._server = await serve(self._handler, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    async def _handler(self, ws: ServerConnection) -> None:
        try:
            async for msg in ws:
                if isinstance(msg, bytes):
                    self.received_audio.append(bytes(msg))
                    continue

                parsed = json.loads(msg)
                self.received_text.append(parsed)
                msg_type = parsed.get("type")
                if self.mode == "stt" and msg_type == "end_of_utterance":
                    await ws.send(
                        json.dumps(
                            {
                                "type": "transcript",
                                "text": "你好",
                                "is_final": True,
                                "confidence": 0.92,
                                "start_time": 0.0,
                                "end_time": 0.4,
                                "utterance_id": 7,
                                "language": "zh",
                                "segments": [
                                    {
                                        "text": "你好",
                                        "language": "zh",
                                        "start_time": 0.0,
                                        "end_time": 0.4,
                                    }
                                ],
                            }
                        )
                    )
                if self.mode == "tts" and msg_type == "start":
                    await ws.send(
                        json.dumps(
                            {
                                "type": "audio_start",
                                "sample_rate": 24000,
                                "encoding": "pcm_s16le",
                            }
                        )
                    )
                if self.mode == "tts" and msg_type == "text":
                    await ws.send(b"\x01\x02" * 8)
                    await ws.send(json.dumps({"type": "audio_end"}))
                if msg_type == "stop":
                    await ws.close()
                    return
        except websockets.exceptions.ConnectionClosed:
            pass


@pytest.fixture
async def stt_server() -> AsyncIterator[_FakeProviderServer]:
    server = _FakeProviderServer("stt")
    await server.start()
    try:
        yield server
    finally:
        await server.stop()


@pytest.fixture
async def tts_server() -> AsyncIterator[_FakeProviderServer]:
    server = _FakeProviderServer("tts")
    await server.start()
    try:
        yield server
    finally:
        await server.stop()


async def _audio_iter(chunks: list[bytes]) -> AsyncIterator[bytes]:
    for chunk in chunks:
        yield chunk


async def _text_iter(chunks: list[TextChunk]) -> AsyncIterator[TextChunk]:
    for chunk in chunks:
        yield chunk


async def test_provider_stt_streams_audio_and_parses_transcript(
    stt_server: _FakeProviderServer,
) -> None:
    client = ProviderSTTClient(
        base_url=f"http://127.0.0.1:{stt_server.port}",
        language_hint="zh",
        session_id="sess-1",
    )

    out = [
        transcript
        async for transcript in client.stream_transcribe(
            _audio_iter([b"\x00\x01" * 16])
        )
    ]

    events = [item["type"] for item in stt_server.received_text]
    assert events == ["start", "end_of_utterance", "stop"]
    assert stt_server.received_text[0]["provider_api_version"] == "1.0"
    assert stt_server.received_text[0]["language"] == "zh"
    assert stt_server.received_text[0]["session_id"] == "sess-1"
    assert stt_server.received_audio == [b"\x00\x01" * 16]
    assert len(out) == 1
    assert isinstance(out[0], Transcript)
    assert out[0].text == "你好"
    assert out[0].is_final is True
    assert out[0].utterance_id == 7
    assert out[0].segments is not None
    assert out[0].segments[0].language == "zh"


async def test_provider_tts_streams_text_and_yields_audio(
    tts_server: _FakeProviderServer,
) -> None:
    client = ProviderTTSClient(
        base_url=f"http://127.0.0.1:{tts_server.port}",
        default_language="en",
        session_id="sess-2",
    )

    out = [
        chunk
        async for chunk in client.stream_synthesize(
            _text_iter([TextChunk(text="hello", language="en", is_final_segment=True)])
        )
    ]

    events = [item["type"] for item in tts_server.received_text]
    assert events == ["start", "text", "stop"]
    assert tts_server.received_text[0]["provider_api_version"] == "1.0"
    assert tts_server.received_text[0]["language"] == "en"
    assert tts_server.received_text[0]["session_id"] == "sess-2"
    assert tts_server.received_text[1] == {
        "type": "text",
        "text": "hello",
        "language": "en",
        "is_final_segment": True,
    }
    assert out == [b"\x01\x02" * 8]


async def test_provider_tts_health_check(tts_server: _FakeProviderServer) -> None:
    client = ProviderTTSClient(base_url=f"http://127.0.0.1:{tts_server.port}")

    assert await client.health_check() is True


def test_provider_clients_from_app_config() -> None:
    cfg = Config(
        stt_provider_url="http://127.0.0.1:18000",
        tts_provider_url="http://127.0.0.1:18001",
        default_language="zh",
        provider_connect_timeout_s=1.25,
    )

    stt = ProviderSTTClient.from_app_config(cfg)
    tts = ProviderTTSClient.from_app_config(cfg)

    assert stt.base_url == "http://127.0.0.1:18000"
    assert stt.language_hint == "zh"
    assert stt.connect_timeout_s == 1.25
    assert tts.base_url == "http://127.0.0.1:18001"
    assert tts.default_language == "zh"
    assert tts.connect_timeout_s == 1.25
