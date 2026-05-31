"""本地 mic / 扬声器 transport（Mac 开发用）— Phase 3 完成双向音频。

依赖 ``sounddevice``（PortAudio binding）。

输入路径（Phase 1）：每次 ``input_stream()`` 起一个 ``RawInputStream``，回调里把
PCM int16 帧扔进 asyncio.Queue，让协程消费。

输出路径（Phase 3）：``output_stream(audio)`` 起一个 ``RawOutputStream``，从
``audio`` 异步迭代器收 PCM 字节切成定长块灌进 buffer queue；PortAudio 输出回调
（在 PortAudio 线程）从 queue 拿块写硬件。``output_stream`` 必须在所有排队音频
真正播完后才返回——TTS 句末不能被截断。Cancellation（caller 关 audio iterator
或本协程被 cancel）：丢弃未播 buffer、立刻停 stream，barge-in 用得上。

Phase 5 会补 VAD 触发的 barge-in；Phase 3 仅保证机制上可被打断（``close()`` 也
会停输出 stream）。
"""
from __future__ import annotations

import asyncio
import collections
import logging
import threading
import time
from collections.abc import AsyncIterator, Awaitable
from typing import Callable, Literal

import sounddevice as sd

try:
    import webrtcvad
except ImportError:  # pragma: no cover - install-time fallback
    # macOS arm64 wheels for ``webrtcvad`` are intermittently missing on PyPI;
    # ``webrtcvad-wheels`` packages an arm64-compatible build under the same
    # module name. If neither is importable we let the exception bubble — the
    # transport cannot run client-side VAD without it (Phase 4 D-01 / STT EOS).
    raise

from vocalize.transports.base import AudioEncoding

log = logging.getLogger(__name__)

# Provider API 输入默认使用 16 kHz mono PCM int16。
DEFAULT_SAMPLE_RATE = 16_000
DEFAULT_CHANNELS = 1
# 30 ms 帧 → 480 samples × 2 bytes = 960 bytes，常见 STT 流式块大小
DEFAULT_BLOCK_SIZE = 480

# Provider API 输出默认使用 24 kHz mono PCM int16。
DEFAULT_OUTPUT_SAMPLE_RATE = 24_000
# 20 ms 帧 @ 24kHz = 480 samples × 2 bytes = 960 bytes。块越小启动延迟越低，
# 但 PortAudio underrun 风险越高；20 ms 是常见 voice agent 折中。
DEFAULT_OUTPUT_BLOCK_SIZE = 480


class MicrophoneTransport:
    """本地麦克风 transport。

    实现 ``AudioTransport`` 协议的 ``input_stream`` / ``output_stream`` / ``close``。

    Args:
        device: ``sounddevice`` 输入设备名或索引；``None`` = 系统默认输入。
        sample_rate: 输入采样率，默认 16 kHz（Provider API 默认输入格式）。
        block_size: 输入回调每次返回的样本数；30 ms @ 16 kHz = 480。
        queue_maxsize: 输入 queue 容量；满了会丢最老帧并告警（避免无界增长）。
        output_device: ``sounddevice`` 输出设备名或索引；``None`` = 系统默认输出。
        output_sample_rate: 输出采样率，默认 24 kHz（Provider API 默认输出格式）。
        output_block_size: 输出回调每次消费的样本数；20 ms @ 24 kHz = 480。
            块越小启动延迟越低，但 PortAudio underrun 风险越高。
        output_queue_maxsize: 输出 queue 容量；默认 200（约 2 秒缓冲 @ 20ms 块）。
            比输入大是因为 TTS 输出更突发——一句合成 50-200 块 burst 进来再慢慢播。
    """

    sample_rate: int
    channels: int
    encoding: AudioEncoding

    def __init__(
        self,
        device: str | int | None = None,
        sample_rate: int = DEFAULT_SAMPLE_RATE,
        block_size: int = DEFAULT_BLOCK_SIZE,
        queue_maxsize: int = 100,
        output_device: str | int | None = None,
        output_sample_rate: int = DEFAULT_OUTPUT_SAMPLE_RATE,
        output_block_size: int = DEFAULT_OUTPUT_BLOCK_SIZE,
        output_queue_maxsize: int = 200,
    ) -> None:
        self.device = device
        self.sample_rate = sample_rate
        self.channels = DEFAULT_CHANNELS
        self.encoding = "pcm_s16le"
        self.block_size = block_size
        self.queue_maxsize = queue_maxsize
        self.output_device = output_device
        self.output_sample_rate = output_sample_rate
        self.output_block_size = output_block_size
        self.output_queue_maxsize = output_queue_maxsize
        self._stream: sd.RawInputStream | None = None
        # 队列升到实例属性：close() 才能把 sentinel 推进去叫醒 input_stream()。
        # None = input_stream() 还没启动 / 已结束（close() 此时是 no-op）。
        self._queue: asyncio.Queue[bytes | None] | None = None
        # 输出侧镜像结构。``_output_queue`` 持 bytes 块（None = 上游结束哨兵）。
        self._output_stream: sd.RawOutputStream | None = None
        self._output_queue: asyncio.Queue[bytes | None] | None = None
        self._output_drained: asyncio.Event | None = None
        # 把 pump / drain 任务也提到实例层：close() 才能在 pump 卡在慢上游
        # iterator 的 __anext__ 时直接 cancel 它（否则 await pump_task 永远不返）。
        self._output_pump_task: asyncio.Task[None] | None = None
        self._output_drain_task: asyncio.Task[None] | None = None

        # Phase 4 Wave 1 instrumentation (CONCERNS.md "Instrumentation gap"
        # — closing the 12s gap between objective stopwatch and measured e2e):
        # ``_last_first_audible_ts`` is wall-clock (monotonic) captured inside
        # the PortAudio output callback the FIRST time a non-zero sample is
        # written to hardware per output_stream() session. The callback runs on
        # a non-asyncio thread, so writes go through ``_last_first_audible_lock``;
        # consumer (pipeline._handle_turn) calls ``pop_first_audible_ts()`` once
        # per turn after output_stream returns.
        self._last_first_audible_ts: float | None = None
        self._last_first_audible_lock = threading.Lock()
        # ``_output_queue_depth_at_first_audio`` records the depth of the
        # bounded ``_output_queue`` at the moment _pump enqueues the first
        # non-empty chunk for an utterance. Validates CONCERNS.md hypothesis #1
        # (queue buffering accumulates before the first audible sample emerges).
        self._output_queue_depth_at_first_audio: int | None = None

        # Phase 4 D-01 — half-duplex AEC gate. ``_output_active`` is set on the
        # first non-empty output chunk in ``output_stream`` and cleared after
        # a 150ms tail window in the finally block (Q-SYS commercial AEC
        # reference). Consumer side of ``input_stream`` drops frames while the
        # event is set and resets VAD state to NOTTRIGGERED so post-output
        # voicing does not inherit pre-output partial state.
        self._output_active: asyncio.Event = asyncio.Event()
        self._output_tail_clear_task: asyncio.Task[None] | None = None

        # Phase 4 Plan 04-04 — webrtcvad client-side EOS detection.
        # RESEARCH §"Configuration recommendation": mode=2, 30ms frames,
        # 10-frame ring (300ms padding), 9-of-10 trigger thresholds.
        # State machine: NOTTRIGGERED → (≥9 voiced in last 10) → TRIGGERED →
        #                (≥9 unvoiced in last 10) → NOTTRIGGERED + fire _on_eos.
        # ``_on_eos`` is None at construct time so the input_stream consumer
        # is born tolerant of late registration (the STT provider hooks it
        # up at the start of stream_transcribe, BEFORE iterating input_stream;
        # the guard `if self._on_eos is not None` is a defensive backstop).
        self._vad = webrtcvad.Vad(mode=2)
        self._vad_buffer: collections.deque[bool] = collections.deque(maxlen=10)
        self._vad_state: Literal["NOTTRIGGERED", "TRIGGERED"] = "NOTTRIGGERED"
        self._on_eos: Callable[[], Awaitable[None]] | None = None
        # Wall-clock (monotonic) at the most recent VAD-detected EOS; consumed
        # via pop_speech_end_ts() by the pipeline to populate
        # TurnTiming.last_speech_end_real (closes the 11s instrumentation gap
        # documented in .planning/debug/instrumentation-vs-ear-11s-gap.md).
        self._last_speech_end_ts: float | None = None

    async def input_stream(self) -> AsyncIterator[bytes]:
        """开 mic stream，按 block 异步 yield raw PCM 字节。"""
        if self._queue is not None:
            # 同一实例不允许并发开两个 input_stream（会让 close() 不知道叫醒谁）。
            raise RuntimeError("input_stream() is already active on this transport")

        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=self.queue_maxsize)
        self._queue = queue

        def _on_loop_enqueue(chunk: bytes) -> None:
            # 跑在 event loop 线程；asyncio.Queue 的所有读写都集中在这里，线程安全。
            if queue.full():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    # 理论上 full=True 时不该 empty，但并发下 close() 也可能 get_nowait
                    pass
                log.warning("mic queue full; dropped oldest frame")
            try:
                queue.put_nowait(chunk)
            except asyncio.QueueFull:
                # close() 已经塞过 sentinel 占位时可能再次满，丢这一帧即可。
                log.warning("mic queue full; dropping incoming frame")

        def _callback(
            indata: memoryview, frames: int, time_info: object, status: sd.CallbackFlags
        ) -> None:
            # 回调跑在 PortAudio 线程；不能直接 await，必须 call_soon_threadsafe。
            # 队列读写一律 schedule 到 loop 线程，避免 asyncio.Queue 的非线程安全问题。
            if status:
                log.warning("mic stream status: %s", status)
            chunk = bytes(indata)
            loop.call_soon_threadsafe(_on_loop_enqueue, chunk)

        stream = sd.RawInputStream(
            samplerate=self.sample_rate,
            blocksize=self.block_size,
            device=self.device,
            channels=self.channels,
            dtype="int16",
            callback=_callback,
        )
        self._stream = stream
        stream.start()
        log.info(
            "mic started: device=%s rate=%d block=%d",
            self.device, self.sample_rate, self.block_size,
        )
        try:
            while True:
                chunk = await queue.get()
                if chunk is None:  # close() 投的 sentinel
                    break
                # Phase 4 D-01 half-duplex gate: drop input while AI is
                # speaking. Runs on the asyncio consumer side (NOT in the
                # PortAudio callback thread), so the gate's 30ms latency cost
                # is negligible vs the 300ms VAD padding window. The gate
                # also resets VAD state so post-output voicing does not
                # inherit pre-output partial-trigger state.
                if self._output_active.is_set():
                    if self._vad_buffer:
                        self._vad_buffer.clear()
                    self._vad_state = "NOTTRIGGERED"
                    continue

                # Phase 4 Plan 04-04 — webrtcvad consumer-side EOS detection.
                # Pitfall 6: pad/trim to exactly 960 bytes (= 480 samples ×
                # int16) regardless of source. PortAudio normally delivers
                # exact 480-sample blocks, but defensive padding keeps the
                # tests + any future variable-block transports safe.
                if len(chunk) != 960:
                    if len(chunk) < 960:
                        chunk_for_vad = chunk + b"\x00" * (960 - len(chunk))
                    else:
                        chunk_for_vad = chunk[:960]
                else:
                    chunk_for_vad = chunk

                is_voiced = self._vad.is_speech(chunk_for_vad, self.sample_rate)
                self._vad_buffer.append(is_voiced)

                if self._vad_state == "NOTTRIGGERED":
                    # 9-of-10 voiced → user starts speaking. No event fires;
                    # frames continue flowing to STT.
                    if sum(self._vad_buffer) >= 9:
                        self._vad_state = "TRIGGERED"
                else:  # TRIGGERED
                    # 9-of-10 unvoiced → user stopped speaking. Stamp wall-
                    # clock for pipeline.TurnTiming.last_speech_end_real
                    # (closes the 11s instrumentation gap), invoke _on_eos
                    # callback if registered (the STT provider sends
                    # {"event": "end_of_utterance"} over WS), reset.
                    if sum(1 for b in self._vad_buffer if not b) >= 9:
                        self._vad_state = "NOTTRIGGERED"
                        self._last_speech_end_ts = time.monotonic()
                        if self._on_eos is not None:
                            try:
                                await self._on_eos()
                            except Exception:
                                log.exception(
                                    "_on_eos callback raised; continuing"
                                )
                        self._vad_buffer.clear()
                yield chunk
        finally:
            try:
                stream.stop()
                stream.close()
            except Exception:
                log.exception("error closing mic stream")
            self._stream = None
            # 解绑队列；之后 close() 即变 no-op，且允许 input_stream() 再次被调用。
            self._queue = None

    async def output_stream(self, audio: AsyncIterator[bytes]) -> None:
        """把 ``audio`` 中的 PCM 字节播到扬声器；播完才返回。

        - 上游 iterator 自然结束 → 等 buffer 排空 → 关 stream → 返回。
        - 协程被 cancel（包括上游 raise / Ctrl-C）→ 丢弃未播 buffer，立刻停 stream。
        """
        if self._output_queue is not None:
            raise RuntimeError(
                "output_stream() is already active on this transport"
            )

        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[bytes | None] = asyncio.Queue(
            maxsize=self.output_queue_maxsize,
        )
        drained = asyncio.Event()
        self._output_queue = queue
        self._output_drained = drained
        # Phase 4 Wave 1: reset per-session instrumentation. _last_first_audible_ts
        # is set by the PortAudio callback the first time a non-zero sample is
        # written; _output_queue_depth_at_first_audio is captured in _pump on the
        # first non-empty chunk. Both are consumed (and reset) by pop_*() on the
        # caller side after output_stream() returns.
        with self._last_first_audible_lock:
            self._last_first_audible_ts = None
        self._output_queue_depth_at_first_audio = None
        # 给回调用：累积上游数据但还没切到块对齐的"半截"PCM
        leftover = bytearray()
        # 标志位：上游已发结束哨兵；callback 只有在排空且 leftover 为空才置 drained
        upstream_done = False
        bytes_per_block = self.output_block_size * 2  # int16 = 2 bytes/sample

        def _signal_drained() -> None:
            drained.set()

        def _callback(
            outdata: memoryview, frames: int, time_info: object,
            status: sd.CallbackFlags,
        ) -> None:
            # 跑在 PortAudio 线程：不能 await，不能直接读 asyncio.Queue。
            # 只读 leftover（受 GIL 保护的本地 bytearray），消费它喂硬件；
            # 真正补 leftover 由 loop 线程的 _pump 协程做。
            nonlocal leftover
            if status:
                log.warning("speaker stream status: %s", status)
            need = frames * 2
            wrote_audio = False  # tracks whether any sample copied here is non-zero
            if len(leftover) >= need:
                outdata[:need] = leftover[:need]
                # Phase 4 Wave 1 t_first_audible probe: only count this write as
                # "audible" if the bytes we copied have any non-zero sample.
                # This is the cheapest reliable check inside the PortAudio hot
                # path (~0.04ms per 480-sample int16 block on M-class CPUs).
                wrote_audio = any(leftover[:need])
                del leftover[:need]
            else:
                # underrun：用零填充剩余，避免咔哒声
                got = len(leftover)
                if got:
                    outdata[:got] = leftover[:]
                    # Same check on partial-leftover writes — even underruns
                    # can carry the first audible sample if leftover is short
                    # but non-zero (e.g. provider emitted 200 bytes total).
                    wrote_audio = any(leftover[:got])
                    leftover.clear()
                outdata[got:need] = b"\x00" * (need - got)
                if upstream_done:
                    # 已经放完所有上游数据 → 通知 output_stream 可以收尾
                    loop.call_soon_threadsafe(_signal_drained)
            # Phase 4 Wave 1: capture wall-clock the first time a non-zero
            # sample is actually written to hardware this session. Lock-protected
            # because the PortAudio callback runs off-loop; pop_first_audible_ts()
            # reads under the same lock.
            if wrote_audio and self._last_first_audible_ts is None:
                with self._last_first_audible_lock:
                    if self._last_first_audible_ts is None:
                        self._last_first_audible_ts = time.monotonic()

        stream = sd.RawOutputStream(
            samplerate=self.output_sample_rate,
            blocksize=self.output_block_size,
            device=self.output_device,
            channels=self.channels,
            dtype="int16",
            callback=_callback,
        )
        self._output_stream = stream
        stream.start()
        log.info(
            "speaker started: device=%s rate=%d block=%d",
            self.output_device, self.output_sample_rate, self.output_block_size,
        )

        async def _pump() -> None:
            """从 audio iterator 拉字节、切块入 queue、再喂 leftover。

            把上游解耦成 queue → leftover 两级缓冲：queue 受 maxsize 限制做
            back-pressure；leftover 给 PortAudio 回调直接读，避免在回调里跨线程
            访问 asyncio.Queue。
            """
            nonlocal upstream_done
            first_chunk_seen = False
            try:
                async for chunk in audio:
                    if not chunk:
                        continue
                    # Phase 4 Wave 1: capture queue depth on the first non-empty
                    # upstream chunk for this output session. Reading
                    # ``queue.qsize()`` is cheap and accurate enough for
                    # offline analysis (no real-time ordering guarantees needed).
                    if not first_chunk_seen:
                        first_chunk_seen = True
                        self._output_queue_depth_at_first_audio = queue.qsize()
                        # Phase 4 D-01 half-duplex gate: arm on first non-empty
                        # outbound chunk so input_stream starts dropping mic
                        # frames before they collide with what we're about to
                        # play. Idempotent — Event.set() on an already-set
                        # Event is a no-op.
                        self._output_active.set()
                    # 切成 block 大小入 queue（上游可能给任意尺寸）
                    for i in range(0, len(chunk), bytes_per_block):
                        await queue.put(chunk[i:i + bytes_per_block])
            finally:
                await queue.put(None)

        async def _drain_into_leftover() -> None:
            """把 queue 块持续追加到 leftover，直到收到 None 哨兵。"""
            # TODO(phase-4): cap leftover growth — currently this drains the
            # bounded queue into an unbounded ``leftover`` bytearray, so
            # output_queue_maxsize stops being a real backpressure ceiling.
            # In Phase 3 this is bounded by per-utterance audio length
            # (~720 KB max @ 24 kHz × 15 s) so it's safe; once Phase 4
            # introduces persistent output streams across turns, cap leftover
            # at e.g. 4× output_block_size and gate the pump on a
            # leftover-below-threshold event so backpressure flows back to TTS.
            nonlocal upstream_done
            while True:
                item = await queue.get()
                if item is None:
                    upstream_done = True
                    return
                leftover.extend(item)

        pump_task = asyncio.create_task(_pump())
        drain_task = asyncio.create_task(_drain_into_leftover())
        self._output_pump_task = pump_task
        self._output_drain_task = drain_task

        try:
            # 等上游耗尽 + 回调把所有 leftover 播完
            await drain_task
            await drained.wait()
            await pump_task
        except asyncio.CancelledError:
            # 两种来源：(a) 调用方 cancel 本协程；(b) close() 主动 cancel 我们
            # 内部的 pump/drain 任务以唤醒卡住的 await。区分点：current_task()
            # 在 (a) 下被标记为 cancelling，(b) 下没有。
            leftover.clear()
            current = asyncio.current_task()
            if current is not None and current.cancelling() > 0:
                raise
            # close() 路径：吞掉 CancelledError，让 output_stream 正常返回
        finally:
            for t in (pump_task, drain_task):
                if not t.done():
                    t.cancel()
                    try:
                        await t
                    except (asyncio.CancelledError, Exception):
                        pass
            try:
                stream.stop()
                stream.close()
            except Exception:
                log.exception("error closing speaker stream")
            self._output_stream = None
            self._output_queue = None
            self._output_drained = None
            self._output_pump_task = None
            self._output_drain_task = None

            # Phase 4 D-01 half-duplex tail window: schedule a delayed clear
            # of _output_active 150ms after stream close. The tail covers two
            # things: (1) any residual reverb/echo bouncing back through the
            # mic after the speaker stops, and (2) the open-air feedback
            # round-trip on the dual-mic Mac demo. Q-SYS commercial AEC tail
            # reference says 150ms is enough for typical room acoustics.
            # Only arm the tail clear if the gate actually fired (something
            # non-empty was sent) — otherwise we leave the (already-clear)
            # Event alone and skip scheduling a useless task.
            if self._output_active.is_set():
                # Cancel any pending tail-clear from a prior session before
                # scheduling a fresh one (idempotent across re-entrant calls).
                if (
                    self._output_tail_clear_task is not None
                    and not self._output_tail_clear_task.done()
                ):
                    self._output_tail_clear_task.cancel()

                async def _clear_after_tail() -> None:
                    try:
                        await asyncio.sleep(0.150)
                        self._output_active.clear()
                    except asyncio.CancelledError:
                        # close() may cancel us before the sleep elapses; in
                        # that case clear immediately so the next mic stream
                        # is not stuck in the gate.
                        self._output_active.clear()
                        raise

                self._output_tail_clear_task = asyncio.create_task(
                    _clear_after_tail()
                )

    async def close(self) -> None:
        """优雅停止：通知 input_stream() 退出 + 关 PortAudio stream。

        幂等：可以反复调用；input_stream() 还没起或已自然结束时也不会报错。
        线程：必须在持有 queue 的 event loop 上调用（异步方法本来就是这个语义）。
        """
        # 先投 sentinel 唤醒 input_stream() 的 await queue.get()。
        # 用 instance 上的 queue ref，跨任务也能拿到。
        queue = self._queue
        if queue is not None:
            try:
                queue.put_nowait(None)
            except asyncio.QueueFull:
                # 满了：丢一帧腾位置再塞 sentinel；保证消费侧一定醒。
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
                try:
                    queue.put_nowait(None)
                except asyncio.QueueFull:
                    # 极端竞态：让一步，下次再说。input_stream() 的 finally
                    # 里 stream.stop() 也会让回调侧不再灌新数据。
                    log.warning("close(): queue still full after eviction")
        # 再停 PortAudio stream；input_stream() 的 finally 会真正 close()。
        # 这里直接 stop 即可，避免和 finally 里的 close() 重复释放。
        if self._stream is not None:
            try:
                self._stream.stop()
            except Exception:
                log.exception("error stopping mic stream")

        # 输出侧同理：投 sentinel 唤醒 output_stream，并 stop PortAudio output。
        out_q = self._output_queue
        if out_q is not None:
            try:
                out_q.put_nowait(None)
            except asyncio.QueueFull:
                try:
                    out_q.get_nowait()
                except asyncio.QueueEmpty:
                    pass
                try:
                    out_q.put_nowait(None)
                except asyncio.QueueFull:
                    log.warning("close(): output queue still full after eviction")
        # 让 output_stream 不要等 buffer 自然排空
        if self._output_drained is not None:
            self._output_drained.set()
        # 直接 cancel pump / drain 任务：sentinel + drained.set() 只能唤醒 _drain
        # 和主等待，但 _pump 可能正悬在上游 audio iterator 的 __anext__ 上
        # （比如慢 TTS / 网络流），那种情况下哨兵进不来、await pump_task 会永等。
        # cancel 是唯一通用的唤醒手段；幂等（重复 cancel 一个 done 任务无副作用）。
        for t in (self._output_pump_task, self._output_drain_task):
            if t is not None and not t.done():
                t.cancel()
        if self._output_stream is not None:
            try:
                self._output_stream.stop()
            except Exception:
                log.exception("error stopping speaker stream")

        # Phase 4 D-01：close() 同时取消 pending tail-clear task。如果不取消，
        # 一个仍在 sleep(0.150) 的 task 会在 transport 已关闭后才 clear gate，
        # 但本身不会泄漏(无对外可观察副作用)；取消纯粹是为了 graceful shutdown
        # 的"无遗留 task"语义。clear() 仍然显式调用一次以保证下次 input_stream
        # 不卡在 gate 上。
        if (
            self._output_tail_clear_task is not None
            and not self._output_tail_clear_task.done()
        ):
            self._output_tail_clear_task.cancel()
        self._output_active.clear()

    # ------------------------------------------------------------------
    # Phase 4 D-04 — pause/resume outbound audio.
    #
    # MicrophoneTransport shares one speaker between user and merchant in
    # the local demo, so "hold filler" has no meaning here — both methods
    # log at INFO and return immediately. The v2 telephony transport will
    # actually flip a flag that swaps the outbound media to a filler audio
    # source. clarification.py (Plan 08) drives both hooks unconditionally;
    # Protocol-level declaration (in transports/base.py) means callers do
    # not need hasattr-gate or try/except around them.
    # ------------------------------------------------------------------
    async def pause_outbound(self) -> None:
        log.info(
            "pause_outbound (no-op for MicrophoneTransport — "
            "v2 telephony transport will hook hold-filler audio here)"
        )

    async def resume_outbound(self) -> None:
        log.info(
            "resume_outbound (no-op for MicrophoneTransport)"
        )

    # ------------------------------------------------------------------
    # Phase 4 Wave 1 instrumentation accessors (CONCERNS.md "Instrumentation
    # gap"). One-shot pop semantics — caller (pipeline._handle_turn) reads the
    # value once after output_stream() returns and the next session resets
    # state when output_stream() opens. Both methods are idempotent: calling
    # twice without an intervening session returns None on the second call.
    # ------------------------------------------------------------------
    def pop_first_audible_ts(self) -> float | None:
        """Return the wall-clock (monotonic) timestamp at which the PortAudio
        output callback first wrote a non-zero sample to hardware for the
        most recent ``output_stream()`` session, then reset to ``None``.

        Returns ``None`` if no non-zero sample was ever written (e.g. silent
        synthesis or output_stream cancelled before any audible bytes flowed).
        """
        with self._last_first_audible_lock:
            ts = self._last_first_audible_ts
            self._last_first_audible_ts = None
        return ts

    def pop_speech_end_ts(self) -> float | None:
        """Return the wall-clock (monotonic) timestamp of the most recent
        VAD-detected end-of-speech, then reset to ``None``.

        One-shot pop semantics matching the other Wave 1 instrumentation
        accessors. The pipeline calls this in ``_handle_turn`` after STT yields
        a final transcript and writes the result into
        ``TurnTiming.last_speech_end_real``. When the pipeline's
        ``effective_speech_end`` property finds this set, ``e2e_perceived`` and
        ``stt_finalize`` both pick it up over ``last_partial_at`` (closing the
        ~11s instrumentation gap from
        .planning/debug/instrumentation-vs-ear-11s-gap.md).

        Returns ``None`` if the consumer never reached a TRIGGERED→NOTTRIGGERED
        transition since the last pop (e.g. user said nothing this turn, or
        STT finalized before the VAD ring filled).
        """
        ts = self._last_speech_end_ts
        self._last_speech_end_ts = None
        return ts

    def pop_queue_depth_at_first_audio(self) -> int | None:
        """Return the depth of the bounded output queue at the moment the
        first non-empty upstream chunk was enqueued in the most recent
        ``output_stream()`` session, then reset to ``None``.

        Validates CONCERNS.md hypothesis #1 (queue buffering hides latency
        between TTS first-byte and audible playback). Returns ``None`` if no
        chunk was ever enqueued.
        """
        depth = self._output_queue_depth_at_first_audio
        self._output_queue_depth_at_first_audio = None
        return depth
