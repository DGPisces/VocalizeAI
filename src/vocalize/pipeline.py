"""VoicePipeline — Phase 3 完成的本地双工语音管道。

把 STT / LLM / TTS 三块流式服务和 transport 串成一个可运行的对话循环：

    transport.input_stream() ──► STT.stream_transcribe ──► final transcripts
                                                                │
                                                                ▼
              LLM.stream_chat ──► segment by sentence punct ──► TTS.stream_synthesize
                                                                │
                                                                ▼
                                                  transport.output_stream

设计与生产-消费拓扑：
- 一个 ``run()`` 协程驱动外层"用户说话→AI 回话→播音"的串行循环；
- 每一轮内部并发：LLM 文本流和 TTS 合成 / 播音同步进行——LLM 还在 yield 的时候，
  TTS 已经能拿到首句开始合成，扬声器就能开始播第一段音频。这是 e2e<2.5s 的关键。
- 段切割发生在 LLM→TTS 之间：按 ``。！？.!?\n`` 切句子边界，最后一段标
  ``is_final_segment=True`` 触发 CosyVoice flush。
- back-pressure 自然存在：TTS 输入是 asyncio.Queue（bounded），TTS 输出是 PortAudio
  buffer；任一侧慢都会反压回 LLM token 拉取速度。

Cancellation：
- 调用方 ``cancel(run_task)`` 或 Ctrl-C 进 ``KeyboardInterrupt`` → finally 关
  transport / STT iter / 当前 turn 任务。
- **Phase 3 不实现 VAD-driven barge-in**——但 TTS 合成 + 播音任务整体可被 cancel，
  Phase 5 可以在用户讲话时直接 cancel 这个任务实现打断。
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import Literal

from vocalize.llm.base import (
    ChatMessage,
    FinishChunk,
    LLMChunk,
    LLMService,
    TextDelta,
    ToolCallDelta,
)
from vocalize.stt.base import STTService
from vocalize.transports.base import AudioTransport
from vocalize.tts.base import TextChunk, TTSService

log = logging.getLogger(__name__)


# 句末标点：触发 TTS 段切割；保留标点本身在送入 TTS 的文本里以保留韵律。
_SENTENCE_ENDERS: frozenset[str] = frozenset("。！？.!?\n")

# `_safe_put` 闭包的签名：接受 TextChunk 段或 None 哨兵 + 可选 ``is_text_chunk``
# 关键字参数（D-13 strict tool round-trip：tool-only chunks 短路为 no-op）。
# Phase 4 Plan 02：``is_text_chunk=False`` 不让 chunk 进队列、不触发 race 检测。
_SafePutFn = Callable[..., Awaitable[None]]

# Tool-call sink 回调：把 streaming ToolCallDelta 累积权交给 orchestrator (Plan 04-09)。
# Default None → 保留 Phase 3 "ignore tool calls" 语义（Pitfall 7：保护 Phase 3 测试）。
_ToolCallSink = Callable[[ToolCallDelta], None]


@dataclass
class TurnTiming:
    """单轮对话的关键耗时（单位：秒）。

    时间起点说明：
    - ``first_partial_at`` / ``last_partial_at`` — 第一个 / 最后一个 partial transcript
      接收时刻；分别近似为"用户开始说话"/"用户停止说话"。
    - ``final_at`` — STT 服务推回 final transcript 的时刻（≈ last_partial + STT
      端点检测 + finalize）。所有下游 timing（ttft_llm / ttft_tts / e2e）以此为起点。
    - 用户真实感知的端到端延迟 = ``stt_finalize + ttft_tts`` (= ``e2e_perceived``)。

    Phase 4 Wave 1 instrumentation (CONCERNS.md "Instrumentation gap"):
    - ``t_first_audible`` — wall-clock (monotonic) at which the PortAudio
      output callback first wrote a non-zero sample to hardware. Closes the
      "first byte enters transport.play" vs "user actually hears sound" gap.
      Captured from ``transport.pop_first_audible_ts()`` after output_stream
      returns; populated only on transports that expose that method (currently
      only MicrophoneTransport — FakeTransport in tests leaves it None).
    - ``last_speech_end_real`` — Wave 1 placeholder for the real VAD-driven
      end-of-speech timestamp. Stays ``None`` in Wave 1 (consumers fall back
      to ``last_partial_at`` via ``effective_speech_end``); Wave 2 wires this
      to the webrtcvad EOS event in microphone.py.
    - ``queue_depth_at_first_audio`` — output-queue depth at the moment the
      first non-empty TTS chunk was enqueued. Validates CONCERNS.md
      hypothesis #1 (queue buffering hides latency).
    """

    user_text: str
    final_at: float
    first_partial_at: float | None = None  # ≈ 用户开始说话
    last_partial_at: float | None = None   # ≈ 用户停止说话（end-of-speech 代理）
    ttft_llm: float | None = None          # final → LLM 第一个 TextDelta
    ttft_tts: float | None = None          # final → 第一段 audio 字节
    e2e: float | None = None               # final → first audio (== ttft_tts in current pipeline)
    # Phase 4 Wave 1 probes — all default None for backward compat with
    # FakeTransport in tests (which lacks pop_first_audible_ts / pop_queue_depth).
    t_first_audible: float | None = None              # monotonic wall-clock; first non-zero PCM written
    last_speech_end_real: float | None = None         # Wave 1 placeholder; Wave 2 wires VAD EOS
    queue_depth_at_first_audio: int | None = None     # output queue depth at first non-empty chunk

    @property
    def stt_finalize(self) -> float | None:
        """从 "用户停止说话"（last_partial）到 STT final 的延迟。

        包含：SenseVoice 端点检测 + finalize + 网络回程。是 STT 部分的最大头。
        """
        if self.last_partial_at is None:
            return None
        return self.final_at - self.last_partial_at

    @property
    def effective_speech_end(self) -> float | None:
        """Phase 4 Wave 1: prefer the real VAD-driven end-of-speech timestamp
        when available (Wave 2+), fall back to ``last_partial_at`` otherwise.

        Wave 1 always returns ``last_partial_at`` because no probe sets
        ``last_speech_end_real`` yet — Wave 2's microphone.py VAD path will.
        """
        return (
            self.last_speech_end_real
            if self.last_speech_end_real is not None
            else self.last_partial_at
        )

    @property
    def e2e_perceived(self) -> float | None:
        """用户真实感知的端到端延迟：从"说完话"到"听到第一个音"。

        Phase 4 Wave 1 semantics (CONCERNS.md "Instrumentation gap"): if both
        ``t_first_audible`` and ``effective_speech_end`` are set, return
        ``t_first_audible - effective_speech_end`` directly — that's the
        objective stopwatch reading. Otherwise fall back to the legacy
        ``stt_finalize + ttft_tts`` formula (kept for FakeTransport-using tests
        and any transport that does not expose pop_first_audible_ts).
        """
        end = self.effective_speech_end
        if self.t_first_audible is not None and end is not None:
            return self.t_first_audible - end
        # Fallback: pre-Wave-1 formula (TTS-first-byte based).
        if self.stt_finalize is None or self.ttft_tts is None:
            return None
        return self.stt_finalize + self.ttft_tts


@dataclass
class _TurnRunState:
    """一轮的可变状态（让段切割与 TTS 任务共享指针）。

    Phase 4 Plan 02 (D-13 strict tool round-trip) 新增：
    - ``finish_reason`` — 在 _handle_llm_chunk 拿到 FinishChunk 时记录；_handle_turn
      在流末按它分流（"stop" → flush buffer；"tool_calls" → discard buffer，不送 TTS）。
    - ``tool_call_in_progress`` — 第一个 ToolCallDelta 出现时翻成 True；_handle_llm_chunk
      的 TextDelta 分支据此把 ``is_text_chunk=False`` 短路 _safe_put（即使 LLM 在 tool
      call 后还吐了几个 TextDelta，也不会进 TTS 队列）。三层 gate 之一。
    """

    timing: TurnTiming
    pieces: list[str] = field(default_factory=list)
    tts_succeeded: bool = False
    finish_reason: Literal["stop", "tool_calls", "length", "content_filter"] | None = None
    tool_call_in_progress: bool = False
    # Phase 4 Plan 04-03 fix #1 dispatch repair (debug session
    # cosyvoice-batch-dispatch-deadcode):
    # ``pending_first_segment`` stash 第一个 sentence-ender 切出的段；只在确定
    # 后续还有内容时才作为 ``is_final=False`` flush。流末 (_handle_turn) 若发现
    # pending 仍非空 + tail 空 → 把它以 ``is_final=True`` 单帧发出，让 cosyvoice
    # server.py:948-966 的 batch dispatch 路径可以在短回复（"好的。" / "4 位"）
    # 上命中，节省 ~1.5s ttft。多句长回复因 _handle_llm_chunk 在第二个 sentence-ender
    # 来到时就把 pending flush 掉，行为与原来等价（仅单 LLM-chunk 间隔的延迟）。
    pending_first_segment: TextChunk | None = None
    mid_segment_flushed: bool = False


class VoicePipeline:
    """组装 transport + STT + LLM + TTS 的语音对话管道。

    服务接口在 ``run()`` / ``_handle_turn()`` 里仍以 Protocol 契约调用，但
    per-turn 的错误恢复路径目前直接 catch ``SenseVoiceError`` /
    ``LLMServiceError`` / ``CosyVoiceError`` 这三个具体类，所以替换成其他
    实现会绕过 per-turn 兜底。
    TODO(phase-4): introduce ``VoiceServiceError`` base class so pipeline can
    catch generically and accept arbitrary service implementations.

    Args:
        transport: 双向音频 transport（mic + speaker / telephony transport (v2)）。
        stt: STT 服务（``stream_transcribe`` 是 async generator）。
        llm: LLM 服务（``stream_chat`` 是 async generator）。
        tts: TTS 服务（``stream_synthesize`` 是 async generator）。
        system_prompt: LLM system message。
        default_language: 用户首句尚未识别出语言时的兜底（``Config.default_language``）。
    """

    def __init__(
        self,
        transport: AudioTransport,
        stt: STTService,
        llm: LLMService,
        tts: TTSService,
        system_prompt: str,
        default_language: str = "zh",
    ) -> None:
        self._transport = transport
        self._stt = stt
        self._llm = llm
        self._tts = tts
        self._system_prompt = system_prompt
        self._default_language = default_language
        self._messages: list[ChatMessage] = [
            ChatMessage(role="system", content=system_prompt),
        ]
        # Phase 4 Plan 02 — D-13 strict tool round-trip 注入点。Plan 04-09
        # DialogueOrchestrator 会在构造完 VoicePipeline 后把自己的累加器
        # 装上来；Phase 3 用法保持 None → ToolCallDelta 走"忽略 + debug log"
        # 老路径（Pitfall 7：保护 Phase 3 现有测试）。
        self._tool_call_sink: _ToolCallSink | None = None

    @property
    def stt_service(self) -> STTService:
        return self._stt

    @property
    def tts_service(self) -> TTSService:
        return self._tts

    async def run(self) -> None:
        """主对话循环。STT final → LLM → TTS → 扬声器 → 下一轮。

        错误恢复策略不对称：
        - LLM/TTS 错误是 per-turn 的（GPU 偶发 OOM、某个 prompt 触发服务端 bug），
          下一轮用户输入仍可以正常走，所以放在 ``_handle_turn`` 里吞掉。
        - STT 错误意味着拿不到下一句用户输入，整个会话失去意义，所以这里 break
          走 finally 优雅关掉 transport，让上层（systemd / 主进程）决定是否重启。
        """
        from vocalize.stt.sensevoice import SenseVoiceError

        audio_in = self._transport.input_stream()
        # Phase 4 Plan 04-04: pass transport reference into STT so it can
        # register a client-side VAD EOS handler (transport._on_eos) that
        # sends {"event": "end_of_utterance"} over WS the moment webrtcvad
        # detects 9-of-10 unvoiced frames. STTService Protocol still accepts
        # a single positional argument; the ``transport`` kwarg is concrete
        # to SenseVoiceClient — we feature-detect it to keep alternative STT
        # implementations (and tests using a fake STT) working without
        # changes.
        try:
            stt_iter = self._stt.stream_transcribe(  # type: ignore[call-arg]
                audio_in, transport=self._transport,
            )
        except TypeError:
            # STT impl doesn't accept the transport kwarg → legacy path,
            # client-side VAD EOS will be unavailable for this STT.
            stt_iter = self._stt.stream_transcribe(audio_in)
        try:
            try:
                first_partial_at: float | None = None
                last_partial_at: float | None = None
                async for transcript in stt_iter:
                    if not transcript.is_final:
                        log.debug("[partial] %s", transcript.text)
                        now = time.monotonic()
                        if first_partial_at is None:
                            first_partial_at = now
                        last_partial_at = now
                        continue
                    user_text = transcript.text.strip()
                    if not user_text:
                        first_partial_at = None
                        last_partial_at = None
                        continue
                    lang = transcript.language or self._default_language
                    log.info("[user lang=%s] %s", lang, user_text)
                    await self._handle_turn(
                        user_text, lang,
                        first_partial_at=first_partial_at,
                        last_partial_at=last_partial_at,
                    )
                    first_partial_at = None
                    last_partial_at = None
            except SenseVoiceError as exc:
                log.error("STT error; ending session: %s", exc)
        finally:
            # 实现是 async generator 一定带 aclose；Protocol 上声明的是 AsyncIterator，
            # mypy 看不到 aclose 属性，所以这里 ignore。
            try:
                await stt_iter.aclose()  # type: ignore[attr-defined]
            except Exception:
                log.debug("error closing STT iter", exc_info=True)
            try:
                await self._transport.close()
            except Exception:
                log.debug("error closing transport", exc_info=True)

    async def _handle_turn(
        self,
        user_text: str,
        language: str,
        *,
        first_partial_at: float | None = None,
        last_partial_at: float | None = None,
    ) -> None:
        """跑一轮：LLM 流 → 段切 → TTS → 扬声器。"""
        from vocalize.llm.openai_compat import LLMServiceError
        from vocalize.tts.cosyvoice import CosyVoiceError

        timing = TurnTiming(
            user_text=user_text,
            final_at=time.monotonic(),
            first_partial_at=first_partial_at,
            last_partial_at=last_partial_at,
        )
        state = _TurnRunState(timing=timing)

        # 语言指令仅注入本次 LLM 调用的 messages 副本，不持久化进 self._messages，
        # 避免历史里塞满 "[reply in Chinese] " 这种调度噪声（Phase 4 transcript
        # event-stream 与 Phase 6 reflection 都会暴露原文）。
        prefixed = self._language_prefix(language) + user_text
        messages_for_call = self._messages + [
            ChatMessage(role="user", content=prefixed)
        ]

        # TODO(phase-4): consider persistent output stream across turns; per-turn
        # RawOutputStream open/close on macOS Core Audio adds ~50-100ms each direction,
        # eating into the <2.5s e2e budget. Measure first; if real, refactor microphone
        # transport to expose a persistent output mode.

        text_q: asyncio.Queue[TextChunk | None] = asyncio.Queue(maxsize=32)

        async def text_chunks() -> AsyncIterator[TextChunk]:
            while True:
                item = await text_q.get()
                if item is None:
                    return
                yield item

        async def play_tts() -> None:
            tts_iter = self._tts.stream_synthesize(text_chunks())
            first_byte_seen = False

            async def wrapped_audio() -> AsyncIterator[bytes]:
                nonlocal first_byte_seen
                async for chunk in tts_iter:
                    if not first_byte_seen:
                        first_byte_seen = True
                        timing.ttft_tts = time.monotonic() - timing.final_at
                    yield chunk

            await self._transport.output_stream(wrapped_audio())

        tts_task = asyncio.create_task(play_tts())

        # 所有写 text_q 的路径都要 short-circuit 已死的 TTS：text_q 是 bounded
        # (maxsize=32)，TTS 死后 queue 没人消费，再 put 会永久阻塞，整个 turn 挂
        # 死。这是 B1 死锁的根因——LLM 在 TTS 死后仍可能继续 yield 几十个 sentence。
        # 注意：TTS 可能在我们 await put 期间才死掉（前几次 put 没填满 queue 同步
        # 完成，从未让出控制权给 TTS task），所以必须把 put 和 tts_task 完成事件
        # race，否则填满第 33 段时永久阻塞。
        async def _safe_put(item: TextChunk | None, *, is_text_chunk: bool = True) -> None:
            # Phase 4 Plan 02 (D-13 strict) entry-predicate gate：调用方传
            # ``is_text_chunk=False``（tool 进行中或显式非文本路径）→ 直接
            # no-op，不进入 race-detection、不进入队列、不触发任何副作用。
            # 这保留 _safe_put 主体的 race-free 不变量（ARCHITECTURE L259-265）
            # 不动；guard 严格在最前。
            if not is_text_chunk:
                return
            if tts_task.done():
                return
            put_task = asyncio.ensure_future(text_q.put(item))
            done, _pending = await asyncio.wait(
                {put_task, tts_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if put_task in done:
                # put 完成；正常路径
                put_task.result()  # propagate cancellation if any
                return
            # tts_task 先 done → cancel 还没完成的 put（会从 queue 把 item 撤掉）
            put_task.cancel()
            try:
                await put_task
            except (asyncio.CancelledError, Exception):
                log.debug("safe_put cleanup raised", exc_info=True)

        try:
            llm_stream = self._llm.stream_chat(messages_for_call)
            try:
                buf: list[str] = []
                # TODO(phase-4): O(n²) buffer scan — currently joins entire buffer and linear-
                # scans for last sentence-ender on every TextDelta. Fine for short replies (<100
                # tokens); refactor to scan only the new chunk for the FIRST sentence-ender if
                # Phase 4 produces longer outputs.
                async for chunk in llm_stream:
                    await self._handle_llm_chunk(
                        chunk, buf, _safe_put, language, state,
                        tool_call_sink=self._tool_call_sink,
                    )
                # LLM 流结束：按 finish_reason 分流。
                # D-13 strict：reason="tool_calls" → 整个 buffer 丢弃，不做 final
                # flush，TTS 队列不收任何 item，tts_succeeded 保持 False，下面
                # assistant 历史提交分支也不会触发——orchestrator (Plan 04-09) 接管
                # tool 执行 + tool-result 回灌 + 二次 stream_chat。
                if state.finish_reason == "tool_calls":
                    log.info(
                        "tool-only turn complete (tool_call_in_progress=%s, "
                        "buf_len=%d discarded)",
                        state.tool_call_in_progress, len(buf),
                    )
                    # 仍然要塞 None 哨兵让 text_chunks() 退出，否则 TTS task hang。
                    # is_text_chunk=False → 走短路 no-op；但我们 *需要* 哨兵流走
                    # 完，所以单独放在 finally 分支（已有）里送 None。
                else:
                    tail = "".join(buf).strip()
                    pending = state.pending_first_segment
                    state.pending_first_segment = None
                    if pending is not None and not tail:
                        # Phase 4 Plan 04-03 fix #1：短回复合并路径。唯一一个
                        # sentence-ender 切出的段就是整个回复 → 直接以
                        # is_final=True 单帧发出。CosyVoice server.py:948-966 的
                        # batch dispatch (text_frame_count==0 && is_final) 现在
                        # 可以命中，省 ~1.5s ttft。
                        await _safe_put(
                            TextChunk(
                                text=pending.text,
                                language=pending.language,
                                is_final_segment=True,
                            )
                        )
                    elif pending is not None and tail:
                        # 罕见：sentence-ender 之后还有无标点尾巴。先 flush pending
                        # (is_final=False)，再发 tail (is_final=True)，保留原有
                        # 多段流式语义。
                        await _safe_put(pending)
                        await _safe_put(
                            TextChunk(text=tail, language=language, is_final_segment=True)
                        )
                    elif tail:
                        await _safe_put(
                            TextChunk(text=tail, language=language, is_final_segment=True)
                        )
                    else:
                        # buffer 为空但前面也没标过 final → 用一个空 final segment 触发 flush
                        await _safe_put(
                            TextChunk(text="", language=language, is_final_segment=True)
                        )
            except LLMServiceError as exc:
                log.error("LLM error mid-turn: %s; abandoning turn", exc)
                # TTS 任务还活着且在消费 text_q，这里推一句兜底文本告诉用户系统出问题，
                # 比纯静默更友好。注意必须在塞 None 哨兵之前。_safe_put 自动跳过已死 TTS。
                # Phase 4 Plan 04-03 fix #1：丢弃 pending_first_segment——fallback
                # chunk 是 is_final=True 的独立单帧，不应再被 pending 干扰。
                state.pending_first_segment = None
                await _safe_put(_fallback_chunk(language))
            finally:
                try:
                    await llm_stream.aclose()  # type: ignore[attr-defined]
                except Exception:
                    log.debug("error closing LLM stream", exc_info=True)
                # 通知 text_chunks() 结束（None 哨兵）；TTS 已死时 _safe_put no-op。
                await _safe_put(None)

            try:
                await tts_task
                state.tts_succeeded = True
            except CosyVoiceError as exc:
                # TTS 中途 fatal 错误（GPU OOM、服务端 fatal 帧）：放弃本轮音频，
                # 不让单次 GPU 抖动 kill 整个会话。注意 LLM-error 路径下 TTS 还活着、
                # 可推 fallback；这里 TTS 任务已 dead，再 put text_q 也合不出音频，
                # 所以只 log，不重试发兜底。
                # TODO(phase-4): relaunch TTS stream for fallback in TTS-error path
                log.error("TTS error mid-turn: %s; abandoning turn audio", exc)

            # Phase 4 Wave 1 instrumentation: drain per-turn probes from the
            # transport AFTER output_stream returned. hasattr-gated so
            # FakeTransport in tests (and any future transport that doesn't
            # expose these probes — e.g. a telephony media-stream transport in v2)
            # keeps working with timing fields left at None.
            if hasattr(self._transport, "pop_first_audible_ts"):
                timing.t_first_audible = self._transport.pop_first_audible_ts()
            if hasattr(self._transport, "pop_queue_depth_at_first_audio"):
                timing.queue_depth_at_first_audio = (
                    self._transport.pop_queue_depth_at_first_audio()
                )
            # Phase 4 Plan 04-04: wire VAD-detected end-of-speech timestamp
            # into TurnTiming. effective_speech_end (used by stt_finalize and
            # e2e_perceived) prefers this over last_partial_at, fixing the
            # ~11s instrumentation gap from
            # .planning/debug/instrumentation-vs-ear-11s-gap.md. hasattr-gated
            # for FakeTransport. The timestamp may have been stamped before
            # ``final_at``; that's expected — VAD detects EOS faster than the
            # server-side fsmn-vad fallback used to.
            if hasattr(self._transport, "pop_speech_end_ts"):
                timing.last_speech_end_real = self._transport.pop_speech_end_ts()

            assistant_text = "".join(state.pieces).strip()
            # 把"干净"的 user 文本（无语言指令前缀）写进历史
            self._messages.append(ChatMessage(role="user", content=user_text))
            # 只在 TTS 实际播出时把 assistant 回复写进历史；否则用户什么都没听到，
            # 持久化"虚假回复"会让下一轮 LLM 误以为已经回过话，对话状态发散。
            #
            # Phase 4 Plan 02 (D-13 strict)：tool-only turn 即使前段有少量 TextDelta
            # 已触发 TTS 段（导致 tts_succeeded=True），也 *不* 在这里提交 assistant
            # 消息——orchestrator (Plan 04-09) 在执行完 tool 后会自己构造完整的
            # assistant(content=..., tool_calls=[...]) 一次性入历史。
            if (
                assistant_text
                and state.tts_succeeded
                and state.finish_reason != "tool_calls"
            ):
                self._messages.append(
                    ChatMessage(role="assistant", content=assistant_text)
                )
            timing.e2e = timing.ttft_tts
            # Phase 4 Wave 1: t_first_audible_dt is t_first_audible measured
            # from final_at (so it lines up with ttft_tts on the same axis;
            # ttft_tts is final→TTS-first-byte, t_first_audible_dt is
            # final→user-actually-hears). e2e_perceived now uses the real
            # audible timestamp when available (see TurnTiming.e2e_perceived).
            t_first_audible_dt = (
                timing.t_first_audible - timing.final_at
                if timing.t_first_audible is not None
                else None
            )
            log.info(
                "[timing] user=%r stt_finalize=%s ttft_llm=%s ttft_tts=%s "
                "t_first_audible_dt=%s queue_depth=%s e2e=%s e2e_perceived=%s",
                user_text,
                _fmt(timing.stt_finalize),
                _fmt(timing.ttft_llm),
                _fmt(timing.ttft_tts),
                _fmt(t_first_audible_dt),
                timing.queue_depth_at_first_audio
                if timing.queue_depth_at_first_audio is not None
                else "n/a",
                _fmt(timing.e2e),
                _fmt(timing.e2e_perceived),
            )
        except BaseException:
            if not tts_task.done():
                tts_task.cancel()
                try:
                    await tts_task
                except (asyncio.CancelledError, Exception):
                    log.debug("tts_task cancel cleanup raised", exc_info=True)
            raise

    async def _handle_llm_chunk(
        self,
        chunk: LLMChunk,
        buf: list[str],
        safe_put: "_SafePutFn",
        language: str,
        state: _TurnRunState,
        tool_call_sink: "_ToolCallSink | None" = None,
    ) -> None:
        """处理单个 LLMChunk：按类型分发到 TextDelta 段切 / FinishChunk 记账 /
        ToolCallDelta 路由 sink。

        Phase 4 Plan 02 (D-13 strict) 三层 gate 之一在此 method 实施：
        - TextDelta 分支调 ``safe_put(..., is_text_chunk=not state.tool_call_in_progress)``
          → 一旦本轮中已经出现 ToolCallDelta，后续 TextDelta 直接被 _safe_put
          短路；
        - FinishChunk 把 ``reason`` 写到 ``state.finish_reason``，由 _handle_turn
          的流末分流读取；
        - ToolCallDelta 把第一次出现作为 ``state.tool_call_in_progress=True`` 的
          翻转点（idempotent），并把 delta 转发给 ``tool_call_sink``。
          ``tool_call_sink is None`` 时退化为 Phase 3 的"忽略 + debug log"行为，
          让 Phase 3 现有测试保持绿（Pitfall 7）。
        """
        if isinstance(chunk, TextDelta):
            if state.timing.ttft_llm is None:
                state.timing.ttft_llm = time.monotonic() - state.timing.final_at
            state.pieces.append(chunk.text)
            buf.append(chunk.text)
            # Phase 4 Plan 04-03 fix #1 dispatch repair：在处理本 delta 切割前先把
            # 上一次 stash 的 pending_first 真正 flush —— 我们已经看到更多 LLM 内容
            # 到来，pending 不再可能成为单帧 final。注意保持 D-13
            # ``is_text_chunk=not state.tool_call_in_progress`` 三层 gate 不变。
            if state.pending_first_segment is not None:
                pending = state.pending_first_segment
                state.pending_first_segment = None
                state.mid_segment_flushed = True
                await safe_put(
                    pending,
                    is_text_chunk=not state.tool_call_in_progress,
                )
            joined = "".join(buf)
            # 找最后一个句末标点；前面的整体作为一段送 TTS
            last = -1
            for i, ch in enumerate(joined):
                if ch in _SENTENCE_ENDERS:
                    last = i
            if last >= 0:
                segment = joined[: last + 1]
                remainder = joined[last + 1:]
                buf.clear()
                if remainder:
                    buf.append(remainder)
                chunk_to_send = TextChunk(
                    text=segment, language=language, is_final_segment=False,
                )
                # 第一段：stash，等下一个 LLM chunk 或 _handle_turn 流末决定是否合
                # 并为 is_final=True 单帧（短回复批量分发路径）。后续段直接 flush。
                if (
                    not state.mid_segment_flushed
                    and state.pending_first_segment is None
                ):
                    state.pending_first_segment = chunk_to_send
                else:
                    state.mid_segment_flushed = True
                    await safe_put(
                        chunk_to_send,
                        is_text_chunk=not state.tool_call_in_progress,
                    )
        elif isinstance(chunk, FinishChunk):
            # 把 reason 暴露给 _handle_turn 的流末分流逻辑（buffer flush vs discard）。
            state.finish_reason = chunk.reason
        elif isinstance(chunk, ToolCallDelta):
            # 第一次见 ToolCallDelta 翻转 in-progress 标志（idempotent，再次进入
            # 不变更）。这同时作为 D-13 strict 的"tool 中段抑制后续 TextDelta 入
            # TTS 队列"的触发条件。
            state.tool_call_in_progress = True
            if tool_call_sink is not None:
                tool_call_sink(chunk)
            else:
                # Phase 3 老路径：保留原有 debug log 语义，不影响任何 Phase 3 测试。
                log.debug("ignoring tool_call delta in Phase 3 pipeline")

    async def speak(self, text: str, language: str) -> None:
        """Phase 4 helper: 把单段文本作为 ``is_final_segment=True`` TTS 单帧朗读。

        定位（Plan 04-09）：``DialogueOrchestrator._drive_turn`` 在 NL 路径
        和 cross-lingual relay 路径都通过这个 helper 朗读，避免再去搭
        text_q / play_tts 双协程结构。

        实现：把 ``text`` 包成 ``TextChunk(is_final_segment=True)`` 单帧
        喂给 ``self._tts.stream_synthesize``，再把音频灌进
        ``self._transport.output_stream``。``output_stream`` 在所有音频实
        际播完后才返回（``MicrophoneTransport`` 已按这个语义实现；
        FakeTransport 在测试中行为一致）。
        """
        if not text:
            return

        async def _one_chunk() -> AsyncIterator[TextChunk]:
            yield TextChunk(text=text, language=language, is_final_segment=True)

        await self._transport.output_stream(self._tts.stream_synthesize(_one_chunk()))

    @staticmethod
    def _language_prefix(language: str) -> str:
        """轻量 per-message 语言指令；不污染 system prompt。"""
        if language.startswith("zh"):
            return "[reply in Chinese] "
        if language.startswith("en"):
            return "[reply in English] "
        return f"[reply in {language}] "


def _fmt(v: float | None) -> str:
    return f"{v:.3f}s" if v is not None else "n/a"


def _fallback_chunk(language: str) -> TextChunk:
    """LLM/TTS 出错时的兜底播报；按语言切中英。"""
    # 注意：本兜底仅在 LLM 出错路径触发——STT 已经成功识别，"没听清"会误导
    # 用户重发音；中英文都用"系统出问题"措辞贴近真实根因，不暗示用户表达有问题。
    if language.startswith("zh"):
        text = "抱歉，我这边出了点问题，能再说一次吗？"
    else:
        text = "Sorry, something went wrong on my end — could you say that again?"
    return TextChunk(text=text, language=language, is_final_segment=True)
