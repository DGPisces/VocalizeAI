"""SenseVoiceClient 协议层测试。

策略：起一个真实的 ``websockets`` server，按 SenseVoice 协议手写期望的服务端
行为（accept start、收 PCM、按指令推 partial/final/error），覆盖：

- start 帧字段正确（language hint、可选 session_id）
- 二进制 PCM 帧透传不变
- partial / final 解析为正确的 ``Transcript`` 字段，含 ``language``
- 非 fatal error 帧不中断流
- fatal error 帧抛 ``SenseVoiceError``
- 调用方 cancel 时客户端发出 ``stop`` 并关闭 socket
"""
from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator

import pytest
import websockets
from websockets.asyncio.server import ServerConnection, serve

from vocalize.stt.base import Transcript
from vocalize.stt.sensevoice import SenseVoiceClient, SenseVoiceError


# ---------------------------------------------------------------------------
# Test fake server
# ---------------------------------------------------------------------------
class FakeServer:
    """脚本化的假 SenseVoice 服务端。

    每个 handler 用 self.script 决定收到什么再发什么；同时记录从客户端收到的事件
    供断言。
    """

    def __init__(self) -> None:
        self.received_text: list[dict] = []      # 解析后的 JSON 控制帧
        self.received_audio: list[bytes] = []    # 原样二进制帧
        self.script: list[dict] | None = None    # 收到第一个 PCM 后向客户端发的消息序列
        self.fatal_after_start: dict | None = None  # 一收到 start 就发的 fatal
        self.send_on_end_of_utterance: list[dict] | None = None
        self._server = None
        self.port: int = 0

    async def start(self) -> None:
        self._server = await serve(self._handler, "127.0.0.1", 0)
        # websockets 15+: server.sockets 是绑定的 socket 列表
        self.port = self._server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    async def _handler(self, ws: ServerConnection) -> None:
        sent_script = False
        try:
            async for msg in ws:
                if isinstance(msg, str):
                    parsed = json.loads(msg)
                    self.received_text.append(parsed)
                    if parsed.get("event") == "start" and self.fatal_after_start:
                        await ws.send(json.dumps(self.fatal_after_start))
                        await ws.close()
                        return
                    if parsed.get("event") == "end_of_utterance" and \
                            self.send_on_end_of_utterance:
                        for item in self.send_on_end_of_utterance:
                            await ws.send(json.dumps(item))
                    if parsed.get("event") == "stop":
                        await ws.close()
                        return
                else:
                    self.received_audio.append(bytes(msg))
                    if not sent_script and self.script:
                        sent_script = True
                        for item in self.script:
                            await ws.send(json.dumps(item))
        except websockets.exceptions.ConnectionClosed:
            pass


@pytest.fixture
async def fake_server() -> AsyncIterator[FakeServer]:
    srv = FakeServer()
    await srv.start()
    try:
        yield srv
    finally:
        await srv.stop()


async def _audio_iter(chunks: list[bytes], delay: float = 0.0) -> AsyncIterator[bytes]:
    for c in chunks:
        if delay:
            await asyncio.sleep(delay)
        yield c


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------
async def test_sends_start_then_audio_then_stop(fake_server: FakeServer) -> None:
    fake_server.script = [
        {"text": "你好", "is_final": True, "confidence": 0.9,
         "start_time": 0.0, "end_time": 0.5, "utterance_id": 0, "language": "zh"},
    ]
    client = SenseVoiceClient(
        host="127.0.0.1", port=fake_server.port,
        language_hint="zh", session_id="sess-1",
    )

    audio = _audio_iter([b"\x00\x01" * 480, b"\x02\x03" * 480])
    out = [t async for t in client.stream_transcribe(audio)]

    # control frames: start (first), end_of_utterance (after audio), stop
    events = [m.get("event") for m in fake_server.received_text]
    assert events[0] == "start"
    assert "end_of_utterance" in events
    assert events[-1] == "stop"

    start_msg = fake_server.received_text[0]
    assert start_msg["language"] == "zh"
    assert start_msg["session_id"] == "sess-1"

    assert fake_server.received_audio == [b"\x00\x01" * 480, b"\x02\x03" * 480]
    assert len(out) == 1
    t = out[0]
    assert isinstance(t, Transcript)
    assert t.text == "你好"
    assert t.is_final is True
    assert t.language == "zh"
    assert t.utterance_id == 0


async def test_parses_partial_then_final_with_language(
    fake_server: FakeServer,
) -> None:
    fake_server.script = [
        {"text": "book a", "is_final": False, "confidence": 0.7,
         "start_time": 0.0, "end_time": 0.4, "utterance_id": 0, "language": None},
        {"text": "book a table for four", "is_final": True, "confidence": 0.95,
         "start_time": 0.0, "end_time": 1.2, "utterance_id": 0, "language": "en"},
    ]
    client = SenseVoiceClient(host="127.0.0.1", port=fake_server.port)

    audio = _audio_iter([b"\x00" * 960])
    out = [t async for t in client.stream_transcribe(audio)]

    assert len(out) == 2
    assert out[0].is_final is False
    assert out[0].language is None
    assert out[1].is_final is True
    assert out[1].language == "en"
    assert out[1].text == "book a table for four"
    assert out[0].utterance_id == out[1].utterance_id == 0


async def test_non_fatal_error_does_not_break_stream(
    fake_server: FakeServer,
) -> None:
    fake_server.script = [
        {"error": "transient inference glitch", "fatal": False},
        {"text": "ok", "is_final": True, "confidence": 1.0,
         "start_time": 0.0, "end_time": 0.3, "utterance_id": 0, "language": "en"},
    ]
    client = SenseVoiceClient(host="127.0.0.1", port=fake_server.port)
    out = [t async for t in client.stream_transcribe(_audio_iter([b"\x00" * 100]))]
    assert len(out) == 1
    assert out[0].text == "ok"


async def test_fatal_error_raises(fake_server: FakeServer) -> None:
    fake_server.fatal_after_start = {"error": "oom", "fatal": True}
    client = SenseVoiceClient(host="127.0.0.1", port=fake_server.port)

    with pytest.raises(SenseVoiceError, match="oom"):
        async for _ in client.stream_transcribe(_audio_iter([b"\x00" * 100])):
            pass


async def test_connection_refused_raises() -> None:
    # 端口 1 几乎肯定连不上
    client = SenseVoiceClient(
        host="127.0.0.1", port=1, connect_timeout_s=1.0, open_timeout_s=1.0,
    )
    with pytest.raises(SenseVoiceError):
        async for _ in client.stream_transcribe(_audio_iter([b"\x00" * 10])):
            pass


async def test_cancellation_sends_stop(fake_server: FakeServer) -> None:
    """调用方 break / aclose() 时客户端应通知服务端 stop，不能泄漏 socket。"""
    # 服务端不主动发任何东西，让客户端等
    fake_server.script = []
    client = SenseVoiceClient(host="127.0.0.1", port=fake_server.port)

    async def slow_audio() -> AsyncIterator[bytes]:
        for _ in range(100):
            await asyncio.sleep(0.05)
            yield b"\x00" * 320

    async def drain() -> None:
        async for _ in client.stream_transcribe(slow_audio()):
            pass

    task = asyncio.create_task(drain())
    # 等到第一个 PCM 到达服务端
    for _ in range(40):
        if fake_server.received_audio:
            break
        await asyncio.sleep(0.05)
    assert fake_server.received_audio, "server never received audio"

    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):
        pass

    # 给 server handler 一点时间把最后的 frame 收完
    for _ in range(20):
        events = [m.get("event") for m in fake_server.received_text]
        if "stop" in events:
            break
        await asyncio.sleep(0.05)
    events = [m.get("event") for m in fake_server.received_text]
    assert "stop" in events, f"expected stop in {events}"


async def _drain_one(gen: AsyncIterator[Transcript]) -> Transcript | None:
    async for t in gen:
        return t
    return None


# ---------------------------------------------------------------------------
# Audio-side failure handling
# ---------------------------------------------------------------------------
class _AudioCaptureError(RuntimeError):
    """Stand-in for a real device read / file decode failure."""


async def test_audio_iterator_failure_surfaces_to_caller(
    fake_server: FakeServer,
) -> None:
    """If the upstream audio iterator raises, the caller MUST see a failure.

    Regression for Codex P2: previously the ``finally`` block only awaited
    ``sender_task`` when not done; an already-failed sender task had its
    exception silently dropped, leaving the receive loop hung waiting for
    server frames that would never come, and emitting a
    ``Task exception was never retrieved`` warning.
    """
    # Server stays silent on PCM so the receive loop has nothing to yield;
    # only the audio-side failure should be the trigger.
    fake_server.script = []
    client = SenseVoiceClient(host="127.0.0.1", port=fake_server.port)

    async def boom() -> AsyncIterator[bytes]:
        # First chunk goes through fine, then the device blows up.
        yield b"\x00" * 320
        await asyncio.sleep(0.05)
        raise _AudioCaptureError("microphone read failed")

    # Should NOT hang — the audio failure must propagate as SenseVoiceError.
    with pytest.raises(SenseVoiceError, match="audio sender failed"):
        async with asyncio.timeout(3.0):
            async for _ in client.stream_transcribe(boom()):
                pass


async def test_audio_iterator_failure_no_orphan_task_warning(
    fake_server: FakeServer,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """No 'Task exception was never retrieved' should leak from cleanup."""
    fake_server.script = []
    client = SenseVoiceClient(host="127.0.0.1", port=fake_server.port)

    captured: list[str] = []
    loop = asyncio.get_running_loop()
    prev_handler = loop.get_exception_handler()

    def _handler(_loop, context):  # type: ignore[no-untyped-def]
        captured.append(str(context.get("message", "")))

    loop.set_exception_handler(_handler)
    try:
        async def boom() -> AsyncIterator[bytes]:
            yield b"\x00" * 320
            await asyncio.sleep(0.05)
            raise _AudioCaptureError("device gone")

        with pytest.raises(SenseVoiceError):
            async with asyncio.timeout(3.0):
                async for _ in client.stream_transcribe(boom()):
                    pass

        # Give the loop a tick to surface any orphan task warnings.
        await asyncio.sleep(0.05)
    finally:
        loop.set_exception_handler(prev_handler)

    leaked = [m for m in captured if "never retrieved" in m]
    assert not leaked, f"orphan task exception leaked: {leaked}"


async def test_fatal_server_error_not_masked_by_sender_failure(
    fake_server: FakeServer,
) -> None:
    """A fatal server error must surface even if the sender also fails.

    Edge case: server sends fatal AFTER receiving start, then closes the
    connection. The sender then hits ``ConnectionClosed`` while writing PCM,
    which is handled internally — but if the sender had a different failure
    mode, the original fatal error must win, not the sender's.
    """
    fake_server.fatal_after_start = {"error": "oom", "fatal": True}
    client = SenseVoiceClient(host="127.0.0.1", port=fake_server.port)

    async def slow_audio() -> AsyncIterator[bytes]:
        for _ in range(50):
            await asyncio.sleep(0.02)
            yield b"\x00" * 320

    # Original fatal "oom" must be what the caller sees.
    with pytest.raises(SenseVoiceError, match="oom"):
        async with asyncio.timeout(3.0):
            async for _ in client.stream_transcribe(slow_audio()):
                pass


async def test_graceful_close_mid_stream_raises() -> None:
    """Fix 1 回归：服务端 code=1000 graceful close 在 sender 还未完成时必须抛错。

    ConnectionClosedOK 是 ConnectionClosed 的子类；修复前它被单独 swallow，
    导致 client 返回 0 条 transcript、没有异常（silent failure）。
    修复后：collapsed except 统一用 sender_done gate，code=1000 也抛 SenseVoiceError。
    """
    from websockets.asyncio.server import serve as ws_serve

    async def handler(ws: ServerConnection) -> None:
        try:
            async for msg in ws:
                if isinstance(msg, str):
                    parsed = json.loads(msg)
                    if parsed.get("event") == "start":
                        # 等一小段让 sender 送出至少一个音频帧再关
                        await asyncio.sleep(0.05)
                        await ws.close(code=1000, reason="going away")
                        return
        except websockets.exceptions.ConnectionClosed:
            pass

    server = await ws_serve(handler, "127.0.0.1", 0)
    try:
        port = server.sockets[0].getsockname()[1]
        client = SenseVoiceClient(host="127.0.0.1", port=port)

        async def slow_audio() -> AsyncIterator[bytes]:
            for _ in range(50):
                await asyncio.sleep(0.02)
                yield b"\x00" * 320

        with pytest.raises(SenseVoiceError, match="mid-stream"):
            async with asyncio.timeout(3.0):
                async for _ in client.stream_transcribe(slow_audio()):
                    pass
    finally:
        server.close()
        await server.wait_closed()


# ---------------------------------------------------------------------------
# Phase 4 Plan 04-04 — client-side VAD EOS handshake
# ---------------------------------------------------------------------------
class _FakeVADTransport:
    """Stand-in for MicrophoneTransport that exposes ``_on_eos`` and tracks
    when the SenseVoiceClient registers a handler on it."""

    def __init__(self) -> None:
        self._on_eos = None


async def test_eos_handler_sends_end_of_utterance_json(
    fake_server: FakeServer,
) -> None:
    """SenseVoiceClient must register transport._on_eos at the start of
    stream_transcribe and, when invoked, send the JSON
    {"event": "end_of_utterance"} over the WS — preempting the server-side
    fsmn-vad fallback (Plan 04-04 Task 3)."""
    # Server replies with a final on receiving end_of_utterance.
    fake_server.send_on_end_of_utterance = [
        {"text": "hi", "is_final": True, "confidence": 0.9,
         "start_time": 0.0, "end_time": 0.4, "utterance_id": 0, "language": "en"},
    ]
    client = SenseVoiceClient(host="127.0.0.1", port=fake_server.port)
    transport = _FakeVADTransport()

    async def slow_audio() -> AsyncIterator[bytes]:
        # Keep the WS open while we drive an EOS via the registered handler.
        for i in range(20):
            await asyncio.sleep(0.02)
            yield b"\x00" * 320
            # After we've sent a couple of audio frames, fire the VAD EOS
            # callback that the client should have registered on us.
            if i == 2 and transport._on_eos is not None:
                await transport._on_eos()

    transcripts = []
    async for t in client.stream_transcribe(slow_audio(), transport=transport):
        transcripts.append(t)
        # Once we got the EOS-triggered final, break to wind down.
        if t.is_final:
            break

    # Server must have received an end_of_utterance event from the client.
    events = [m.get("event") for m in fake_server.received_text]
    assert events.count("end_of_utterance") >= 1, (
        f"expected at least one client-sent end_of_utterance, got {events}"
    )
    assert transcripts and transcripts[-1].text == "hi"
    # last_eos_wall_clock stamped on EOS send.
    assert client.last_eos_wall_clock is not None


async def test_eos_handler_no_transport_no_registration(
    fake_server: FakeServer,
) -> None:
    """If transport is None / lacks ``_on_eos``, stream_transcribe must not
    crash and must not register anything (legacy path)."""
    fake_server.script = [
        {"text": "ok", "is_final": True, "confidence": 0.9,
         "start_time": 0.0, "end_time": 0.4, "utterance_id": 0, "language": "en"},
    ]
    client = SenseVoiceClient(host="127.0.0.1", port=fake_server.port)

    audio = _audio_iter([b"\x00" * 320, b"\x00" * 320])
    out = [t async for t in client.stream_transcribe(audio)]  # no transport kwarg
    assert len(out) == 1
    assert client.last_eos_wall_clock is None


async def test_server_crash_mid_send_is_not_silently_swallowed() -> None:
    """B2 回归：服务端在客户端还在推 PCM 时强制关连接，``_send_audio`` 不能把
    ``ConnectionClosed`` 当成 clean close（之前会无条件 set sender_done，
    导致接收侧也把 close 判为 OK，pipeline 拿到截断转写却没有错误）。
    """
    from websockets.asyncio.server import serve

    async def handler(ws):  # type: ignore[no-untyped-def]
        try:
            async for msg in ws:
                if isinstance(msg, str):
                    parsed = json.loads(msg)
                    if parsed.get("event") == "start":
                        await ws.close(code=1011, reason="boom")
                        return
        except websockets.exceptions.ConnectionClosed:
            pass

    server = await serve(handler, "127.0.0.1", 0)
    try:
        port = server.sockets[0].getsockname()[1]
        client = SenseVoiceClient(host="127.0.0.1", port=port)

        async def slow_audio() -> AsyncIterator[bytes]:
            for _ in range(50):
                await asyncio.sleep(0.02)
                yield b"\x00" * 320

        with pytest.raises(SenseVoiceError):
            async with asyncio.timeout(3.0):
                async for _ in client.stream_transcribe(slow_audio()):
                    pass
    finally:
        server.close()
        await server.wait_closed()
