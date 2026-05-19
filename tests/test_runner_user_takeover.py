from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest

from vocalize.dialogue.state import TaskPhase, TaskState
from vocalize.dialogue.user_channel import WebSocketUserChannel
from vocalize.llm.base import LLMChunk, TextDelta
from vocalize.server.runner import DialogueOrchestratorRunner
from vocalize.server.state import Session


def _build_runner_for_test(session: Session) -> DialogueOrchestratorRunner:
    runner = DialogueOrchestratorRunner.__new__(DialogueOrchestratorRunner)
    runner.text_frames = []
    runner.audio_blocks = []
    runner.stop = asyncio.Event()
    runner._session = session
    runner._hint_q = None
    runner._takeover_q = None
    runner._merchant_transcript_cache = {}
    runner._pending_ai_outputs = []
    runner._orchestrator = None
    runner._user_channel = None
    return runner


class _FakeLLM:
    def __init__(
        self,
        chunks: list[LLMChunk] | None = None,
        raise_exc: Exception | None = None,
    ) -> None:
        self._chunks = list(chunks or [])
        self._raise = raise_exc
        self.call_count = 0

    async def stream_chat(
        self,
        *,
        messages: Any,
    ) -> AsyncIterator[LLMChunk]:
        self.call_count += 1
        if self._raise is not None:
            raise self._raise
        for chunk in self._chunks:
            yield chunk


class _PushChannel:
    def __init__(self, events: list[dict[str, Any]], order: list[str]) -> None:
        self._events = events
        self._order = order

    async def push_event(self, event: dict[str, Any]) -> None:
        self._events.append(event)
        self._order.append("translation_frame")


class _OrchestratorWithSegment:
    current_segment_id = "seg-1"


class _MerchantTTS:
    def __init__(self, spoken: list[tuple[str, str]], order: list[str]) -> None:
        self._spoken = spoken
        self._order = order

    def stream_synthesize(self, chunks: Any) -> Any:
        async def _gen() -> Any:
            async for chunk in chunks:
                self._spoken.append((chunk.text, chunk.language))
                self._order.append("merchant_tts")
                yield b"voice"

        return _gen()


class _MerchantTransport:
    def __init__(self) -> None:
        self.delivered: list[bytes] = []
        self.calls: list[str] = []
        self.paused = True

    async def resume_outbound(self) -> None:
        self.paused = False
        self.calls.append("resume")

    async def pause_outbound(self) -> None:
        self.paused = True
        self.calls.append("pause")

    async def output_stream(self, audio: Any) -> None:
        async for chunk in audio:
            if not self.paused:
                self.delivered.append(chunk)


async def _drive_takeover_once(
    runner: DialogueOrchestratorRunner,
    *,
    timeout: float = 1.0,
) -> None:
    task = asyncio.create_task(runner._consume_takeover_q())
    try:
        await asyncio.wait_for(_wait_for_delivery(runner), timeout=timeout)
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


async def _wait_for_delivery(runner: DialogueOrchestratorRunner) -> None:
    while True:
        transport = runner._merchant_transport
        if transport.delivered:
            return
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_user_channel_takeover_queues_passthrough_id_tuple() -> None:
    sent: list[dict[str, Any]] = []
    takeover_q: asyncio.Queue = asyncio.Queue()

    async def send_json(frame: dict[str, Any]) -> None:
        sent.append(frame)

    channel = WebSocketUserChannel(
        send_json=send_json,
        text_input_queue=asyncio.Queue(),
        ack_clarification_queue=asyncio.Queue(),
        get_phase=lambda: TaskPhase.EXECUTION_ACTIVE,
        user_takeover_queue=takeover_q,
    )

    await channel._text_q.put(("Hello", "en", "user_takeover"))
    assert await channel.dispatch_one_input() is None

    frame = sent[0]
    queued = await takeover_q.get()
    assert queued == ("Hello", "en", frame["id"])
    assert frame["type"] == "transcript_update"
    assert frame["role"] == "user_takeover_passthrough"
    assert frame["subtype"] == "user_takeover_passthrough"


@pytest.mark.asyncio
async def test_runner_consume_takeover_q_translates_cross_lingual() -> None:
    state = TaskState(
        session_id="s",
        user_task_description="t",
        phase=TaskPhase.EXECUTION_ACTIVE,
        user_lang="en",
        merchant_lang="zh",
        user_takeover_active=True,
    )
    runner = _build_runner_for_test(
        Session(session_id="s", task_description="t", task_state=state)
    )
    order: list[str] = []
    spoken: list[tuple[str, str]] = []
    events: list[dict[str, Any]] = []
    runner._takeover_q = asyncio.Queue()
    runner._takeover_q.put_nowait(("Hello", "en", "pf-1"))
    runner._merchant_lang_supplier = lambda: "zh"
    runner._merchant_transport = _MerchantTransport()
    runner._merchant_tts = _MerchantTTS(spoken, order)
    runner._llm = _FakeLLM(chunks=[TextDelta("你好")])
    runner._user_channel = _PushChannel(events, order)
    runner._orchestrator = _OrchestratorWithSegment()

    await _drive_takeover_once(runner)

    assert spoken == [("你好", "zh")]
    assert runner._merchant_transport.delivered == [b"voice"]
    assert order == ["translation_frame", "merchant_tts"]
    assert events[0]["event"] == "transcript_update"
    assert events[0]["role"] == "ai_to_merchant"
    assert events[0]["subtype"] == "translation"
    assert events[0]["parent_id"] == "pf-1"
    assert events[0]["segment_id"] == "seg-1"
    assert events[0]["text"] == "你好"


@pytest.mark.asyncio
async def test_runner_consume_takeover_q_same_lang_no_translation_frame() -> None:
    state = TaskState(
        session_id="s",
        user_task_description="t",
        phase=TaskPhase.EXECUTION_ACTIVE,
        user_lang="zh",
        merchant_lang="zh",
        user_takeover_active=True,
    )
    runner = _build_runner_for_test(
        Session(session_id="s", task_description="t", task_state=state)
    )
    order: list[str] = []
    spoken: list[tuple[str, str]] = []
    events: list[dict[str, Any]] = []
    llm = _FakeLLM(chunks=[TextDelta("ignored")])
    runner._takeover_q = asyncio.Queue()
    runner._takeover_q.put_nowait(("你好", "zh", "pf-1"))
    runner._merchant_lang_supplier = lambda: "zh"
    runner._merchant_transport = _MerchantTransport()
    runner._merchant_tts = _MerchantTTS(spoken, order)
    runner._llm = llm
    runner._user_channel = _PushChannel(events, order)
    runner._orchestrator = _OrchestratorWithSegment()

    await _drive_takeover_once(runner)

    assert spoken == [("你好", "zh")]
    assert events == []
    assert llm.call_count == 0


@pytest.mark.asyncio
async def test_runner_consume_takeover_q_relay_failure_falls_back_to_original() -> None:
    state = TaskState(
        session_id="s",
        user_task_description="t",
        phase=TaskPhase.EXECUTION_ACTIVE,
        user_lang="en",
        merchant_lang="zh",
        user_takeover_active=True,
    )
    runner = _build_runner_for_test(
        Session(session_id="s", task_description="t", task_state=state)
    )
    order: list[str] = []
    spoken: list[tuple[str, str]] = []
    events: list[dict[str, Any]] = []
    runner._takeover_q = asyncio.Queue()
    runner._takeover_q.put_nowait(("Hello", "en", "pf-1"))
    runner._merchant_lang_supplier = lambda: "zh"
    runner._merchant_transport = _MerchantTransport()
    runner._merchant_tts = _MerchantTTS(spoken, order)
    runner._llm = _FakeLLM(raise_exc=RuntimeError("translation unavailable"))
    runner._user_channel = _PushChannel(events, order)
    runner._orchestrator = _OrchestratorWithSegment()

    await _drive_takeover_once(runner)

    assert spoken == [("Hello", "zh")]
    assert events == []
