"""CosyVoiceClient 协议层测试。

策略：起一个真实的 ``websockets`` server，按 CosyVoice 协议手写期望的服务端
行为（accept start、收 text 帧、按指令推 audio_start / 二进制 PCM / audio_end /
error），覆盖：

- start 帧字段正确（language、speed、可选 prompt_*、可选 session_id）
- 多个 text 帧 + 最后一个 is_final_segment 完整 forward
- 二进制 PCM 帧按顺序透传
- audio_start.sample_rate 覆盖 client.output_sample_rate
- 非 fatal error 不中断流；fatal error → CosyVoiceError
- caller break / aclose() → server 收到 stop
- health_check 在健康/故障时分别返回 True/False
"""
from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator
from typing import Any

import pytest
import websockets
from websockets.asyncio.server import ServerConnection, serve

from vocalize.tts.base import TextChunk
from vocalize.tts.cosyvoice import CosyVoiceClient, CosyVoiceError


class FakeServer:
    """脚本化假 CosyVoice 服务端。

    收到 start → 立即发 audio_start（如果脚本指定）；之后每收一个 text 帧，
    按 ``per_text_script`` 的策略推音频/控制帧；收到 stop 关闭。
    """

    def __init__(self) -> None:
        self.received_text: list[dict] = []
        self.audio_start: dict | None = None
        # 收到第 i 个 text 帧后要发的 list[bytes | dict]（dict 是 JSON 控制帧）
        self.per_text_script: list[list] = []
        self.fatal_after_start: dict | None = None
        self._server: Any = None
        self.port: int = 0

    async def start(self) -> None:
        self._server = await serve(self._handler, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    async def _handler(self, ws: ServerConnection) -> None:
        text_idx = 0
        try:
            async for msg in ws:
                if not isinstance(msg, str):
                    continue
                parsed = json.loads(msg)
                self.received_text.append(parsed)
                event = parsed.get("event")
                if event == "start":
                    if self.fatal_after_start:
                        await ws.send(json.dumps(self.fatal_after_start))
                        await ws.close()
                        return
                    if self.audio_start is not None:
                        await ws.send(json.dumps(self.audio_start))
                elif event == "text":
                    if text_idx < len(self.per_text_script):
                        for item in self.per_text_script[text_idx]:
                            if isinstance(item, (bytes, bytearray)):
                                await ws.send(bytes(item))
                            else:
                                await ws.send(json.dumps(item))
                    text_idx += 1
                elif event == "stop":
                    await ws.close()
                    return
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


async def _text_iter(chunks: list[TextChunk]) -> AsyncIterator[TextChunk]:
    for c in chunks:
        yield c


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------
async def test_start_frame_fields(fake_server: FakeServer) -> None:
    fake_server.audio_start = {
        "event": "audio_start", "sample_rate": 24000, "encoding": "pcm_s16le",
        "channels": 1, "utterance_id": 0, "mode": "zero_shot",
    }
    fake_server.per_text_script = [
        [b"\x01\x02" * 10, {"event": "audio_end", "utterance_id": 0}],
    ]
    client = CosyVoiceClient(
        host="127.0.0.1", port=fake_server.port,
        default_language="en", speed=1.2,
        prompt_wav="/tmp/x.wav", prompt_text="hi",
        session_id="sess-99",
    )
    out = [b async for b in client.stream_synthesize(_text_iter([
        TextChunk(text="hello", language="en", is_final_segment=True),
    ]))]

    assert out == [b"\x01\x02" * 10]
    start = fake_server.received_text[0]
    assert start["event"] == "start"
    assert start["language"] == "en"
    assert start["speed"] == 1.2
    assert start["prompt_wav"] == "/tmp/x.wav"
    assert start["prompt_text"] == "hi"
    assert start["session_id"] == "sess-99"


async def test_multiple_text_frames_with_final_flush(
    fake_server: FakeServer,
) -> None:
    fake_server.audio_start = {
        "event": "audio_start", "sample_rate": 24000,
        "encoding": "pcm_s16le", "channels": 1, "utterance_id": 0,
        "mode": "zero_shot",
    }
    fake_server.per_text_script = [
        [b"\xaa" * 4],
        [b"\xbb" * 4, {"event": "audio_end", "utterance_id": 0}],
    ]
    client = CosyVoiceClient(host="127.0.0.1", port=fake_server.port)
    out = [b async for b in client.stream_synthesize(_text_iter([
        TextChunk(text="hi.", language="en", is_final_segment=False),
        TextChunk(text="bye.", language="en", is_final_segment=True),
    ]))]
    assert out == [b"\xaa" * 4, b"\xbb" * 4]
    text_events = [m for m in fake_server.received_text if m.get("event") == "text"]
    assert len(text_events) == 2
    assert text_events[0]["is_final_segment"] is False
    assert text_events[1]["is_final_segment"] is True
    # stop 应在最后
    assert fake_server.received_text[-1]["event"] == "stop"


async def test_pcm_bytes_pass_through_in_order(fake_server: FakeServer) -> None:
    blocks = [bytes([i]) * 8 for i in range(5)]
    fake_server.audio_start = {
        "event": "audio_start", "sample_rate": 24000,
        "encoding": "pcm_s16le", "channels": 1, "utterance_id": 0,
        "mode": "zero_shot",
    }
    fake_server.per_text_script = [list(blocks)]
    client = CosyVoiceClient(host="127.0.0.1", port=fake_server.port)
    out = [b async for b in client.stream_synthesize(_text_iter([
        TextChunk(text="x", language="zh", is_final_segment=True),
    ]))]
    assert out == blocks


async def test_audio_start_mismatch_logs_warning_no_mutation(
    fake_server: FakeServer, caplog: pytest.LogCaptureFixture,
) -> None:
    """I3 回归：服务端 audio_start.sample_rate 与客户端配置不一致时，只 log
    warning，不 mutate ``output_sample_rate``。Mutation 会让已经按客户端 SR 打开
    的下游 PortAudio output stream 出现 pitch-shift bug。
    """
    fake_server.audio_start = {
        "event": "audio_start", "sample_rate": 22050,
        "encoding": "pcm_s16le", "channels": 1, "utterance_id": 0,
        "mode": "zero_shot",
    }
    fake_server.per_text_script = [[b"\x00\x00"]]
    client = CosyVoiceClient(
        host="127.0.0.1", port=fake_server.port, output_sample_rate=24000,
    )
    with caplog.at_level("WARNING", logger="vocalize.tts.cosyvoice"):
        async for _ in client.stream_synthesize(_text_iter([
            TextChunk(text="x", language="zh", is_final_segment=True),
        ])):
            pass
    assert client.output_sample_rate == 24000  # client config wins
    assert any("sample_rate" in r.message for r in caplog.records)


async def test_non_fatal_error_does_not_break_stream(
    fake_server: FakeServer,
) -> None:
    fake_server.audio_start = {
        "event": "audio_start", "sample_rate": 24000,
        "encoding": "pcm_s16le", "channels": 1, "utterance_id": 0,
        "mode": "zero_shot",
    }
    fake_server.per_text_script = [
        [{"error": "transient glitch", "fatal": False}, b"\x11" * 4],
    ]
    client = CosyVoiceClient(host="127.0.0.1", port=fake_server.port)
    out = [b async for b in client.stream_synthesize(_text_iter([
        TextChunk(text="x", language="zh", is_final_segment=True),
    ]))]
    assert out == [b"\x11" * 4]


async def test_fatal_error_raises(fake_server: FakeServer) -> None:
    fake_server.fatal_after_start = {"error": "oom", "fatal": True}
    client = CosyVoiceClient(host="127.0.0.1", port=fake_server.port)
    with pytest.raises(CosyVoiceError, match="oom"):
        async for _ in client.stream_synthesize(_text_iter([
            TextChunk(text="x", language="zh", is_final_segment=True),
        ])):
            pass


async def test_connection_refused_returns_health_false() -> None:
    client = CosyVoiceClient(
        host="127.0.0.1", port=1, connect_timeout_s=1.0, open_timeout_s=1.0,
    )
    assert await client.health_check() is False


async def test_health_check_ok(fake_server: FakeServer) -> None:
    client = CosyVoiceClient(host="127.0.0.1", port=fake_server.port)
    assert await client.health_check() is True


async def test_break_triggers_stop(fake_server: FakeServer) -> None:
    """caller 用 ``break`` 提前结束 → 客户端 finally 发 stop。

    用 ``contextlib.aclosing`` 保证确定性触发（async-for 自动 GC 时机不可靠）。
    """
    # 服务端发完 audio_start 后无限拖时间，让 caller 有机会 break
    fake_server.audio_start = {
        "event": "audio_start", "sample_rate": 24000,
        "encoding": "pcm_s16le", "channels": 1, "utterance_id": 0,
        "mode": "zero_shot",
    }
    fake_server.per_text_script = [[b"\x01" * 4, b"\x02" * 4, b"\x03" * 4]]

    client = CosyVoiceClient(host="127.0.0.1", port=fake_server.port)
    async with contextlib.aclosing(client.stream_synthesize(_text_iter([  # type: ignore[type-var]
        TextChunk(text="hi", language="zh", is_final_segment=True),
    ]))) as it:
        async for _audio in it:
            break

    # 给 server 一点时间收完
    for _ in range(20):
        events = [m.get("event") for m in fake_server.received_text]
        if "stop" in events:
            break
        await asyncio.sleep(0.05)
    events = [m.get("event") for m in fake_server.received_text]
    assert "stop" in events, f"expected stop in {events}"


async def test_empty_final_segment_chunk_is_forwarded(
    fake_server: FakeServer,
) -> None:
    """B4 回归：``TextChunk(text="", is_final_segment=True)`` 是 pipeline 触发服务端
    flush 的哨兵；``_send_text`` 不能因 ``not chunk.text`` 把它过滤掉，否则尾音被吞。
    """
    fake_server.audio_start = {
        "event": "audio_start", "sample_rate": 24000,
        "encoding": "pcm_s16le", "channels": 1, "utterance_id": 0,
        "mode": "zero_shot",
    }
    fake_server.per_text_script = [
        [b"\x33" * 4],
        [b"\x44" * 4, {"event": "audio_end", "utterance_id": 0}],
    ]
    client = CosyVoiceClient(host="127.0.0.1", port=fake_server.port)
    out = [b async for b in client.stream_synthesize(_text_iter([
        TextChunk(text="hi.", language="en", is_final_segment=False),
        TextChunk(text="", language="en", is_final_segment=True),
    ]))]
    assert out == [b"\x33" * 4, b"\x44" * 4]
    text_events = [m for m in fake_server.received_text if m.get("event") == "text"]
    assert len(text_events) == 2
    # 第二个 text 帧必须送达且带 is_final_segment=True
    assert text_events[1]["text"] == ""
    assert text_events[1]["is_final_segment"] is True


async def test_server_crash_mid_send_is_not_silently_swallowed() -> None:
    """B2 回归：服务端在客户端还在推 text 时强制关连接，``_send_text`` 不能把
    ``ConnectionClosed`` 当成 clean close（之前会无条件 set sender_done，
    导致接收侧把 close 也判为 OK，pipeline 拿到截断音频却没有错误）。
    """
    crashing_server: Any = None

    async def handler(ws: ServerConnection) -> None:
        # 收到 start 后立即关 socket（不发任何 audio_end / audio_start）
        try:
            async for msg in ws:
                if isinstance(msg, str):
                    parsed = json.loads(msg)
                    if parsed.get("event") == "start":
                        await ws.close(code=1011, reason="boom")
                        return
        except websockets.exceptions.ConnectionClosed:
            pass

    crashing_server = await serve(handler, "127.0.0.1", 0)
    try:
        port = crashing_server.sockets[0].getsockname()[1]
        client = CosyVoiceClient(host="127.0.0.1", port=port)

        async def slow_text() -> AsyncIterator[TextChunk]:
            for i in range(20):
                await asyncio.sleep(0.02)
                yield TextChunk(text=f"chunk{i}.", language="en",
                                is_final_segment=False)

        with pytest.raises(CosyVoiceError):
            async with asyncio.timeout(3.0):
                async for _ in client.stream_synthesize(slow_text()):
                    pass
    finally:
        crashing_server.close()
        await crashing_server.wait_closed()


async def test_graceful_close_mid_stream_raises() -> None:
    """Fix 1 回归：服务端 code=1000 graceful close 在 sender 还未完成时必须抛错。

    ConnectionClosedOK 是 ConnectionClosed 的子类；修复前它被单独 swallow，
    导致 client 返回 0 字节、没有异常（silent failure）。
    修复后：collapsed except 统一用 sender_done gate，code=1000 也抛 CosyVoiceError。
    """
    async def handler(ws: ServerConnection) -> None:
        try:
            async for msg in ws:
                if not isinstance(msg, str):
                    continue
                parsed = json.loads(msg)
                if parsed.get("event") == "start":
                    # 等一小段让 sender 送出至少一个 text 帧再关
                    await asyncio.sleep(0.05)
                    await ws.close(code=1000, reason="going away")
                    return
        except websockets.exceptions.ConnectionClosed:
            pass

    server = await serve(handler, "127.0.0.1", 0)
    try:
        port = server.sockets[0].getsockname()[1]
        client = CosyVoiceClient(host="127.0.0.1", port=port)

        async def slow_text() -> AsyncIterator[TextChunk]:
            for i in range(20):
                await asyncio.sleep(0.02)
                yield TextChunk(text=f"chunk{i}.", language="zh",
                                is_final_segment=False)

        with pytest.raises(CosyVoiceError, match="mid-stream"):
            async with asyncio.timeout(3.0):
                async for _ in client.stream_synthesize(slow_text()):
                    pass
    finally:
        server.close()
        await server.wait_closed()


def test_post_init_rejects_invalid_port() -> None:
    """I1 回归：``CosyVoiceClient.__post_init__`` 校验 port 范围。"""
    with pytest.raises(CosyVoiceError, match="port"):
        CosyVoiceClient(host="x", port=0)


def test_post_init_rejects_prompt_wav_without_text() -> None:
    """I1 回归：zero-shot 克隆需要 wav + 对应文本同时给出。"""
    with pytest.raises(CosyVoiceError, match="prompt_wav"):
        CosyVoiceClient(host="x", prompt_wav="/tmp/x.wav", prompt_text=None)


async def test_from_app_config_missing_gpu_host() -> None:
    from vocalize.config import Config
    cfg = Config(gpu_host="")
    with pytest.raises(CosyVoiceError, match="GPU_HOST"):
        CosyVoiceClient.from_app_config(cfg)


async def test_from_app_config_ok() -> None:
    from vocalize.config import Config
    cfg = Config(gpu_host="example.test", cosyvoice_ws_port=9000,
                 default_language="en")
    client = CosyVoiceClient.from_app_config(cfg)
    assert client.host == "example.test"
    assert client.port == 9000
    assert client.default_language == "en"
