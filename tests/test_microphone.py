"""MicrophoneTransport 单元测试。

不依赖真实音频设备：monkeypatch ``sd.RawInputStream`` 为 fake，让测试手动触发
PortAudio 回调。覆盖 PR #4 code-review 提出的两个修复：

1. ``close()`` 跨任务能唤醒 ``input_stream()`` 的 ``await queue.get()``（Issue 2）。
2. 回调发得比消费快时不会泄露 ``QueueFull``，且 "queue full" 告警至少打一次（Issue 1）。
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from typing import Any

import pytest

from vocalize.transports import microphone as mic_mod
from vocalize.transports.microphone import MicrophoneTransport


class _FakeStream:
    """伪 sounddevice.RawInputStream：只记录 callback、不真起 PortAudio。

    测试可读取 ``self.callback`` 直接喂 PCM 字节，模拟回调线程到达。
    """

    instances: list["_FakeStream"] = []

    def __init__(self, *, callback: Any, **kwargs: Any) -> None:
        self.callback = callback
        self.kwargs = kwargs
        self.started = False
        self.stopped = False
        self.closed = False
        _FakeStream.instances.append(self)

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped = True

    def close(self) -> None:
        self.closed = True


class _FakeOutputStream:
    """伪 sounddevice.RawOutputStream：测试驱动 callback 拉数据写入伪 hardware buffer。"""

    instances: list["_FakeOutputStream"] = []

    def __init__(self, *, callback: Any, blocksize: int, **kwargs: Any) -> None:
        self.callback = callback
        self.blocksize = blocksize
        self.kwargs = kwargs
        self.started = False
        self.stopped = False
        self.closed = False
        self.played: bytearray = bytearray()
        _FakeOutputStream.instances.append(self)

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped = True

    def close(self) -> None:
        self.closed = True

    def pump(self) -> None:
        """模拟 PortAudio 的一次 callback 调用。"""
        buf = bytearray(self.blocksize * 2)
        view = memoryview(buf)
        self.callback(view, self.blocksize, None, 0)
        self.played.extend(bytes(buf))


@pytest.fixture
def fake_sd(monkeypatch: pytest.MonkeyPatch) -> type[_FakeStream]:
    """把 sounddevice.RawInputStream 换成 _FakeStream，测试期间不开真硬件。"""
    _FakeStream.instances = []
    monkeypatch.setattr(mic_mod.sd, "RawInputStream", _FakeStream)
    return _FakeStream


@pytest.fixture
def fake_sd_output(
    monkeypatch: pytest.MonkeyPatch,
) -> type[_FakeOutputStream]:
    _FakeOutputStream.instances = []
    monkeypatch.setattr(mic_mod.sd, "RawOutputStream", _FakeOutputStream)
    return _FakeOutputStream


# ---------------------------------------------------------------------------
# Issue 2: close() must unblock input_stream() across tasks
# ---------------------------------------------------------------------------
async def test_close_from_other_task_unblocks_input_stream(
    fake_sd: type[_FakeStream],
) -> None:
    """另一个任务调 close()，input_stream() 必须 1 秒内退出（而不是死锁在 queue.get）。"""
    transport = MicrophoneTransport(queue_maxsize=4)

    received: list[bytes] = []

    async def consume() -> None:
        async for chunk in transport.input_stream():
            received.append(chunk)

    consumer = asyncio.create_task(consume())

    # 等到 input_stream() 把 fake stream 起来 + 把 self._queue 装好
    for _ in range(50):
        if fake_sd.instances and transport._queue is not None:
            break
        await asyncio.sleep(0.01)
    assert fake_sd.instances, "input_stream did not open the fake sd stream"
    assert transport._queue is not None

    # 从 "另一个任务" 调 close()
    async def closer() -> None:
        await transport.close()

    await asyncio.wait_for(asyncio.create_task(closer()), timeout=1.0)
    await asyncio.wait_for(consumer, timeout=1.0)

    # close() 之后状态被清零、stream 被 stop
    assert transport._queue is None
    assert fake_sd.instances[0].stopped is True  # type: ignore[unreachable]


async def test_close_is_idempotent_before_and_after(
    fake_sd: type[_FakeStream],
) -> None:
    """close() 在 input_stream 未启动 / 已结束时都应是 no-op。"""
    transport = MicrophoneTransport()
    # 启动前
    await transport.close()
    await transport.close()  # 再叫一次也不应报错

    async def consume_one() -> None:
        async for _ in transport.input_stream():
            return

    consumer = asyncio.create_task(consume_one())
    for _ in range(50):
        if fake_sd.instances and transport._queue is not None:
            break
        await asyncio.sleep(0.01)
    # 先 close 让 input_stream 自然退出
    await transport.close()
    await asyncio.wait_for(consumer, timeout=1.0)

    # input_stream 已经收尾了；再 close 仍应 no-op
    assert transport._queue is None
    await transport.close()
    await transport.close()


# ---------------------------------------------------------------------------
# Issue 1: callback floods must not leak QueueFull, and warning is logged
# ---------------------------------------------------------------------------
async def test_callback_overflow_no_queuefull_leak_and_warns(
    fake_sd: type[_FakeStream],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """回调灌得比消费快：旧帧被丢、新帧入列、不抛 QueueFull、且打了 warning。"""
    caplog.set_level(logging.WARNING, logger=mic_mod.log.name)
    transport = MicrophoneTransport(queue_maxsize=2)

    started = asyncio.Event()
    received: list[bytes] = []

    async def consume() -> None:
        # 故意不 drain：让 callback 灌爆 queue
        async for chunk in transport.input_stream():
            started.set()
            received.append(chunk)
            # 第一个收到后挂起，让后续 callback 触发 overflow 路径
            await asyncio.sleep(10.0)

    consumer = asyncio.create_task(consume())

    # 等 fake stream 起来 + queue ready
    for _ in range(50):
        if fake_sd.instances and transport._queue is not None:
            break
        await asyncio.sleep(0.01)
    assert fake_sd.instances, "fake stream not opened"

    cb = fake_sd.instances[0].callback
    # 模拟 PortAudio 线程：直接同步触发回调多次。
    # call_soon_threadsafe 把工作推到 loop 线程；asyncio.sleep(0) 让 loop 处理。
    # 灌足够多的帧把 maxsize=2 撑爆，并触发 _on_loop_enqueue 的 overflow 分支。
    for i in range(10):
        cb(bytes([i, i]) * 10, 10, None, 0)
    # 让 loop 处理所有 call_soon_threadsafe 排进来的回调
    for _ in range(20):
        await asyncio.sleep(0)

    # 起码有过一次成功消费 + 没有任何未捕获异常上浮
    # （consumer 还在 sleep；不应该是 done()，更不应该是 done with exception）
    assert not consumer.done(), (
        f"consumer died with: {consumer.exception() if consumer.done() else None}"
    )
    assert started.is_set(), "consumer never received any frame"

    # 关键：至少打一次 "queue full" 告警
    full_warnings = [
        r for r in caplog.records
        if r.levelno == logging.WARNING and "queue full" in r.getMessage().lower()
    ]
    assert full_warnings, (
        f"expected at least one 'queue full' warning, got: "
        f"{[r.getMessage() for r in caplog.records]}"
    )

    # 收尾：close + 等 consumer 退出
    await transport.close()
    try:
        await asyncio.wait_for(consumer, timeout=1.0)
    except asyncio.TimeoutError:
        consumer.cancel()
        try:
            await consumer
        except (asyncio.CancelledError, BaseException):
            pass


# ---------------------------------------------------------------------------
# Phase 3: output_stream tests
# ---------------------------------------------------------------------------
async def _bytes_iter(blocks: list[bytes]) -> Any:
    for b in blocks:
        yield b


async def _wait_for(predicate: Any, timeout: float = 2.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("predicate never became true")
        await asyncio.sleep(0.01)


async def test_output_stream_plays_bytes_in_order(
    fake_sd_output: type[_FakeOutputStream],
) -> None:
    transport = MicrophoneTransport(
        output_block_size=4, output_queue_maxsize=8,
    )
    payload = bytes(range(16))  # 16 bytes = 8 samples = 2 blocks of 4

    async def driver() -> None:
        # 等 _pump 把 leftover 灌满 + 期间反复 pump callback
        for _ in range(50):
            await asyncio.sleep(0)
            if fake_sd_output.instances:
                fake_sd_output.instances[0].pump()

    out_task = asyncio.create_task(transport.output_stream(_bytes_iter([payload])))
    drv_task = asyncio.create_task(driver())
    await asyncio.wait_for(out_task, timeout=2.0)
    drv_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await drv_task

    inst = fake_sd_output.instances[0]
    assert inst.started is True
    assert inst.stopped is True
    # 前 16 字节应等于 payload；之后的字节都是 underrun 用 0 填的
    assert bytes(inst.played[:16]) == payload


async def test_output_stream_waits_for_playout(
    fake_sd_output: type[_FakeOutputStream],
) -> None:
    """上游 iterator 已耗尽但 leftover 还有数据时，output_stream 不应提前返回。"""
    transport = MicrophoneTransport(
        output_block_size=4, output_queue_maxsize=8,
    )
    payload = bytes([0x33] * 16)  # 4 blocks worth

    out_task = asyncio.create_task(transport.output_stream(_bytes_iter([payload])))

    # 让上游 _pump 跑完，但故意暂时不 pump callback
    await _wait_for(lambda: bool(fake_sd_output.instances))
    inst = fake_sd_output.instances[0]
    # 给 loop 时间把所有 16 字节灌进 leftover
    for _ in range(20):
        await asyncio.sleep(0)
    # 此时 output_stream 不应该已经返回
    assert not out_task.done(), "output_stream returned before audio drained"

    # pump 几次直到 underrun → drained → 任务结束
    for _ in range(10):
        inst.pump()
        await asyncio.sleep(0)
    await asyncio.wait_for(out_task, timeout=1.0)
    assert bytes(inst.played[:16]) == payload


async def test_output_stream_cancellation_stops_stream(
    fake_sd_output: type[_FakeOutputStream],
) -> None:
    transport = MicrophoneTransport(
        output_block_size=4, output_queue_maxsize=8,
    )

    async def slow_audio() -> Any:
        for _ in range(1000):
            await asyncio.sleep(0.05)
            yield b"\x00" * 8

    task = asyncio.create_task(transport.output_stream(slow_audio()))
    await _wait_for(lambda: bool(fake_sd_output.instances))
    inst = fake_sd_output.instances[0]
    assert inst.started is True

    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    assert inst.stopped is True


async def test_close_stops_both_input_and_output(
    fake_sd: type[_FakeStream],
    fake_sd_output: type[_FakeOutputStream],
) -> None:
    transport = MicrophoneTransport(
        output_block_size=4, output_queue_maxsize=8,
    )

    async def slow_audio() -> Any:
        for _ in range(1000):
            await asyncio.sleep(0.05)
            yield b"\x00" * 8

    async def consume_input() -> None:
        async for _ in transport.input_stream():
            pass

    in_task = asyncio.create_task(consume_input())
    out_task = asyncio.create_task(transport.output_stream(slow_audio()))

    await _wait_for(lambda: bool(fake_sd.instances) and bool(fake_sd_output.instances))

    await transport.close()
    # close 必须把两条都结束掉——不能依赖外面再 cancel out_task，否则 close 撑不起
    # Phase 5 barge-in / Phase 4.5 systemd graceful shutdown。
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await asyncio.wait_for(in_task, timeout=2.0)
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await asyncio.wait_for(out_task, timeout=1.0)

    assert in_task.done() is True
    assert out_task.done() is True
    assert fake_sd.instances[0].stopped is True
    assert fake_sd_output.instances[0].stopped is True

    # 幂等：再 close 不应抛
    await transport.close()
    await transport.close()


async def test_close_terminates_output_stream_with_slow_audio(
    fake_sd_output: type[_FakeOutputStream],
) -> None:
    """C1 回归：上游 audio 卡在 ``await asyncio.sleep(60)`` 时，``close()`` 必须
    cancel pump_task，让 ``output_stream`` 在 1 秒内退出。

    旧实现里 close 只投 sentinel 不 cancel pump，pump 卡在 anext 上永远收不到
    sentinel，``await pump_task`` 永等。
    """
    transport = MicrophoneTransport(
        output_block_size=4, output_queue_maxsize=8,
    )

    first_chunk_consumed = asyncio.Event()

    async def slow_iter() -> Any:
        yield b"\x01" * 8
        first_chunk_consumed.set()
        await asyncio.sleep(60)
        yield b"\x02" * 8  # 永远到不了

    out_task = asyncio.create_task(transport.output_stream(slow_iter()))
    await asyncio.wait_for(first_chunk_consumed.wait(), timeout=1.0)

    await transport.close()
    # 关键：不再外加 out_task.cancel() 兜底，close() 必须自己负责
    await asyncio.wait_for(out_task, timeout=1.0)
    assert out_task.done() is True
    assert fake_sd_output.instances[0].stopped is True


# ---------------------------------------------------------------------------
# Phase 4 — half-duplex AEC gate + log-only pause/resume_outbound
# ---------------------------------------------------------------------------
async def test_half_duplex_gate_drops_input_during_output(
    fake_sd: type[_FakeStream],
) -> None:
    """While ``transport._output_active`` is set (AI is speaking), input frames
    must be dropped at the consumer side; after clear(), new frames must flow
    again (per RESEARCH §"Half-Duplex AEC Gate").
    """
    transport = MicrophoneTransport(queue_maxsize=16)

    received: list[bytes] = []
    consume_started = asyncio.Event()

    async def consume() -> None:
        async for chunk in transport.input_stream():
            consume_started.set()
            received.append(chunk)

    consumer = asyncio.create_task(consume())

    # 等 fake stream 起来
    for _ in range(50):
        if fake_sd.instances and transport._queue is not None:
            break
        await asyncio.sleep(0.01)
    assert fake_sd.instances, "fake stream not opened"
    cb = fake_sd.instances[0].callback

    # Phase 1: 先确认正常 frame 能流过(gate 还没 set)
    cb(b"\x11" * 960, 480, None, 0)
    for _ in range(20):
        if received:
            break
        await asyncio.sleep(0.01)
    assert received, "input_stream should yield before gate is set"
    pre_gate_count = len(received)

    # Phase 2: set output_active gate, frames should be dropped
    transport._output_active.set()
    for _ in range(5):
        cb(b"\x22" * 960, 480, None, 0)
    # 让 loop 处理这些 call_soon_threadsafe
    for _ in range(30):
        await asyncio.sleep(0)
    assert len(received) == pre_gate_count, (
        f"expected no frames during gate-active, but got "
        f"{len(received) - pre_gate_count} extra"
    )

    # Phase 3: clear gate, frames should flow again
    transport._output_active.clear()
    cb(b"\x33" * 960, 480, None, 0)
    for _ in range(50):
        if len(received) > pre_gate_count:
            break
        await asyncio.sleep(0.01)
    assert len(received) > pre_gate_count, (
        "frame should flow after gate cleared"
    )
    # 最新一帧应该是清 gate 之后的(0x33 而非 0x22)
    assert received[-1] == b"\x33" * 960

    # 收尾
    await transport.close()
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await asyncio.wait_for(consumer, timeout=1.0)


async def test_output_stream_sets_and_clears_output_active(
    fake_sd_output: type[_FakeOutputStream],
) -> None:
    """``output_stream`` must set ``_output_active`` on first non-empty chunk
    and schedule a 150ms tail-clear in finally.
    """
    transport = MicrophoneTransport(
        output_block_size=4, output_queue_maxsize=8,
    )
    payload = bytes(range(16))

    assert not transport._output_active.is_set()

    async def driver() -> None:
        for _ in range(50):
            await asyncio.sleep(0.01)
            if fake_sd_output.instances:
                fake_sd_output.instances[0].pump()

    out_task = asyncio.create_task(transport.output_stream(_bytes_iter([payload])))
    drv_task = asyncio.create_task(driver())

    # 等到 output_active 被 set(意味着第一个非空 chunk 已经 enqueue)
    for _ in range(100):
        if transport._output_active.is_set():
            break
        await asyncio.sleep(0.01)
    assert transport._output_active.is_set(), (
        "output_active must be set on first non-empty output chunk"
    )

    await asyncio.wait_for(out_task, timeout=2.0)
    drv_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await drv_task

    # finally 调度了 150ms tail clear task。等过 tail window。
    await asyncio.sleep(0.20)
    assert not transport._output_active.is_set(), (
        "output_active must be cleared after 150ms tail window"
    )


# Alias to the exact pytest node ID 04-VALIDATION.md row references
# ('tests/test_microphone.py::test_half_duplex_gate'). Mirrors the
# test_dialogue_state.py::test_readiness_schema alias pattern.
async def test_half_duplex_gate(fake_sd: type[_FakeStream]) -> None:
    """Alias of test_half_duplex_gate_drops_input_during_output so the exact
    pytest node ID 04-VALIDATION.md references resolves verbatim."""
    await test_half_duplex_gate_drops_input_during_output(fake_sd)


# ---------------------------------------------------------------------------
# Phase 4 Plan 04 — webrtcvad client-side EOS detection
# ---------------------------------------------------------------------------
async def test_vad_eos_fires_callback_after_silence(
    fake_sd: type[_FakeStream],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After 9-of-10 voiced frames triggers VAD, then 9-of-10 unvoiced frames
    fires _on_eos exactly once (per RESEARCH §"webrtcvad Client EOS")."""
    transport = MicrophoneTransport(queue_maxsize=64)

    eos_calls: list[float] = []

    async def on_eos() -> None:
        eos_calls.append(time.monotonic())

    transport._on_eos = on_eos

    # Patch is_speech 为可脚本化:每次调用按 voiced_pattern 顺序返回。
    voiced_pattern: list[bool] = []

    def fake_is_speech(audio: bytes, sample_rate: int) -> bool:
        if voiced_pattern:
            return voiced_pattern.pop(0)
        return False

    monkeypatch.setattr(transport._vad, "is_speech", fake_is_speech)

    received: list[bytes] = []

    async def consume() -> None:
        async for chunk in transport.input_stream():
            received.append(chunk)

    consumer = asyncio.create_task(consume())

    for _ in range(50):
        if fake_sd.instances and transport._queue is not None:
            break
        await asyncio.sleep(0.01)
    cb = fake_sd.instances[0].callback

    # Step 1: 灌 10 个 voiced 帧 → NOTTRIGGERED → TRIGGERED
    voiced_pattern.extend([True] * 10)
    for _ in range(10):
        cb(b"\x11" * 960, 480, None, 0)
    for _ in range(40):
        await asyncio.sleep(0.01)
        if len(received) >= 10:
            break
    assert transport._vad_state == "TRIGGERED", (
        f"expected TRIGGERED after 10 voiced frames, got {transport._vad_state}"
    )
    assert eos_calls == [], "EOS should not fire on TRIGGER transition"

    # Step 2: 灌 10 个 unvoiced 帧 → TRIGGERED → NOTTRIGGERED → fire EOS
    voiced_pattern.extend([False] * 10)
    for _ in range(10):
        cb(b"\x00" * 960, 480, None, 0)
    for _ in range(80):
        await asyncio.sleep(0.01)
        if eos_calls:
            break
    assert len(eos_calls) == 1, (
        f"expected exactly 1 EOS callback, got {len(eos_calls)}"
    )
    assert transport._vad_state == "NOTTRIGGERED"

    await transport.close()
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await asyncio.wait_for(consumer, timeout=1.0)


async def test_vad_chunk_size_padding(
    fake_sd: type[_FakeStream],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """webrtcvad demands exactly 480 / 960 / 1440 samples — Pitfall 6 says we
    must pad/trim off-spec chunks before is_speech() to avoid RuntimeError."""
    transport = MicrophoneTransport()

    chunk_sizes_seen: list[int] = []

    def fake_is_speech(audio: bytes, sample_rate: int) -> bool:
        chunk_sizes_seen.append(len(audio))
        return False

    monkeypatch.setattr(transport._vad, "is_speech", fake_is_speech)

    async def consume() -> None:
        async for _ in transport.input_stream():
            pass

    consumer = asyncio.create_task(consume())

    for _ in range(50):
        if fake_sd.instances and transport._queue is not None:
            break
        await asyncio.sleep(0.01)
    cb = fake_sd.instances[0].callback

    # 灌一个 478-sample (956-byte) 短帧 + 一个 482-sample (964-byte) 长帧
    cb(b"\x00" * 956, 478, None, 0)
    cb(b"\x00" * 964, 482, None, 0)
    cb(b"\x00" * 960, 480, None, 0)  # exact

    for _ in range(50):
        await asyncio.sleep(0.01)
        if len(chunk_sizes_seen) >= 3:
            break

    # 全部应该 pad/trim 到正好 960
    assert all(sz == 960 for sz in chunk_sizes_seen), (
        f"all VAD inputs must be 960 bytes, got {chunk_sizes_seen}"
    )

    await transport.close()
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await asyncio.wait_for(consumer, timeout=1.0)


async def test_vad_state_resets_under_output_gate(
    fake_sd: type[_FakeStream],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the half-duplex gate is set, VAD state must be reset to
    NOTTRIGGERED and the ring buffer cleared so post-output voicing does not
    inherit pre-output partial-trigger state."""
    transport = MicrophoneTransport(queue_maxsize=64)

    monkeypatch.setattr(transport._vad, "is_speech", lambda audio, sr: True)

    async def consume() -> None:
        async for _ in transport.input_stream():
            pass

    consumer = asyncio.create_task(consume())

    for _ in range(50):
        if fake_sd.instances and transport._queue is not None:
            break
        await asyncio.sleep(0.01)
    cb = fake_sd.instances[0].callback

    # Set the gate FIRST, then push voiced frames — they should be dropped
    # at the half-duplex gate before reaching VAD; state must remain
    # NOTTRIGGERED and ring must be empty.
    transport._output_active.set()
    for _ in range(15):
        cb(b"\x11" * 960, 480, None, 0)
    for _ in range(30):
        await asyncio.sleep(0.01)

    assert transport._vad_state == "NOTTRIGGERED"
    assert len(transport._vad_buffer) == 0, (
        f"VAD ring must be empty under gate, got {list(transport._vad_buffer)}"
    )

    await transport.close()
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await asyncio.wait_for(consumer, timeout=1.0)


async def test_vad_eos_with_late_registration_no_crash(
    fake_sd: type[_FakeStream],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If frames arrive before _on_eos is registered, the input_stream must
    not crash on `await self._on_eos()` — guard the call with `is not None`.
    Then late-register the handler and verify it fires on the next EOS."""
    transport = MicrophoneTransport(queue_maxsize=64)
    assert transport._on_eos is None  # construct-time invariant

    voiced_pattern: list[bool] = []

    def fake_is_speech(audio: bytes, sample_rate: int) -> bool:
        if voiced_pattern:
            return voiced_pattern.pop(0)
        return False

    monkeypatch.setattr(transport._vad, "is_speech", fake_is_speech)

    async def consume() -> None:
        async for _ in transport.input_stream():
            pass

    consumer = asyncio.create_task(consume())

    for _ in range(50):
        if fake_sd.instances and transport._queue is not None:
            break
        await asyncio.sleep(0.01)
    cb = fake_sd.instances[0].callback

    # Phase A: NO _on_eos registered; trigger a full voiced→unvoiced cycle.
    # Must not raise AttributeError / TypeError.
    voiced_pattern.extend([True] * 10 + [False] * 10)
    for _ in range(20):
        cb(b"\x00" * 960, 480, None, 0)
    for _ in range(50):
        await asyncio.sleep(0.01)
        if transport._vad_state == "NOTTRIGGERED" and not voiced_pattern:
            break

    # Consumer must still be alive (no crash)
    assert not consumer.done() or consumer.exception() is None, (
        f"consumer crashed without _on_eos: "
        f"{consumer.exception() if consumer.done() else None}"
    )

    # Phase B: late-register handler; fire another full cycle; handler called.
    eos_calls: list[float] = []

    async def on_eos() -> None:
        eos_calls.append(time.monotonic())

    transport._on_eos = on_eos
    voiced_pattern.extend([True] * 10 + [False] * 10)
    for _ in range(20):
        cb(b"\x00" * 960, 480, None, 0)
    for _ in range(80):
        await asyncio.sleep(0.01)
        if eos_calls:
            break

    assert len(eos_calls) == 1, (
        f"late-registered handler should fire exactly once; got {len(eos_calls)}"
    )

    await transport.close()
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await asyncio.wait_for(consumer, timeout=1.0)


async def test_vad_pop_speech_end_ts(
    fake_sd: type[_FakeStream],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """VAD EOS must stamp wall-clock onto a poppable accessor so the pipeline
    can wire it into TurnTiming.last_speech_end_real."""
    transport = MicrophoneTransport(queue_maxsize=64)

    voiced_pattern: list[bool] = [True] * 10 + [False] * 10

    def fake_is_speech(audio: bytes, sample_rate: int) -> bool:
        if voiced_pattern:
            return voiced_pattern.pop(0)
        return False

    monkeypatch.setattr(transport._vad, "is_speech", fake_is_speech)

    assert transport.pop_speech_end_ts() is None  # initial

    async def consume() -> None:
        async for _ in transport.input_stream():
            pass

    consumer = asyncio.create_task(consume())

    for _ in range(50):
        if fake_sd.instances and transport._queue is not None:
            break
        await asyncio.sleep(0.01)
    cb = fake_sd.instances[0].callback

    t_before = time.monotonic()
    for _ in range(20):
        cb(b"\x00" * 960, 480, None, 0)
    for _ in range(80):
        await asyncio.sleep(0.01)
        if not voiced_pattern:
            break
    # 让 EOS callback 完整跑一轮
    for _ in range(10):
        await asyncio.sleep(0.01)

    ts = transport.pop_speech_end_ts()
    t_after = time.monotonic()
    assert ts is not None, "pop_speech_end_ts must return a value after VAD EOS"
    assert t_before <= ts <= t_after
    # one-shot: second pop returns None
    assert transport.pop_speech_end_ts() is None

    await transport.close()
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await asyncio.wait_for(consumer, timeout=1.0)


async def test_pause_resume_outbound_logs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``pause_outbound()`` and ``resume_outbound()`` are no-ops in Phase 4 for
    MicrophoneTransport (the abstraction belongs to TwilioTransport in
    Phase 5); they must succeed (return None) and emit an INFO-level log
    record matching 'no-op for MicrophoneTransport'.
    """
    caplog.set_level(logging.INFO, logger=mic_mod.log.name)
    transport = MicrophoneTransport()

    pause_result = await transport.pause_outbound()
    assert pause_result is None
    pause_records = [
        r for r in caplog.records
        if "pause_outbound" in r.getMessage() and r.levelno == logging.INFO
    ]
    assert pause_records, (
        f"expected pause_outbound INFO log, got: "
        f"{[r.getMessage() for r in caplog.records]}"
    )
    assert any(
        "no-op for MicrophoneTransport" in r.getMessage()
        for r in pause_records
    )

    caplog.clear()
    resume_result = await transport.resume_outbound()
    assert resume_result is None
    resume_records = [
        r for r in caplog.records
        if "resume_outbound" in r.getMessage() and r.levelno == logging.INFO
    ]
    assert resume_records, (
        f"expected resume_outbound INFO log, got: "
        f"{[r.getMessage() for r in caplog.records]}"
    )
