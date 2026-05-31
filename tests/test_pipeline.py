"""VoicePipeline 端到端测试（all-fake services）。

策略：把 transport / STT / LLM / TTS 全部替换成 in-memory 假实现，记录关键
调用、断言：
- 单轮 zh：transcript → LLM → TTS（最后段标 final）→ 音频原样写入 transport.output
- 多轮：detected_language 在 zh / en / zh 之间切，每轮 TTS 收到的 chunk language 正确
- LLM 失败：pipeline 不崩，能进入下一轮
- shutdown：cancel(run) 关 transport 与 streams
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Literal

import pytest

from vocalize.llm.base import (
    ChatMessage,
    FinishChunk,
    LLMChunk,
    TextDelta,
    ToolCallDelta,
    ToolDef,
)
from vocalize.llm.openai_compat import LLMServiceError
from vocalize.pipeline import VoicePipeline
from vocalize.providers.speech import SpeechProviderError
from vocalize.stt.base import Transcript
from vocalize.transports.base import AudioEncoding
from vocalize.tts.base import TextChunk


class FakeTransport:
    sample_rate: int = 16000
    channels: int = 1
    encoding: AudioEncoding = "pcm_s16le"

    def __init__(self) -> None:
        self.output_blocks: list[bytes] = []
        self.closed = False
        self._input_done = asyncio.Event()
        self.output_calls = 0

    async def input_stream(self) -> AsyncIterator[bytes]:
        # 不发音频，但保持 STT 的输入 iterator 不立即耗尽
        try:
            await self._input_done.wait()
        except asyncio.CancelledError:
            raise
        if False:  # pragma: no cover - keep function an async generator
            yield b""  # type: ignore[unreachable]

    async def output_stream(self, audio: AsyncIterator[bytes]) -> None:
        self.output_calls += 1
        async for chunk in audio:
            self.output_blocks.append(chunk)

    async def close(self) -> None:
        self.closed = True
        self._input_done.set()


class FakeSTT:
    def __init__(self, transcripts: list[Transcript]) -> None:
        self._transcripts = transcripts

    async def stream_transcribe(
        self, audio_chunks: AsyncIterator[bytes]
    ) -> AsyncIterator[Transcript]:
        for t in self._transcripts:
            yield t
            await asyncio.sleep(0)


class FakeLLM:
    """记录每次 stream_chat 的调用入参 + 按 turn 脚本 yield chunks。"""

    def __init__(
        self,
        scripts: list[list[LLMChunk] | Exception],
    ) -> None:
        self._scripts = list(scripts)
        self.calls: list[list[ChatMessage]] = []

    async def stream_chat(
        self,
        messages: list[ChatMessage],
        tools: list[ToolDef] | None = None,
    ) -> AsyncIterator[LLMChunk]:
        # 拷贝 messages，避免后续 pipeline mutation 污染断言
        self.calls.append([ChatMessage(**m.__dict__) for m in messages])
        if not self._scripts:
            return
        item = self._scripts.pop(0)
        if isinstance(item, Exception):
            raise item
        for chunk in item:
            yield chunk


class FakeTTS:
    output_sample_rate: int = 24000
    output_encoding: AudioEncoding = "pcm_s16le"

    def __init__(self, audio_per_call: list[list[bytes]]) -> None:
        self._audio_per_call = list(audio_per_call)
        self.received_chunks: list[list[TextChunk]] = []

    async def stream_synthesize(
        self, text_chunks: AsyncIterator[TextChunk]
    ) -> AsyncIterator[bytes]:
        per_call: list[TextChunk] = []
        async for c in text_chunks:
            per_call.append(c)
        self.received_chunks.append(per_call)
        if not self._audio_per_call:
            return
        for b in self._audio_per_call.pop(0):
            yield b


def _td(t: str) -> TextDelta:
    return TextDelta(text=t)


def _fin(reason: Literal["stop", "tool_calls", "length", "content_filter"] = "stop") -> FinishChunk:
    return FinishChunk(reason=reason)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------
async def test_single_turn_happy_path_zh() -> None:
    transport = FakeTransport()
    stt = FakeSTT([
        Transcript(text="你好", is_final=True, confidence=1.0,
                   start_time=0, end_time=1, utterance_id=0, language="zh"),
    ])
    # Phase 4 Plan 04-03 fix #1: 此测试断言"中间段不应都标 final"——多段流式语义。
    # 必须包含 ≥2 个 sentence-ender 才能产生 mid (is_final=False) + final
    # (is_final=True) 两个 chunk；单 sentence-ender 现在会被合并为单帧 final
    # （短回复批量分发路径）。
    llm = FakeLLM([
        [_td("你好"), _td("。"), _td("请问几位"), _td("？"), _fin()],
    ])
    tts = FakeTTS([[b"\x01" * 4, b"\x02" * 4]])

    pipeline = VoicePipeline(
        transport=transport, stt=stt, llm=llm, tts=tts,
        system_prompt="sys", default_language="zh",
    )

    async def runner() -> None:
        await pipeline.run()

    task = asyncio.create_task(runner())
    # STT 只产 1 条 final + 然后 input_stream 一直等；让 pipeline 跑一会儿
    for _ in range(50):
        await asyncio.sleep(0.01)
        if tts.received_chunks and transport.output_blocks:
            break
    # 让 pipeline 退出：close transport 让 input 自然结束 → STT iter 自然结束
    await transport.close()
    await asyncio.wait_for(task, timeout=2.0)

    # TTS 收到了 chunks，最后一个 is_final_segment=True
    assert tts.received_chunks
    chunks = tts.received_chunks[0]
    assert all(c.language == "zh" for c in chunks)
    assert chunks[-1].is_final_segment is True
    # 中间段不应该都标 final
    assert any(c.is_final_segment is False for c in chunks)
    # 拼起来包含完整文本
    full = "".join(c.text for c in chunks)
    assert "你好" in full and "请问几位" in full

    assert transport.output_blocks == [b"\x01" * 4, b"\x02" * 4]
    assert transport.closed is True


async def test_multi_turn_language_switches() -> None:
    transport = FakeTransport()
    stt = FakeSTT([
        Transcript(text="你好", is_final=True, confidence=1, start_time=0,
                   end_time=1, utterance_id=0, language="zh"),
        Transcript(text="hello", is_final=True, confidence=1, start_time=1,
                   end_time=2, utterance_id=1, language="en"),
        Transcript(text="再见", is_final=True, confidence=1, start_time=2,
                   end_time=3, utterance_id=2, language="zh"),
    ])
    llm = FakeLLM([
        [_td("你好。"), _fin()],
        [_td("hi."), _fin()],
        [_td("再见。"), _fin()],
    ])
    tts = FakeTTS([[b"a"], [b"b"], [b"c"]])

    pipeline = VoicePipeline(
        transport=transport, stt=stt, llm=llm, tts=tts,
        system_prompt="sys", default_language="zh",
    )
    task = asyncio.create_task(pipeline.run())
    for _ in range(80):
        await asyncio.sleep(0.01)
        if len(tts.received_chunks) >= 3:
            break
    await transport.close()
    await asyncio.wait_for(task, timeout=2.0)

    assert len(tts.received_chunks) == 3
    langs = [c.language for chunks in tts.received_chunks for c in chunks]
    # 第 1 轮 zh, 第 2 轮 en, 第 3 轮 zh
    assert tts.received_chunks[0][0].language == "zh"
    assert tts.received_chunks[1][0].language == "en"
    assert tts.received_chunks[2][0].language == "zh"
    assert "zh" in langs and "en" in langs

    # 同样语言信息也应该传到 LLM（我们用 prefix 注入）
    user_msgs = [
        m.content for call in llm.calls for m in call if m.role == "user"
    ]
    assert any("Chinese" in u for u in user_msgs)
    assert any("English" in u for u in user_msgs)


async def test_llm_error_does_not_crash_pipeline() -> None:
    transport = FakeTransport()
    stt = FakeSTT([
        Transcript(text="你好", is_final=True, confidence=1, start_time=0,
                   end_time=1, utterance_id=0, language="zh"),
        Transcript(text="再来", is_final=True, confidence=1, start_time=1,
                   end_time=2, utterance_id=1, language="zh"),
    ])
    llm = FakeLLM([
        LLMServiceError("boom"),
        [_td("ok。"), _fin()],
    ])
    tts = FakeTTS([[], [b"x"]])  # 第一轮 LLM 失败 → TTS 还是被调用（拿不到内容）

    pipeline = VoicePipeline(
        transport=transport, stt=stt, llm=llm, tts=tts,
        system_prompt="sys", default_language="zh",
    )
    task = asyncio.create_task(pipeline.run())
    for _ in range(80):
        await asyncio.sleep(0.01)
        if len(llm.calls) >= 2:
            break
    await transport.close()
    await asyncio.wait_for(task, timeout=2.0)

    # 第二轮成功跑完
    assert len(llm.calls) == 2
    assert b"x" in transport.output_blocks
    assert transport.closed is True


async def test_tts_error_does_not_crash_pipeline() -> None:
    """C2 回归：TTS 第一轮抛 SpeechProviderError，第二轮正常；pipeline 必须跑完两轮。"""
    transport = FakeTransport()
    stt = FakeSTT([
        Transcript(text="你好", is_final=True, confidence=1, start_time=0,
                   end_time=1, utterance_id=0, language="zh"),
        Transcript(text="再来", is_final=True, confidence=1, start_time=1,
                   end_time=2, utterance_id=1, language="zh"),
    ])
    llm = FakeLLM([
        [_td("第一轮。"), _fin()],
        [_td("第二轮。"), _fin()],
    ])

    class FlakyTTS:
        output_sample_rate: int = 24000
        output_encoding: AudioEncoding = "pcm_s16le"

        def __init__(self) -> None:
            self.calls = 0
            self.received_chunks: list[list[TextChunk]] = []

        async def stream_synthesize(
            self, text_chunks: AsyncIterator[TextChunk]
        ) -> AsyncIterator[bytes]:
            self.calls += 1
            per_call: list[TextChunk] = []
            async for c in text_chunks:
                per_call.append(c)
            self.received_chunks.append(per_call)
            if self.calls == 1:
                raise SpeechProviderError("simulated provider outage")
            yield b"second-turn-audio"

    tts = FlakyTTS()
    pipeline = VoicePipeline(
        transport=transport, stt=stt, llm=llm, tts=tts,
        system_prompt="sys", default_language="zh",
    )
    task = asyncio.create_task(pipeline.run())
    for _ in range(80):
        await asyncio.sleep(0.01)
        if tts.calls >= 2 and b"second-turn-audio" in transport.output_blocks:
            break
    await transport.close()
    await asyncio.wait_for(task, timeout=2.0)

    # 第一轮 TTS 失败 → 不应 kill 会话；第二轮音频必须落到 transport
    assert tts.calls == 2
    assert b"second-turn-audio" in transport.output_blocks
    assert transport.closed is True


async def test_user_message_history_is_clean() -> None:
    """I6 回归：``self._messages`` 里的 user 消息必须是无前缀的纯文本。"""
    transport = FakeTransport()
    stt = FakeSTT([
        Transcript(text="你好", is_final=True, confidence=1, start_time=0,
                   end_time=1, utterance_id=0, language="zh"),
        Transcript(text="hello", is_final=True, confidence=1, start_time=1,
                   end_time=2, utterance_id=1, language="en"),
        Transcript(text="再见", is_final=True, confidence=1, start_time=2,
                   end_time=3, utterance_id=2, language="zh"),
    ])
    llm = FakeLLM([
        [_td("你好。"), _fin()],
        [_td("hi."), _fin()],
        [_td("再见。"), _fin()],
    ])
    tts = FakeTTS([[b"a"], [b"b"], [b"c"]])
    pipeline = VoicePipeline(
        transport=transport, stt=stt, llm=llm, tts=tts,
        system_prompt="sys", default_language="zh",
    )
    task = asyncio.create_task(pipeline.run())
    for _ in range(80):
        await asyncio.sleep(0.01)
        if len(tts.received_chunks) >= 3:
            break
    await transport.close()
    await asyncio.wait_for(task, timeout=2.0)

    # 历史里所有 user 消息都不应有调度噪声前缀
    user_msgs = [m for m in pipeline._messages if m.role == "user"]
    assert len(user_msgs) == 3
    for m in user_msgs:
        assert "[reply in" not in m.content, (
            f"user history polluted with language-hint prefix: {m.content!r}"
        )
    # 内容是原始 transcript 文本
    assert [m.content for m in user_msgs] == ["你好", "hello", "再见"]


async def test_tts_dies_during_llm_error_does_not_deadlock() -> None:
    """I3 回归：TTS 先死、LLM 后报错时，fallback put 和 None 哨兵不应永久阻塞。

    text_q maxsize=32；FakeLLM 推 60 个 TextDelta（超过队列容量）后抛
    LLMServiceError。FlakyTTS 在第一个 chunk 消费前直接抛 SpeechProviderError，
    让 TTS task 在 LLM 还没报错时已 dead。若 fix 缺失，两次 await text_q.put()
    在 queue 满时永久阻塞，asyncio.wait_for 超时即为回归。
    """
    transport = FakeTransport()
    stt = FakeSTT([
        Transcript(text="你好", is_final=True, confidence=1, start_time=0,
                   end_time=1, utterance_id=0, language="zh"),
    ])

    class LLMWithManyDeltasThenError:
        """先 yield 60 个 TextDelta 让 text_q 填满，然后抛 LLMServiceError。"""

        async def stream_chat(
            self,
            messages: list[ChatMessage],
            tools: list[ToolDef] | None = None,
        ) -> AsyncIterator[LLMChunk]:
            for i in range(60):
                yield TextDelta(text=f"词{i}")
            raise LLMServiceError("boom after many deltas")

    class FlakyTTSImmediate:
        """立即抛 SpeechProviderError，不消费任何 text chunk。"""
        output_sample_rate: int = 24000
        output_encoding: AudioEncoding = "pcm_s16le"

        async def stream_synthesize(
            self, text_chunks: AsyncIterator[TextChunk]
        ) -> AsyncIterator[bytes]:
            raise SpeechProviderError("simulated immediate TTS death")
            yield b""  # type: ignore[unreachable]

    pipeline = VoicePipeline(
        transport=transport,
        stt=stt,
        llm=LLMWithManyDeltasThenError(),
        tts=FlakyTTSImmediate(),
        system_prompt="sys",
        default_language="zh",
    )

    # Must complete within 5 seconds; without the fix it hangs on put() → TimeoutError
    async def run_and_close() -> None:
        task = asyncio.create_task(pipeline.run())
        await asyncio.sleep(0.2)
        await transport.close()
        await task

    await asyncio.wait_for(run_and_close(), timeout=5.0)
    assert transport.closed is True


async def test_tts_dies_during_normal_llm_completion_does_not_deadlock() -> None:
    """B1 回归：TTS 立即死，LLM 仍正常 stream 大量带句末标点的 TextDelta 后正常
    FinishChunk 收尾——若 ``_handle_llm_chunk`` 的 in-loop ``text_q.put`` 没有
    short-circuit 已死的 TTS，第 33 个段就会在 bounded queue (maxsize=32) 上永久阻塞。
    """
    transport = FakeTransport()
    stt = FakeSTT([
        Transcript(text="你好", is_final=True, confidence=1, start_time=0,
                   end_time=1, utterance_id=0, language="zh"),
    ])

    class LLMManySegmentsThenFinish:
        async def stream_chat(
            self,
            messages: list[ChatMessage],
            tools: list[ToolDef] | None = None,
        ) -> AsyncIterator[LLMChunk]:
            # 60 个带句末标点的 delta → 60 个段，远超 text_q maxsize=32
            for i in range(60):
                yield TextDelta(text=f"句{i}。")
            yield FinishChunk(reason="stop")

    class FlakyTTSImmediate:
        output_sample_rate: int = 24000
        output_encoding: AudioEncoding = "pcm_s16le"

        async def stream_synthesize(
            self, text_chunks: AsyncIterator[TextChunk]
        ) -> AsyncIterator[bytes]:
            raise SpeechProviderError("simulated immediate TTS death")
            yield b""  # type: ignore[unreachable]

    pipeline = VoicePipeline(
        transport=transport,
        stt=stt,
        llm=LLMManySegmentsThenFinish(),
        tts=FlakyTTSImmediate(),
        system_prompt="sys",
        default_language="zh",
    )

    async def run_and_close() -> None:
        task = asyncio.create_task(pipeline.run())
        await asyncio.sleep(0.2)
        await transport.close()
        await task

    await asyncio.wait_for(run_and_close(), timeout=5.0)
    assert transport.closed is True


async def test_tts_failure_does_not_pollute_history() -> None:
    """B3 回归：TTS 抛 SpeechProviderError 时 assistant 文本不应入历史——用户什么都没听到，
    持久化"虚假回复"会让下一轮 LLM 对话状态发散。
    """
    transport = FakeTransport()
    stt = FakeSTT([
        Transcript(text="你好", is_final=True, confidence=1, start_time=0,
                   end_time=1, utterance_id=0, language="zh"),
    ])
    llm = FakeLLM([
        [_td("这一段不会被听到。"), _fin()],
    ])

    class DyingTTS:
        output_sample_rate: int = 24000
        output_encoding: AudioEncoding = "pcm_s16le"

        async def stream_synthesize(
            self, text_chunks: AsyncIterator[TextChunk]
        ) -> AsyncIterator[bytes]:
            async for _ in text_chunks:
                pass
            raise SpeechProviderError("simulated TTS death")
            yield b""  # type: ignore[unreachable]

    pipeline = VoicePipeline(
        transport=transport, stt=stt, llm=llm, tts=DyingTTS(),
        system_prompt="sys", default_language="zh",
    )
    task = asyncio.create_task(pipeline.run())
    for _ in range(80):
        await asyncio.sleep(0.01)
        if llm.calls:
            break
    # 给 TTS 失败一点时间走完
    await asyncio.sleep(0.05)
    await transport.close()
    await asyncio.wait_for(task, timeout=2.0)

    assistant_msgs = [m for m in pipeline._messages if m.role == "assistant"]
    assert assistant_msgs == [], (
        f"TTS failure leaked assistant text into history: {assistant_msgs}"
    )


async def test_stt_error_ends_session_cleanly() -> None:
    """I7 回归：STT 抛 SpeechProviderError 应 end-session-cleanly——不外抛、关 transport。"""
    transport = FakeTransport()

    class BoomSTT:
        async def stream_transcribe(
            self, audio_chunks: AsyncIterator[bytes]
        ) -> AsyncIterator[Transcript]:
            yield Transcript(text="hi", is_final=True, confidence=1,
                             start_time=0, end_time=1, utterance_id=0,
                             language="en")
            raise SpeechProviderError("STT provider died")

    llm = FakeLLM([[_td("ok."), _fin()]])
    tts = FakeTTS([[b"x"]])

    pipeline = VoicePipeline(
        transport=transport, stt=BoomSTT(), llm=llm, tts=tts,
        system_prompt="sys", default_language="en",
    )

    # Should return cleanly, not raise
    await asyncio.wait_for(pipeline.run(), timeout=3.0)
    assert transport.closed is True


async def test_shutdown_closes_transport() -> None:
    transport = FakeTransport()

    class HangingSTT:
        async def stream_transcribe(
            self, audio_chunks: AsyncIterator[bytes]
        ) -> AsyncIterator[Transcript]:
            await asyncio.Event().wait()
            if False:  # pragma: no cover - keep function an async generator
                yield  # type: ignore[unreachable]

    pipeline = VoicePipeline(
        transport=transport, stt=HangingSTT(), llm=FakeLLM([]), tts=FakeTTS([]),
        system_prompt="sys", default_language="zh",
    )
    task = asyncio.create_task(pipeline.run())
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert transport.closed is True


# ---------------------------------------------------------------------------
# Phase 4 — tool round-trip skeleton (D-13)
# ---------------------------------------------------------------------------
async def test_tool_call_round_trip_no_audio() -> None:
    """A tool-only assistant turn (FinishChunk reason='tool_calls') must NOT
    produce TTS audio; the post-tool natural-language reply (reason='stop')
    must be the single TTS turn observed.

    Wave 0 skeleton — body pytest.skip until Wave 2 wires:
      (1) ChatMessage.tool_calls field on src/vocalize/llm/base.py
      (2) ToolCall dataclass + _chat_message_to_openai 'tool_calls' branch
      (3) VoicePipeline._handle_llm_chunk buffer + flush-on-'stop' (D-13)

    Plan 04-02 scope: this test verifies the protocol-mechanic side of D-13
    (tool-only turn produces no audio + no assistant-history commit). The
    tool-result re-invocation path (FakeLLM call 2) is the orchestrator's
    responsibility (Plan 04-09); here we only assert the FIRST turn behavior
    and that _handle_llm_chunk routes ToolCallDelta to the injected sink.
    """
    transport = FakeTransport()
    stt = FakeSTT([
        Transcript(text="把日期改成5月10号", is_final=True, confidence=1.0,
                   start_time=0, end_time=1, utterance_id=0, language="zh"),
    ])
    # Tool-only turn: stream emits ToolCallDelta(s) then FinishChunk(reason="tool_calls").
    llm = FakeLLM([
        [
            ToolCallDelta(
                tool_call_index=0,
                tool_call_id="call_abc",
                name="update_booking_field",
                arguments_delta='{"slot":',
            ),
            ToolCallDelta(
                tool_call_index=0,
                tool_call_id=None,
                name=None,
                arguments_delta='"date","value":"2026-05-10"}',
            ),
            FinishChunk(reason="tool_calls"),
        ],
    ])
    tts = FakeTTS([[]])

    pipeline = VoicePipeline(
        transport=transport, stt=stt, llm=llm, tts=tts,
        system_prompt="sys", default_language="zh",
    )

    # Inject a tool_call_sink so we can assert the deltas were routed.
    captured: list[ToolCallDelta] = []
    def sink(delta: ToolCallDelta) -> None:
        captured.append(delta)
    pipeline._tool_call_sink = sink  # type: ignore[attr-defined]

    task = asyncio.create_task(pipeline.run())
    for _ in range(80):
        await asyncio.sleep(0.01)
        if llm.calls and len(captured) >= 2:
            break
    await transport.close()
    await asyncio.wait_for(task, timeout=2.0)

    # 1. Tool-only turn produces NO TTS audio chunks (text queue strictly gated).
    assert tts.received_chunks == [[]] or tts.received_chunks == [], (
        f"tool-only turn leaked TTS chunks: {tts.received_chunks}"
    )
    assert transport.output_blocks == [], (
        f"tool-only turn leaked audio bytes: {transport.output_blocks}"
    )

    # 2. The injected sink received both ToolCallDelta in order.
    assert len(captured) == 2
    assert captured[0].tool_call_id == "call_abc"
    assert captured[0].arguments_delta == '{"slot":'
    assert captured[1].arguments_delta == '"date","value":"2026-05-10"}'

    # 3. assistant message NOT committed to history (tts_succeeded=False path).
    assistant_msgs = [m for m in pipeline._messages if m.role == "assistant"]
    assert assistant_msgs == [], (
        f"tool-only turn must not commit assistant message: {assistant_msgs}"
    )
    # user transcript IS committed (it was a real input).
    user_msgs = [m for m in pipeline._messages if m.role == "user"]
    assert [m.content for m in user_msgs] == ["把日期改成5月10号"]


async def test_text_only_turn_unchanged_by_tool_branch() -> None:
    """Phase 3 happy-path regression: a normal text turn with FinishChunk(reason="stop")
    still produces the exact same TTS output as before the buffer-and-branch refactor.
    """
    transport = FakeTransport()
    stt = FakeSTT([
        Transcript(text="你好", is_final=True, confidence=1.0,
                   start_time=0, end_time=1, utterance_id=0, language="zh"),
    ])
    llm = FakeLLM([
        [_td("你好"), _td("，"), _td("请问几位"), _td("？"), _fin()],
    ])
    tts = FakeTTS([[b"\x01" * 4]])

    pipeline = VoicePipeline(
        transport=transport, stt=stt, llm=llm, tts=tts,
        system_prompt="sys", default_language="zh",
    )
    task = asyncio.create_task(pipeline.run())
    for _ in range(80):
        await asyncio.sleep(0.01)
        if tts.received_chunks and transport.output_blocks:
            break
    await transport.close()
    await asyncio.wait_for(task, timeout=2.0)

    # Same shape as test_single_turn_happy_path_zh expectations.
    assert tts.received_chunks
    chunks = tts.received_chunks[0]
    assert chunks[-1].is_final_segment is True
    full = "".join(c.text for c in chunks)
    assert "你好" in full and "请问几位" in full
    # assistant message IS committed (text turn with tts_succeeded=True).
    assistant_msgs = [m for m in pipeline._messages if m.role == "assistant"]
    assert len(assistant_msgs) == 1
    assert "请问几位" in assistant_msgs[0].content


async def test_mixed_text_then_tool_call_gates_post_tool_text() -> None:
    """Mixed stream: TextDelta → ToolCallDelta → TextDelta → FinishChunk(reason="tool_calls").

    Per the entry-predicate gate (is_text_chunk=not state.tool_call_in_progress):
    text emitted BEFORE the first ToolCallDelta MAY flow to TTS (existing per-sentence
    behavior); text emitted AFTER the first ToolCallDelta MUST be a no-op at the
    queue boundary; the trailing buffer is discarded entirely on tool_calls finish.
    """
    transport = FakeTransport()
    stt = FakeSTT([
        Transcript(text="改日期", is_final=True, confidence=1.0,
                   start_time=0, end_time=1, utterance_id=0, language="zh"),
    ])
    llm = FakeLLM([
        [
            _td("好的。"),  # before first tool call — may flush as a sentence
            ToolCallDelta(
                tool_call_index=0,
                tool_call_id="call_x",
                name="update_booking_field",
                arguments_delta='{"slot":"date"}',
            ),
            _td("不应播报。"),  # after tool call — must be gated to no-op
            FinishChunk(reason="tool_calls"),
        ],
    ])
    tts = FakeTTS([[]])

    pipeline = VoicePipeline(
        transport=transport, stt=stt, llm=llm, tts=tts,
        system_prompt="sys", default_language="zh",
    )
    captured: list[ToolCallDelta] = []
    pipeline._tool_call_sink = lambda d: captured.append(d)  # type: ignore[attr-defined]

    task = asyncio.create_task(pipeline.run())
    for _ in range(80):
        await asyncio.sleep(0.01)
        if llm.calls and captured:
            break
    await transport.close()
    await asyncio.wait_for(task, timeout=2.0)

    # tool delta routed to sink.
    assert len(captured) == 1
    assert captured[0].tool_call_id == "call_x"

    # Crucially: text emitted AFTER the tool call must NOT reach TTS.
    received_text = "".join(
        c.text for chunks in tts.received_chunks for c in chunks
    )
    assert "不应播报" not in received_text, (
        f"post-tool-call text leaked to TTS: {received_text!r}"
    )
    # assistant message NOT committed (tool-only finish discards buffer).
    assistant_msgs = [m for m in pipeline._messages if m.role == "assistant"]
    assert assistant_msgs == []


# ---------------------------------------------------------------------------
# Phase 4 Plan 04-03 fix #1 dispatch repair.
# ---------------------------------------------------------------------------
async def test_batch_dispatch_short_reply_single_frame() -> None:
    """Phase 4 Plan 04-03 fix #1 dispatch repair regression.

    A short reply that completes in a single sentence-ender ("好的。") MUST be
    sent to the TTS service as ONE TextChunk with is_final_segment=True — that
    is the precondition for provider batch dispatch
    (text_frame_count_for_session==0 AND is_final) to ever fire. Pre-fix,
    the pipeline emitted 2 frames (mid is_final=False + empty is_final=True
    sentinel), making the batch path unreachable.
    """
    transport = FakeTransport()
    stt = FakeSTT([
        Transcript(text="几位", is_final=True, confidence=1.0,
                   start_time=0, end_time=1, utterance_id=0, language="zh"),
    ])
    llm = FakeLLM([
        # Single sentence-ender, no trailing tokens — the canonical short reply.
        [_td("好的"), _td("。"), _fin()],
    ])
    tts = FakeTTS([[b"\x01" * 4]])

    pipeline = VoicePipeline(
        transport=transport, stt=stt, llm=llm, tts=tts,
        system_prompt="sys", default_language="zh",
    )
    task = asyncio.create_task(pipeline.run())
    for _ in range(80):
        await asyncio.sleep(0.01)
        if tts.received_chunks and transport.output_blocks:
            break
    await transport.close()
    await asyncio.wait_for(task, timeout=2.0)

    assert tts.received_chunks
    chunks = tts.received_chunks[0]
    # Exactly ONE chunk with is_final_segment=True and the full reply text.
    assert len(chunks) == 1, (
        f"short reply must compact into a single TTS frame to enable "
        f"provider batch dispatch; got {len(chunks)} frames: "
        f"{[(c.text, c.is_final_segment) for c in chunks]}"
    )
    assert chunks[0].is_final_segment is True
    assert chunks[0].text == "好的。"


async def test_bistream_dispatch_multi_segment_unchanged() -> None:
    """Companion to test_batch_dispatch_short_reply_single_frame.

    Multi-sentence replies MUST still stream as multiple chunks (mid + final)
    so that streaming provider paths keep working — the batch dispatch repair
    must not regress long-reply ttft. With "好的。明天给你确认。" the patched
    pipeline:
      1. stash 第一段 "好的。" 进 pending（is_final=False）
      2. 看到第二段的第一个 LLM token "明天" → flush pending（is_final=False）
         + stash "明天给你确认。" 进新的 pending（mid_segment_flushed=True 之后
         继续 sentence-ender flush 时直接发出 is_final=False）
      3. 流末空 tail + 第二段已经作为 mid is_final=False 流出 → 空哨兵 final
    断言只要"≥2 chunk + 中间至少一个 is_final=False + 末尾 is_final=True"
    维持，具体帧数不重要（取决于 stash 时序）。
    """
    transport = FakeTransport()
    stt = FakeSTT([
        Transcript(text="确认一下", is_final=True, confidence=1.0,
                   start_time=0, end_time=1, utterance_id=0, language="zh"),
    ])
    llm = FakeLLM([
        [_td("好的。"), _td("明天"), _td("给你确认。"), _fin()],
    ])
    tts = FakeTTS([[b"\x01" * 4]])

    pipeline = VoicePipeline(
        transport=transport, stt=stt, llm=llm, tts=tts,
        system_prompt="sys", default_language="zh",
    )
    task = asyncio.create_task(pipeline.run())
    for _ in range(80):
        await asyncio.sleep(0.01)
        if tts.received_chunks and transport.output_blocks:
            break
    await transport.close()
    await asyncio.wait_for(task, timeout=2.0)

    chunks = tts.received_chunks[0]
    assert len(chunks) >= 2, (
        f"multi-segment reply must stream as multiple chunks; got: "
        f"{[(c.text, c.is_final_segment) for c in chunks]}"
    )
    # 至少一个 mid (is_final=False) 段
    assert any(c.is_final_segment is False for c in chunks)
    # 末尾必为 is_final=True
    assert chunks[-1].is_final_segment is True
    full = "".join(c.text for c in chunks)
    assert "好的" in full and "明天给你确认" in full
