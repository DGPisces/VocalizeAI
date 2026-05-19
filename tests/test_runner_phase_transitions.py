from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Any

import pytest
from pydantic import ValidationError

from vocalize.dialogue.state import (
    CallbackEntry,
    ReadinessVerdict,
    SlotAssumption,
    TaskPhase,
    TaskState,
)
from vocalize.dialogue.user_channel import WebSocketUserChannel
from vocalize.server.runner import (
    DialogueOrchestratorRunner,
    _CONTROL_RECONNECT_PHASES,
    _RoleTaggedTransport,
    _translate_event,
)
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
    return runner


async def _make_channel(sent: list[dict[str, Any]]) -> WebSocketUserChannel:
    async def send_json(frame: dict[str, Any]) -> None:
        sent.append(frame)

    return WebSocketUserChannel(
        send_json=send_json,
        text_input_queue=asyncio.Queue(),
        ack_clarification_queue=asyncio.Queue(),
    )


@pytest.mark.asyncio
async def test_hangup_with_uncertain_assumptions_transitions_to_post_call_review() -> None:
    state = TaskState(
        session_id="s",
        user_task_description="t",
        phase=TaskPhase.EXECUTION_ACTIVE,
        uncertain_assumptions=[
            SlotAssumption(
                id="a-1",
                slot="party_size",
                question="How many?",
                assumed_value=4,
                source="user_timeout",
                created_at=datetime.now(timezone.utc),
            )
        ],
    )
    session = Session(session_id="s", task_description="t", task_state=state)
    sent: list[dict[str, Any]] = []
    channel = await _make_channel(sent)
    runner = _build_runner_for_test(session)

    await runner._handle_hangup(channel=channel)

    assert state.phase == TaskPhase.POST_CALL_REVIEW
    assert {
        "type": "phase_change",
        "previous": "execution_active",
        "current": "post_call_review",
    } in sent
    assert not runner.stop.is_set()


@pytest.mark.asyncio
async def test_hangup_without_uncertain_assumptions_transitions_to_completed() -> None:
    state = TaskState(
        session_id="s",
        user_task_description="t",
        phase=TaskPhase.EXECUTION_ACTIVE,
    )
    session = Session(session_id="s", task_description="t", task_state=state)
    sent: list[dict[str, Any]] = []
    channel = await _make_channel(sent)
    runner = _build_runner_for_test(session)

    await runner._handle_hangup(channel=channel)

    assert state.phase == TaskPhase.COMPLETED
    assert {
        "type": "phase_change",
        "previous": "execution_active",
        "current": "completed",
    } in sent
    assert runner.stop.is_set()


@pytest.mark.asyncio
async def test_hangup_outside_execution_active_leaves_phase_unchanged() -> None:
    state = TaskState(
        session_id="s",
        user_task_description="t",
        phase=TaskPhase.POST_CALL_REVIEW,
    )
    session = Session(session_id="s", task_description="t", task_state=state)
    sent: list[dict[str, Any]] = []
    channel = await _make_channel(sent)
    runner = _build_runner_for_test(session)

    await runner._handle_hangup(channel=channel)

    assert state.phase == TaskPhase.POST_CALL_REVIEW
    assert not any(frame["type"] == "phase_change" for frame in sent)
    assert runner.stop.is_set()


@pytest.mark.asyncio
async def test_ended_mode_transitions_to_completed() -> None:
    state = TaskState(
        session_id="s",
        user_task_description="t",
        phase=TaskPhase.POST_CALL_REVIEW,
    )
    session = Session(session_id="s", task_description="t", task_state=state)
    sent: list[dict[str, Any]] = []
    channel = await _make_channel(sent)
    runner = _build_runner_for_test(session)

    await runner._handle_mode_ended(channel=channel)

    assert state.phase == TaskPhase.COMPLETED
    assert {
        "type": "phase_change",
        "previous": "post_call_review",
        "current": "completed",
    } in sent
    assert runner.stop.is_set()


def test_translate_event_state_update_passthrough() -> None:
    event = {
        "event": "state_update",
        "diff": {"relay_failed": True, "original_id": "t-1"},
    }

    assert _translate_event(event) == event


def test_cache_merchant_transcript_eviction_keeps_latest_200() -> None:
    session = Session(session_id="s", task_description="t")
    runner = _build_runner_for_test(session)

    for i in range(201):
        runner._cache_merchant_transcript(
            id=f"t-{i}",
            text=f"text-{i}",
            lang="en",
        )

    assert len(runner._merchant_transcript_cache) == 200
    assert "t-0" not in runner._merchant_transcript_cache
    assert runner._merchant_transcript_cache["t-1"] == ("text-1", "en")
    assert runner._merchant_transcript_cache["t-200"] == ("text-200", "en")


def test_consume_pending_hints_drains_hint_queue() -> None:
    session = Session(session_id="s", task_description="t")
    runner = _build_runner_for_test(session)
    runner._hint_q = asyncio.Queue()
    runner._hint_q.put_nowait(("they have a private room", "en"))
    runner._hint_q.put_nowait(("we want one", "en"))

    assert runner.consume_pending_hints() == [
        ("they have a private room", "en"),
        ("we want one", "en"),
    ]
    assert runner.consume_pending_hints() == []


@pytest.mark.asyncio
async def test_user_takeover_text_reaches_merchant_tts() -> None:
    state = TaskState(
        session_id="s",
        user_task_description="t",
        phase=TaskPhase.EXECUTION_ACTIVE,
        user_lang="en",
        merchant_lang="en",
        user_takeover_active=True,
    )
    session = Session(session_id="s", task_description="t", task_state=state)

    spoken: list[tuple[str, str]] = []
    delivered: list[bytes] = []
    transport_calls: list[str] = []
    spoken_event = asyncio.Event()

    class _MerchantTTS:
        def stream_synthesize(self, chunks: Any) -> Any:
            async def _gen() -> Any:
                async for chunk in chunks:
                    spoken.append((chunk.text, chunk.language))
                    spoken_event.set()
                    yield b"voice"

            return _gen()

    class _MerchantTransport:
        def __init__(self) -> None:
            self.paused = True

        async def resume_outbound(self) -> None:
            self.paused = False
            transport_calls.append("resume")

        async def pause_outbound(self) -> None:
            self.paused = True
            transport_calls.append("pause")

        async def output_stream(self, audio: Any) -> None:
            async for chunk in audio:
                if not self.paused:
                    delivered.append(chunk)

    runner = _build_runner_for_test(session)
    runner._merchant_tts = _MerchantTTS()
    runner._merchant_transport = _MerchantTransport()
    runner._takeover_q = asyncio.Queue()
    runner._merchant_lang_supplier = lambda: state.merchant_lang or "zh"

    runner._takeover_q.put_nowait(("yes please", "en", "pf-1"))
    consumer = asyncio.create_task(runner._consume_takeover_q())
    await asyncio.wait_for(spoken_event.wait(), timeout=1.0)
    consumer.cancel()
    try:
        await consumer
    except asyncio.CancelledError:
        pass

    assert spoken == [("yes please", "en")]
    assert delivered == [b"voice"]
    assert transport_calls == ["resume", "pause"]


@pytest.mark.asyncio
async def test_role_tagged_transport_keeps_overlapping_stream_roles() -> None:
    from vocalize.transports.web import WebUserTransport

    sent: list[tuple[str, bytes]] = []
    user_second_chunk = asyncio.Event()
    release_user_second_chunk = asyncio.Event()

    async def outbound_send(role: str, pcm: bytes) -> None:
        sent.append((role, pcm))

    async def user_audio():
        yield b"user-1"
        user_second_chunk.set()
        await release_user_second_chunk.wait()
        yield b"user-2"

    async def merchant_audio():
        yield b"merchant-1"

    transport = WebUserTransport(
        inbound_queue=asyncio.Queue(),
        outbound_send=outbound_send,
    )
    user_transport = _RoleTaggedTransport(transport, "ai_to_user")
    merchant_transport = _RoleTaggedTransport(transport, "ai_to_merchant")

    user_task = asyncio.create_task(user_transport.output_stream(user_audio()))
    await asyncio.wait_for(user_second_chunk.wait(), timeout=1.0)
    await merchant_transport.output_stream(merchant_audio())
    release_user_second_chunk.set()
    await asyncio.wait_for(user_task, timeout=1.0)

    assert sent == [
        ("ai_to_user", b"user-1"),
        ("ai_to_merchant", b"merchant-1"),
        ("ai_to_user", b"user-2"),
    ]


@pytest.mark.asyncio
async def test_on_demand_translate_emits_translation_transcript() -> None:
    state = TaskState(
        session_id="s",
        user_task_description="t",
        phase=TaskPhase.EXECUTION_ACTIVE,
        user_lang="zh",
        merchant_lang="en",
        auto_translate_merchant=False,
    )
    session = Session(session_id="s", task_description="t", task_state=state)
    sent: list[dict[str, Any]] = []
    channel = await _make_channel(sent)

    class _LLMReturning:
        async def stream_chat(self, *, messages: Any) -> Any:
            class _Chunk:
                text = "你好"

            yield _Chunk()

    runner = _build_runner_for_test(session)
    runner._llm = _LLMReturning()
    runner._merchant_transcript_cache = {"t-42": ("hello", "en")}

    payload = json.dumps({"type": "on_demand_translate", "transcript_id": "t-42"})
    await runner._handle_on_demand_translate(payload, channel=channel)

    matching = [
        frame for frame in sent
        if frame.get("type") == "transcript_update"
        and frame.get("subtype") == "translation"
        and frame.get("parent_id") == "t-42"
    ]
    assert len(matching) == 1
    assert matching[0]["text"] == "你好"
    assert matching[0]["lang"] == "zh"
    assert matching[0]["role"] == "ai_to_user"


@pytest.mark.asyncio
async def test_on_demand_translate_unknown_id_emits_error() -> None:
    state = TaskState(
        session_id="s",
        user_task_description="t",
        phase=TaskPhase.EXECUTION_ACTIVE,
    )
    session = Session(session_id="s", task_description="t", task_state=state)
    sent: list[dict[str, Any]] = []
    channel = await _make_channel(sent)
    runner = _build_runner_for_test(session)
    runner._merchant_transcript_cache = {}

    payload = json.dumps({
        "type": "on_demand_translate",
        "transcript_id": "missing",
    })
    await runner._handle_on_demand_translate(payload, channel=channel)

    assert any(
        frame.get("type") == "error" and frame.get("code") == 1003
        for frame in sent
    )


@pytest.mark.asyncio
async def test_on_demand_translate_failure_emits_error() -> None:
    state = TaskState(
        session_id="s",
        user_task_description="t",
        phase=TaskPhase.EXECUTION_ACTIVE,
        user_lang="zh",
        merchant_lang="en",
    )
    session = Session(session_id="s", task_description="t", task_state=state)
    sent: list[dict[str, Any]] = []
    channel = await _make_channel(sent)

    class _LLMFailing:
        async def stream_chat(self, *, messages: Any) -> Any:
            raise RuntimeError("translation unavailable")
            yield

    runner = _build_runner_for_test(session)
    runner._llm = _LLMFailing()
    runner._merchant_transcript_cache = {"t-42": ("hello", "en")}

    payload = json.dumps({"type": "on_demand_translate", "transcript_id": "t-42"})
    await runner._handle_on_demand_translate(payload, channel=channel)

    assert any(
        frame.get("type") == "error" and frame.get("code") == 1004
        for frame in sent
    )


@pytest.mark.asyncio
async def test_clarification_timeout_appends_callback_entry() -> None:
    state = TaskState(
        session_id="s",
        user_task_description="t",
        phase=TaskPhase.NEEDS_CLARIFICATION,
    )
    assumption = state.record_uncertain_assumption(
        slot="party_size",
        question="How many?",
        assumed_value=4,
        source="user_timeout",
    )
    session = Session(session_id="s", task_description="t", task_state=state)
    sent: list[dict[str, Any]] = []
    channel = await _make_channel(sent)
    runner = _build_runner_for_test(session)

    await runner._handle_clarification_timeout(
        assumption_id=assumption.id,
        state=state,
        channel=channel,
    )

    callback = state.pending_callbacks[-1]
    assert callback.status == "queued"
    assert callback.assumption_id == assumption.id
    assert callback.created_at.tzinfo is timezone.utc
    assert assumption.callback_id == callback.id
    assert any(
        frame.get("type") == "pending_callback_added"
        and frame.get("callback", {}).get("id") == callback.id
        for frame in sent
    )


@pytest.mark.asyncio
async def test_clarification_timeout_transitions_to_post_call_review() -> None:
    state = TaskState(
        session_id="s",
        user_task_description="t",
        phase=TaskPhase.NEEDS_CLARIFICATION,
    )
    assumption = state.record_uncertain_assumption(
        slot="party_size",
        question="How many?",
        assumed_value=4,
        source="user_timeout",
    )
    session = Session(session_id="s", task_description="t", task_state=state)
    sent: list[dict[str, Any]] = []
    channel = await _make_channel(sent)
    runner = _build_runner_for_test(session)

    await runner._handle_clarification_timeout(
        assumption_id=assumption.id,
        state=state,
        channel=channel,
    )

    assert state.phase == TaskPhase.POST_CALL_REVIEW
    assert {
        "type": "phase_change",
        "previous": "needs_clarification",
        "current": "post_call_review",
    } in sent


@pytest.mark.asyncio
async def test_clarification_timeout_from_needs_clarification_uses_new_legal_edge() -> None:
    state = TaskState(
        session_id="s",
        user_task_description="t",
        phase=TaskPhase.NEEDS_CLARIFICATION,
    )
    assumption = state.record_uncertain_assumption(
        slot="party_size",
        question="How many?",
        assumed_value=4,
        source="user_timeout",
    )
    session = Session(session_id="s", task_description="t", task_state=state)
    sent: list[dict[str, Any]] = []
    channel = await _make_channel(sent)
    runner = _build_runner_for_test(session)

    await runner._handle_clarification_timeout(
        assumption_id=assumption.id,
        state=state,
        channel=channel,
    )

    assert state.phase == TaskPhase.POST_CALL_REVIEW


@pytest.mark.asyncio
async def test_trigger_callback_runs_callback_and_emits_segment_transcripts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from vocalize.dialogue.state import CallbackEntry
    from vocalize.llm.base import FinishChunk, TextDelta, ToolCallDelta

    state = TaskState(
        session_id="s",
        user_task_description="t",
        phase=TaskPhase.POST_CALL_REVIEW,
        user_lang="zh",
        merchant_lang="zh",
    )
    assumption = SlotAssumption(
        id="a-1",
        slot="party_size",
        question="How many?",
        assumed_value=4,
        source="user_timeout",
        created_at=datetime.now(timezone.utc),
    )
    state.uncertain_assumptions.append(assumption)
    callback = CallbackEntry(
        id="cb-1",
        assumption_id="a-1",
        correction="6",
        created_at=datetime.now(timezone.utc),
    )
    state.pending_callbacks.append(callback)
    session = Session(session_id="s", task_description="t", task_state=state)
    sent: list[dict[str, Any]] = []
    channel = await _make_channel(sent)

    class _LLM:
        def __init__(self) -> None:
            self.turn = 0

        async def stream_chat(self, *, messages: Any) -> Any:
            self.turn += 1
            if self.turn == 1:
                yield TextDelta(text="刚才说错了一点，其实是 6 位。")
                yield FinishChunk(reason="stop")
            else:
                yield ToolCallDelta(
                    tool_call_index=0,
                    tool_call_id="x",
                    name="finalize_task",
                    arguments_delta=json.dumps({"success": True}),
                )
                yield FinishChunk(reason="tool_calls")

    runner = _build_runner_for_test(session)
    runner._llm = _LLM()
    merchant_spoken: list[tuple[str, str]] = []

    async def fake_reply() -> str:
        return "好的，记下了"

    async def fake_merchant_speak(text: str, lang: str) -> None:
        merchant_spoken.append((text, lang))

    monkeypatch.setattr(runner, "_await_callback_merchant_reply", fake_reply)
    monkeypatch.setattr(runner, "_merchant_speak", fake_merchant_speak)
    payload = json.dumps({"type": "trigger_callback", "callback_id": "cb-1"})

    await runner._handle_trigger_callback(payload, channel=channel)

    assert state.phase == TaskPhase.POST_CALL_REVIEW
    assert callback.status == "completed"
    assert callback.transcript_segment_id is not None
    transcript_frames = [
        frame for frame in sent
        if frame.get("type") == "transcript_update"
        and frame.get("subtype") == "callback_segment"
    ]
    assert [
        (frame["role"], frame["text"], frame["segment_id"])
        for frame in transcript_frames
    ] == [
        (
            "ai_to_merchant",
            "刚才说错了一点，其实是 6 位。",
            callback.transcript_segment_id,
        ),
        ("merchant_to_ai", "好的，记下了", callback.transcript_segment_id),
    ]
    assert merchant_spoken == [("刚才说错了一点，其实是 6 位。", "zh")]
    assert {
        "type": "phase_change",
        "previous": "post_call_review",
        "current": "callback_active",
    } in sent
    assert {
        "type": "phase_change",
        "previous": "callback_active",
        "current": "post_call_review",
    } in sent


@pytest.mark.asyncio
async def test_trigger_callback_unknown_id_emits_error() -> None:
    state = TaskState(
        session_id="s",
        user_task_description="t",
        phase=TaskPhase.POST_CALL_REVIEW,
    )
    session = Session(session_id="s", task_description="t", task_state=state)
    sent: list[dict[str, Any]] = []
    channel = await _make_channel(sent)
    runner = _build_runner_for_test(session)
    payload = json.dumps({"type": "trigger_callback", "callback_id": "missing"})

    await runner._handle_trigger_callback(payload, channel=channel)

    assert any(
        frame.get("type") == "error" and frame.get("code") == 1005
        for frame in sent
    )


@pytest.mark.asyncio
async def test_trigger_callback_nonqueued_id_emits_error() -> None:
    from vocalize.dialogue.state import CallbackEntry

    state = TaskState(
        session_id="s",
        user_task_description="t",
        phase=TaskPhase.POST_CALL_REVIEW,
    )
    state.pending_callbacks.append(
        CallbackEntry(
            id="cb-1",
            assumption_id="a-1",
            correction="6",
            status="completed",
            created_at=datetime.now(timezone.utc),
        )
    )
    session = Session(session_id="s", task_description="t", task_state=state)
    sent: list[dict[str, Any]] = []
    channel = await _make_channel(sent)
    runner = _build_runner_for_test(session)
    payload = json.dumps({"type": "trigger_callback", "callback_id": "cb-1"})

    await runner._handle_trigger_callback(payload, channel=channel)

    assert any(
        frame.get("type") == "error" and frame.get("code") == 1008
        for frame in sent
    )


@pytest.mark.asyncio
async def test_trigger_callback_failure_emits_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from vocalize.dialogue.state import CallbackEntry
    from vocalize.llm.base import FinishChunk, TextDelta

    state = TaskState(
        session_id="s",
        user_task_description="t",
        phase=TaskPhase.POST_CALL_REVIEW,
        merchant_lang="zh",
    )
    state.pending_callbacks.append(
        CallbackEntry(
            id="cb-1",
            assumption_id="a-1",
            correction="6",
            created_at=datetime.now(timezone.utc),
        )
    )
    session = Session(session_id="s", task_description="t", task_state=state)
    sent: list[dict[str, Any]] = []
    channel = await _make_channel(sent)

    class _LLM:
        async def stream_chat(self, *, messages: Any) -> Any:
            yield TextDelta(text="Correction.")
            yield FinishChunk(reason="stop")

    runner = _build_runner_for_test(session)
    runner._llm = _LLM()

    async def fail_reply() -> str:
        raise RuntimeError("stt failed")

    async def fake_merchant_speak(text: str, lang: str) -> None:
        pass

    monkeypatch.setattr(runner, "_await_callback_merchant_reply", fail_reply)
    monkeypatch.setattr(runner, "_merchant_speak", fake_merchant_speak)

    await runner._handle_trigger_callback(
        json.dumps({"type": "trigger_callback", "callback_id": "cb-1"}),
        channel=channel,
    )

    assert state.phase == TaskPhase.POST_CALL_REVIEW
    assert state.pending_callbacks[0].status == "queued"
    assert any(
        frame.get("type") == "error" and frame.get("code") == 1010
        for frame in sent
    )
    assert any(
        frame.get("type") == "state_update"
        and frame.get("diff", {}).get("pending_callbacks", [{}])[0].get("status")
        == "queued"
        for frame in sent
    )


@pytest.mark.asyncio
async def test_callback_reply_wait_times_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    from vocalize.server import runner as runner_module

    monkeypatch.setattr(runner_module, "CALLBACK_REPLY_TIMEOUT_S", 0.01)
    session = Session(session_id="s", task_description="t")
    runner = _build_runner_for_test(session)

    class _SilentSTT:
        async def stream_transcribe(self, _audio):
            await asyncio.sleep(3600)
            if False:
                yield None

    class _Transport:
        async def input_stream(self):
            if False:
                yield b""

    runner._merchant_pipeline = SimpleNamespace(
        _stt=_SilentSTT(),
        _transport=_Transport(),
    )

    with pytest.raises(RuntimeError, match="timed out"):
        await runner._await_callback_merchant_reply()


@pytest.mark.asyncio
async def test_runner_uses_first_text_input_as_missing_task_description() -> None:
    session = Session(session_id="s", task_description=None)
    runner = _build_runner_for_test(session)
    sent: list[dict[str, Any]] = []
    text_q: asyncio.Queue = asyncio.Queue()
    text_q.put_nowait(("帮我订海底捞", "zh", "default"))

    async def send_json(frame: dict[str, Any]) -> None:
        sent.append(frame)

    channel = WebSocketUserChannel(
        send_json=send_json,
        text_input_queue=text_q,
        ack_clarification_queue=asyncio.Queue(),
        get_phase=lambda: TaskPhase.DRAFT,
    )

    task_text = await runner._resolve_task_text(channel=channel)

    assert task_text == "帮我订海底捞"
    assert session.task_description == "帮我订海底捞"
    assert sent == []


def test_merchant_transcript_cache_persists_across_runner_instances() -> None:
    session = Session(session_id="s", task_description="t")

    def fail_pipeline_factory(_transport):
        raise AssertionError("pipeline construction not needed")

    first = DialogueOrchestratorRunner(
        session=session,
        user_pipeline_factory=fail_pipeline_factory,
        merchant_pipeline_factory=fail_pipeline_factory,
    )
    first._cache_merchant_transcript(id="m-1", text="hello", lang="en")

    second = DialogueOrchestratorRunner(
        session=session,
        user_pipeline_factory=fail_pipeline_factory,
        merchant_pipeline_factory=fail_pipeline_factory,
    )

    assert second._merchant_transcript_cache["m-1"] == ("hello", "en")


@pytest.mark.asyncio
async def test_merchant_text_inject_rejected_when_test_frames_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("VOCALIZE_ENABLE_TEST_FRAMES", raising=False)
    session = Session(session_id="s", task_description="t")
    sent: list[dict[str, Any]] = []
    channel = await _make_channel(sent)
    runner = _build_runner_for_test(session)

    await runner._dispatch_text_frame(
        json.dumps({
            "type": "merchant_text_inject",
            "text": "Hello",
            "scenario_id": "handover-readiness",
            "seed": "merchant-direct",
        }),
        channel=channel,
    )

    assert any(
        frame.get("type") == "error"
        and frame.get("code") == 1013
        and "VOCALIZE_ENABLE_TEST_FRAMES" in frame.get("message_en", "")
        for frame in sent
    )


@pytest.mark.asyncio
async def test_merchant_text_inject_dispatches_when_test_frames_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VOCALIZE_ENABLE_TEST_FRAMES", "1")
    state = TaskState(
        session_id="s",
        user_task_description="t",
        phase=TaskPhase.EXECUTION_ACTIVE,
    )
    session = Session(session_id="s", task_description="t", task_state=state)
    sent: list[dict[str, Any]] = []
    channel = await _make_channel(sent)
    runner = _build_runner_for_test(session)
    calls: list[tuple[str, Any]] = []

    class _FakeOrchestrator:
        _merchant = object()
        current_segment_id = "seg-1"

        def _prepend_user_hints(self, text: str) -> str:
            calls.append(("hints", text))
            return f"hinted: {text}"

        async def _emit_merchant_transcript(self, text: str) -> None:
            calls.append(("transcript", text))

        async def _drive_turn(self, channel_obj: Any, *, user_text: str) -> None:
            calls.append(("drive", (channel_obj, user_text)))

    fake_orchestrator = _FakeOrchestrator()
    runner._orchestrator = fake_orchestrator

    await runner._dispatch_text_frame(
        json.dumps({
            "type": "merchant_text_inject",
            "text": "Hello",
            "scenario_id": "handover-readiness",
            "seed": "merchant-direct",
            "lang_hint": "en",
        }),
        channel=channel,
    )

    assert calls == [
        ("transcript", "Hello"),
        ("hints", "Hello"),
        ("drive", (fake_orchestrator._merchant, "hinted: Hello")),
    ]
    assert not any(frame.get("type") == "error" for frame in sent)


@pytest.mark.asyncio
async def test_runner_recovers_callback_active_state_on_reconnect() -> None:
    from vocalize.transports.web import WebUserTransport

    state = TaskState(
        session_id="s",
        user_task_description="t",
        phase=TaskPhase.CALLBACK_ACTIVE,
    )
    callback = CallbackEntry(
        id="cb-1",
        assumption_id="a-1",
        correction="6",
        status="in_progress",
        created_at=datetime.now(timezone.utc),
    )
    state.pending_callbacks.append(callback)
    session = Session(session_id="s", task_description="t", task_state=state)
    sent: list[dict[str, Any]] = []
    channel = await _make_channel(sent)

    def fail_pipeline_factory(_transport):
        raise AssertionError("callback reconnect recovery should not build pipelines")

    runner = DialogueOrchestratorRunner(
        session=session,
        user_pipeline_factory=fail_pipeline_factory,
        merchant_pipeline_factory=fail_pipeline_factory,
    )
    transport = WebUserTransport(
        inbound_queue=asyncio.Queue(),
        outbound_send=lambda role, pcm: asyncio.sleep(0),
    )

    task = asyncio.create_task(runner.run(channel=channel, transport=transport))
    await asyncio.sleep(0.05)
    runner.stop.set()
    await asyncio.wait_for(task, timeout=2.0)

    assert state.phase == TaskPhase.POST_CALL_REVIEW
    assert callback.status == "queued"
    assert callback.started_at is None
    assert callback.completed_at is None
    assert {
        "type": "phase_change",
        "previous": "callback_active",
        "current": "post_call_review",
    } in sent
    assert any(
        frame.get("type") == "state_update"
        and frame.get("diff", {}).get("pending_callbacks", [{}])[0].get("status")
        == "queued"
        for frame in sent
    )


def test_control_reconnect_phases_includes_execution_active_and_clarification() -> None:
    assert {
        TaskPhase.EXECUTION_ACTIVE,
        TaskPhase.NEEDS_CLARIFICATION,
        TaskPhase.AWAIT_USER_CLARIFICATION,
    }.issubset(_CONTROL_RECONNECT_PHASES)


@pytest.mark.asyncio
async def test_recover_execution_active_reconnect_marks_segment_and_transitions() -> None:
    state = TaskState(
        session_id="s",
        user_task_description="t",
        phase=TaskPhase.EXECUTION_ACTIVE,
        merchant_held=True,
        clarification_holds_used=2,
    )
    segment = state.start_call_segment()
    session = Session(session_id="s", task_description="t", task_state=state)
    sent: list[dict[str, Any]] = []
    channel = await _make_channel(sent)
    runner = _build_runner_for_test(session)

    await runner._recover_execution_active_reconnect(state=state, channel=channel)

    assert state.phase == TaskPhase.POST_CALL_REVIEW
    assert state.merchant_held is False
    assert state.clarification_holds_used == 0
    assert state.call_segments[-1].interrupted is True
    assert state.call_segments[-1].interrupt_reason == "ws_close"
    assert {
        "type": "segment_interrupted",
        "segment_id": segment.id,
        "reason": "ws_close",
    } in sent
    assert {
        "type": "phase_change",
        "previous": "execution_active",
        "current": "post_call_review",
    } in sent


@pytest.mark.asyncio
async def test_recover_execution_active_reconnect_noop_for_non_matching_phase() -> None:
    state = TaskState(
        session_id="s",
        user_task_description="t",
        phase=TaskPhase.COMPLETED,
    )
    session = Session(session_id="s", task_description="t", task_state=state)
    sent: list[dict[str, Any]] = []
    channel = await _make_channel(sent)
    runner = _build_runner_for_test(session)

    await runner._recover_execution_active_reconnect(state=state, channel=channel)

    assert state.phase == TaskPhase.COMPLETED
    assert sent == []


@pytest.mark.asyncio
async def test_recover_execution_active_reconnect_from_needs_clarification_uses_new_legal_edge() -> None:
    state = TaskState(
        session_id="s",
        user_task_description="t",
        phase=TaskPhase.NEEDS_CLARIFICATION,
    )
    session = Session(session_id="s", task_description="t", task_state=state)
    sent: list[dict[str, Any]] = []
    channel = await _make_channel(sent)
    runner = _build_runner_for_test(session)

    await runner._recover_execution_active_reconnect(state=state, channel=channel)

    assert state.phase == TaskPhase.POST_CALL_REVIEW
    assert {
        "type": "phase_change",
        "previous": "needs_clarification",
        "current": "post_call_review",
    } in sent


@pytest.mark.asyncio
async def test_orchestrator_starts_segment_on_execution_active_entry() -> None:
    from vocalize.dialogue.orchestrator import DialogueOrchestrator

    state = TaskState(session_id="s", user_task_description="t")
    events: list[dict[str, Any]] = []
    orch = DialogueOrchestrator.__new__(DialogueOrchestrator)
    orch._state = state
    orch._current_segment_id = None

    async def _emit(event: dict[str, Any]) -> None:
        events.append(event)

    orch._emit = _emit

    await orch._start_call_segment()

    assert len(state.call_segments) == 1
    assert orch._current_segment_id == state.call_segments[0].id
    assert events == [
        {
            "event": "call_segment_added",
            "segment": state.call_segments[0].model_dump(mode="json"),
        }
    ]


@pytest.mark.asyncio
async def test_orchestrator_ends_segment_on_post_call_review_exit_path() -> None:
    from vocalize.dialogue.orchestrator import DialogueOrchestrator

    state = TaskState(session_id="s", user_task_description="t")
    segment = state.start_call_segment()
    events: list[dict[str, Any]] = []
    orch = DialogueOrchestrator.__new__(DialogueOrchestrator)
    orch._state = state
    orch._current_segment_id = segment.id

    async def _emit(event: dict[str, Any]) -> None:
        events.append(event)

    orch._emit = _emit

    await orch._end_current_call_segment()

    assert state.call_segments[-1].ended_at is not None
    assert state.call_segments[-1].interrupted is False
    assert state.call_segments[-1].interrupt_reason is None
    assert orch._current_segment_id is None
    assert events == []


@pytest.mark.asyncio
async def test_confirm_assumption_wrong_emits_pending_callback_added() -> None:
    state = TaskState(
        session_id="s",
        user_task_description="t",
        phase=TaskPhase.POST_CALL_REVIEW,
    )
    assumption = state.record_uncertain_assumption(
        slot="party_size",
        question="How many?",
        assumed_value=4,
        source="user_timeout",
    )
    session = Session(session_id="s", task_description="t", task_state=state)
    sent: list[dict[str, Any]] = []
    channel = await _make_channel(sent)
    runner = _build_runner_for_test(session)
    payload = json.dumps({
        "type": "confirm_assumption",
        "assumption_id": assumption.id,
        "choice": "wrong",
        "correction": "6",
        "note": "checked twice",
    })

    await runner._handle_confirm_assumption(payload, channel=channel)

    assert state.uncertain_assumptions[0].status == "corrected"
    assert state.pending_callbacks[0].correction == "6"
    assert state.pending_callbacks[0].note == "checked twice"
    assert any(
        frame.get("type") == "pending_callback_added"
        and frame["callback"]["id"] == state.pending_callbacks[0].id
        for frame in sent
    )
    assert any(
        frame.get("type") == "state_update"
        and frame.get("diff", {}).get("uncertain_assumptions", [])[0]["status"]
        == "corrected"
        for frame in sent
    )


@pytest.mark.asyncio
async def test_confirm_assumption_correct_emits_assumption_state_update() -> None:
    state = TaskState(
        session_id="s",
        user_task_description="t",
        phase=TaskPhase.POST_CALL_REVIEW,
    )
    assumption = state.record_uncertain_assumption(
        slot="party_size",
        question="How many?",
        assumed_value=4,
        source="user_timeout",
    )
    session = Session(session_id="s", task_description="t", task_state=state)
    sent: list[dict[str, Any]] = []
    channel = await _make_channel(sent)
    runner = _build_runner_for_test(session)

    await runner._handle_confirm_assumption(
        json.dumps({
            "type": "confirm_assumption",
            "assumption_id": assumption.id,
            "choice": "correct",
            "correction": None,
        }),
        channel=channel,
    )

    assert state.uncertain_assumptions[0].status == "confirmed"
    assert any(
        frame.get("type") == "state_update"
        and frame.get("diff", {}).get("uncertain_assumptions", [])[0]["status"]
        == "confirmed"
        for frame in sent
    )


@pytest.mark.asyncio
async def test_confirm_assumption_unknown_id_emits_error() -> None:
    state = TaskState(
        session_id="s",
        user_task_description="t",
        phase=TaskPhase.POST_CALL_REVIEW,
    )
    session = Session(session_id="s", task_description="t", task_state=state)
    sent: list[dict[str, Any]] = []
    channel = await _make_channel(sent)
    runner = _build_runner_for_test(session)
    payload = json.dumps({
        "type": "confirm_assumption",
        "assumption_id": "missing",
        "choice": "correct",
    })

    await runner._handle_confirm_assumption(payload, channel=channel)

    assert any(
        frame.get("type") == "error" and frame.get("code") == 1006
        for frame in sent
    )


@pytest.mark.asyncio
async def test_confirm_assumption_wrong_without_correction_emits_error() -> None:
    state = TaskState(
        session_id="s",
        user_task_description="t",
        phase=TaskPhase.POST_CALL_REVIEW,
    )
    assumption = state.record_uncertain_assumption(
        slot="party_size",
        question="How many?",
        assumed_value=4,
        source="user_timeout",
    )
    session = Session(session_id="s", task_description="t", task_state=state)
    sent: list[dict[str, Any]] = []
    channel = await _make_channel(sent)
    runner = _build_runner_for_test(session)
    payload = json.dumps({
        "type": "confirm_assumption",
        "assumption_id": assumption.id,
        "choice": "wrong",
    })

    await runner._handle_confirm_assumption(payload, channel=channel)

    assert state.pending_callbacks == []
    assert any(
        frame.get("type") == "error" and frame.get("code") == 1007
        for frame in sent
    )


@pytest.mark.asyncio
async def test_set_auto_translate_toggles_state() -> None:
    state = TaskState(
        session_id="s",
        user_task_description="t",
        phase=TaskPhase.EXECUTION_ACTIVE,
        auto_translate_merchant=True,
    )
    session = Session(session_id="s", task_description="t", task_state=state)
    sent: list[dict[str, Any]] = []
    channel = await _make_channel(sent)
    runner = _build_runner_for_test(session)
    payload = json.dumps({"type": "set_auto_translate", "value": False})

    await runner._handle_set_auto_translate(payload, channel=channel)

    assert state.auto_translate_merchant is False
    assert any(
        frame.get("type") == "state_update"
        and frame.get("diff", {}).get("auto_translate_merchant") is False
        for frame in sent
    )
    assert session.auto_translate_merchant is False


@pytest.mark.asyncio
async def test_set_auto_translate_uses_validated_boolean_value() -> None:
    state = TaskState(
        session_id="s",
        user_task_description="t",
        phase=TaskPhase.EXECUTION_ACTIVE,
        auto_translate_merchant=True,
    )
    session = Session(session_id="s", task_description="t", task_state=state)
    sent: list[dict[str, Any]] = []
    channel = await _make_channel(sent)
    runner = _build_runner_for_test(session)
    payload = json.dumps({"type": "set_auto_translate", "value": "false"})

    await runner._handle_set_auto_translate(payload, channel=channel)

    assert state.auto_translate_merchant is False
    assert any(
        frame.get("type") == "state_update"
        and frame.get("diff", {}).get("auto_translate_merchant") is False
        for frame in sent
    )


@pytest.mark.asyncio
async def test_set_devices_updates_session_device_selection_only() -> None:
    session = Session(session_id="s", task_description="t")
    sent: list[dict[str, Any]] = []
    channel = await _make_channel(sent)
    runner = _build_runner_for_test(session)
    payload = json.dumps({
        "type": "set_devices",
        "input_id": "mic-1",
        "output_id": "speaker-2",
        "aec": False,
    })

    await runner._dispatch_text_frame(payload, channel=channel)

    assert session.device_selection.input_id == "mic-1"
    assert session.device_selection.output_id == "speaker-2"
    assert session.device_selection.aec is False
    assert sent == []


@pytest.mark.asyncio
async def test_set_devices_rejects_local_test_artifacts() -> None:
    session = Session(session_id="s", task_description="t")
    sent: list[dict[str, Any]] = []
    channel = await _make_channel(sent)
    runner = _build_runner_for_test(session)
    payload = json.dumps({
        "type": "set_devices",
        "input_id": "mic-1",
        "output_id": "speaker-2",
        "aec": True,
        "recording": "local-blob",
        "permission_status": "granted",
        "test_result": "passed",
    })

    with pytest.raises(ValidationError):
        await runner._dispatch_text_frame(payload, channel=channel)

    assert session.device_selection.input_id == ""
    assert session.device_selection.output_id == ""
    assert session.device_selection.aec is True
    assert sent == []


@pytest.mark.asyncio
async def test_cancel_callback_marks_server_state_cancelled() -> None:
    from vocalize.dialogue.state import CallbackEntry

    state = TaskState(
        session_id="s",
        user_task_description="t",
        phase=TaskPhase.POST_CALL_REVIEW,
    )
    callback = CallbackEntry(
        id="cb-1",
        assumption_id="a-1",
        correction="6",
        created_at=datetime.now(timezone.utc),
    )
    state.pending_callbacks.append(callback)
    session = Session(session_id="s", task_description="t", task_state=state)
    sent: list[dict[str, Any]] = []
    channel = await _make_channel(sent)
    runner = _build_runner_for_test(session)

    await runner._handle_cancel_callback(
        json.dumps({"type": "cancel_callback", "callback_id": "cb-1"}),
        channel=channel,
    )

    assert callback.status == "cancelled"
    assert any(
        frame.get("type") == "state_update"
        and frame.get("diff", {}).get("pending_callbacks", [{}])[0].get("status")
        == "cancelled"
        for frame in sent
    )


@pytest.mark.asyncio
async def test_handle_restore_callback_flips_cancelled_to_queued() -> None:
    state = TaskState(
        session_id="s",
        user_task_description="t",
        phase=TaskPhase.POST_CALL_REVIEW,
    )
    callback = CallbackEntry(
        id="cb-1",
        assumption_id="a-1",
        correction="6",
        status="cancelled",
        created_at=datetime.now(timezone.utc),
    )
    state.pending_callbacks.append(callback)
    session = Session(session_id="s", task_description="t", task_state=state)
    sent: list[dict[str, Any]] = []
    channel = await _make_channel(sent)
    runner = _build_runner_for_test(session)

    await runner._handle_restore_callback(
        json.dumps({"type": "restore_callback", "callback_id": "cb-1"}),
        channel=channel,
    )

    assert callback.status == "queued"
    assert any(
        frame.get("type") == "state_update"
        and frame.get("diff", {}).get("pending_callbacks", [{}])[0].get("status")
        == "queued"
        for frame in sent
    )
    assert not any(frame.get("type") == "error" for frame in sent)


@pytest.mark.asyncio
async def test_handle_restore_callback_error_on_non_cancelled() -> None:
    state = TaskState(
        session_id="s",
        user_task_description="t",
        phase=TaskPhase.POST_CALL_REVIEW,
        pending_callbacks=[
            CallbackEntry(
                id="cb-1",
                assumption_id="a-1",
                correction="6",
                created_at=datetime.now(timezone.utc),
            )
        ],
    )
    session = Session(session_id="s", task_description="t", task_state=state)
    sent: list[dict[str, Any]] = []
    channel = await _make_channel(sent)
    runner = _build_runner_for_test(session)

    await runner._handle_restore_callback(
        json.dumps({"type": "restore_callback", "callback_id": "cb-1"}),
        channel=channel,
    )

    assert state.pending_callbacks[0].status == "queued"
    assert sent[-1]["type"] == "error"
    assert sent[-1]["code"] == 1012


@pytest.mark.asyncio
async def test_handle_restore_callback_error_on_missing_callback() -> None:
    state = TaskState(
        session_id="s",
        user_task_description="t",
        phase=TaskPhase.POST_CALL_REVIEW,
    )
    session = Session(session_id="s", task_description="t", task_state=state)
    sent: list[dict[str, Any]] = []
    channel = await _make_channel(sent)
    runner = _build_runner_for_test(session)

    await runner._handle_restore_callback(
        json.dumps({"type": "restore_callback", "callback_id": "missing"}),
        channel=channel,
    )

    assert sent[-1]["type"] == "error"
    assert sent[-1]["code"] == 1005


@pytest.mark.asyncio
async def test_mode_takeover_on_pauses_merchant_outbound_and_acks() -> None:
    state = TaskState(
        session_id="s",
        user_task_description="t",
        phase=TaskPhase.EXECUTION_ACTIVE,
    )
    session = Session(session_id="s", task_description="t", task_state=state)
    sent: list[dict[str, Any]] = []
    channel = await _make_channel(sent)
    calls: list[str] = []

    class _MerchantTransport:
        async def pause_outbound(self) -> None:
            calls.append("pause")

        async def resume_outbound(self) -> None:
            calls.append("resume")

    runner = _build_runner_for_test(session)
    runner._merchant_transport = _MerchantTransport()

    await runner._handle_mode_takeover_on(channel=channel)

    assert state.user_takeover_active is True
    assert calls == ["pause"]
    assert {"type": "mode_ack", "mode": "user_takeover"} in sent


@pytest.mark.asyncio
async def test_mode_takeover_off_resumes_outbound_and_clears_pending_outputs() -> None:
    state = TaskState(
        session_id="s",
        user_task_description="t",
        phase=TaskPhase.EXECUTION_ACTIVE,
        user_takeover_active=True,
    )
    session = Session(session_id="s", task_description="t", task_state=state)
    sent: list[dict[str, Any]] = []
    channel = await _make_channel(sent)
    calls: list[str] = []

    class _MerchantTransport:
        async def pause_outbound(self) -> None:
            calls.append("pause")

        async def resume_outbound(self) -> None:
            calls.append("resume")

    runner = _build_runner_for_test(session)
    runner._merchant_transport = _MerchantTransport()
    runner._pending_ai_outputs = [("queued text", "en")]

    await runner._handle_mode_takeover_off(channel=channel)

    assert state.user_takeover_active is False
    assert runner._pending_ai_outputs == []
    assert calls == ["resume"]
    assert {"type": "mode_ack", "mode": "call_listening"} in sent


@pytest.mark.asyncio
async def test_merchant_speak_drops_ai_output_during_user_takeover() -> None:
    state = TaskState(
        session_id="s",
        user_task_description="t",
        phase=TaskPhase.EXECUTION_ACTIVE,
        user_takeover_active=True,
    )
    session = Session(session_id="s", task_description="t", task_state=state)
    runner = _build_runner_for_test(session)
    runner._pending_ai_outputs = []

    await runner._merchant_speak("AI text", "en")

    assert runner._pending_ai_outputs == []


@pytest.mark.asyncio
async def test_merchant_speak_synthesizes_when_takeover_inactive() -> None:
    state = TaskState(
        session_id="s",
        user_task_description="t",
        phase=TaskPhase.EXECUTION_ACTIVE,
        user_takeover_active=False,
    )
    session = Session(session_id="s", task_description="t", task_state=state)
    spoken: list[tuple[str, str]] = []

    class _MerchantTTS:
        def stream_synthesize(self, chunks: Any) -> Any:
            async def _gen() -> Any:
                async for chunk in chunks:
                    spoken.append((chunk.text, chunk.language))
                    yield b""

            return _gen()

    class _MerchantTransport:
        async def output_stream(self, audio: Any) -> None:
            async for _ in audio:
                pass

    runner = _build_runner_for_test(session)
    runner._merchant_tts = _MerchantTTS()
    runner._merchant_transport = _MerchantTransport()

    await runner._merchant_speak("AI text", "en")

    assert spoken == [("AI text", "en")]
    assert runner._pending_ai_outputs == []


@pytest.mark.asyncio
async def test_handover_rejected_after_readiness_regression() -> None:
    state = TaskState(
        session_id="s",
        user_task_description="t",
        phase=TaskPhase.COLLECTING,
    )
    session = Session(session_id="s", task_description="t", task_state=state)
    sent: list[dict[str, Any]] = []
    channel = await _make_channel(sent)
    runner = _build_runner_for_test(session)
    runner._readiness_passed = asyncio.Event()
    runner._handover_ready = asyncio.Event()

    class _Transport:
        def drain_inbound(self) -> int:
            raise AssertionError("must not drain before readiness re-passes")

        def set_drop_inbound(self, drop: bool) -> None:
            raise AssertionError("must not open inbound before readiness re-passes")

    runner._web_transport = _Transport()
    raw = json.dumps({"type": "mode_change", "mode": "call_listening"})

    await runner._dispatch_text_frame(raw, channel=channel)

    assert any(
        frame.get("type") == "error" and frame.get("code") == 1002
        for frame in sent
    )
    assert not any(
        frame.get("type") == "mode_ack"
        and frame.get("mode") == "call_listening"
        for frame in sent
    )
    assert not runner._handover_ready.is_set()


@pytest.mark.asyncio
async def test_handover_rejected_when_readiness_event_is_stale() -> None:
    state = TaskState(
        session_id="s",
        user_task_description="t",
        phase=TaskPhase.COLLECTING,
        readiness=ReadinessVerdict(missing_critical=["date"], confidence=0.4),
    )
    session = Session(session_id="s", task_description="t", task_state=state)
    sent: list[dict[str, Any]] = []
    channel = await _make_channel(sent)
    runner = _build_runner_for_test(session)
    runner._readiness_passed = asyncio.Event()
    runner._readiness_passed.set()
    runner._handover_ready = asyncio.Event()
    calls: list[str] = []

    class _Transport:
        def drain_inbound(self) -> int:
            calls.append("drain")
            return 0

        def set_drop_inbound(self, drop: bool) -> None:
            calls.append(f"drop:{drop}")

    runner._web_transport = _Transport()
    raw = json.dumps({"type": "mode_change", "mode": "call_listening"})

    await runner._dispatch_text_frame(raw, channel=channel)

    assert any(
        frame.get("type") == "error" and frame.get("code") == 1002
        for frame in sent
    )
    assert not runner._handover_ready.is_set()
    assert calls == []
