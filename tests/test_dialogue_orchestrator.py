"""dialogue.orchestrator tests — cross-context isolation + scenario tests.

v1 Core Engine refactor (2026-05-04): all tests use ``TaskState`` / ``TaskPhase``
instead of ``BookingState`` / ``BookingPhase``. The orchestrator's ``run()`` now
takes a task description string and calls ``generate_task_schema()`` (Layer 1)
before preflight, so every LLM script list must include a task-planner script
as the first entry.

Strategy:
- We use the dial-now override path (D-11) on the user side so preflight
  short-circuits without needing an LLM tool-dispatch loop on the user
  pipeline. This lets us focus assertions on the merchant-side orchestrator
  logic (D-13 strict tool round-trip + D-14 isolation + D-15 relay).
- merchant-side LLM is scripted via ``tests.conftest.make_scripted_llm``.
- A ``_task_planner_script()`` helper provides the emit_task_schema tool
  call that satisfies Layer 1.
"""
from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import date
from typing import Any, Literal, cast

import pytest

from tests.conftest import make_scripted_llm
from vocalize.llm.base import (
    FinishChunk,
    TextDelta,
    ToolCall,
    ToolCallDelta,
)
from vocalize.stt.base import Transcript
from vocalize.transports.base import AudioEncoding
from vocalize.tts.base import TextChunk

pytestmark = pytest.mark.asyncio

# Module-level production imports — wrap so collection succeeds in Wave 0.
try:
    from vocalize.dialogue.orchestrator import Channel, DialogueOrchestrator
    from vocalize.dialogue.state import (
        DialogueOrchestratorError,
        ReadinessVerdict,
        SlotDef,
        TaskPhase,
        TaskState,
    )
    from vocalize.pipeline import VoicePipeline

    _ORCHESTRATOR_AVAILABLE = True
except ImportError:
    _ORCHESTRATOR_AVAILABLE = False


# ---------------------------------------------------------------------------
# Local fakes — same shape as tests/test_pipeline.py FakeTransport/STT/TTS
# but parameterized for two-channel orchestrator scenarios.
# ---------------------------------------------------------------------------


class _OrchTransport:
    """AudioTransport-shape fake: scripted bytes input + recording output.

    Records the *channel* the chunk was played on (set by the test wiring
    to either ``"user"`` or ``"merchant"``) plus the bytes themselves so
    cross-channel TTS routing assertions (B-3 bidirectional relay) can be
    verified without inspecting per-pipeline state.
    """

    sample_rate: int = 16000
    channels: int = 1
    encoding: AudioEncoding = "pcm_s16le"

    def __init__(self, name: Literal["user", "merchant"]) -> None:
        self.name = name
        self.recorded_output: list[bytes] = []
        self.outbound_log: list[str] = []
        self.closed = False
        self._input_done = asyncio.Event()

    async def input_stream(self) -> AsyncIterator[bytes]:
        # Emit one frame so STT iterator has audio to consume; then park.
        yield b"\x00" * 320
        try:
            await self._input_done.wait()
        except asyncio.CancelledError:
            raise

    async def output_stream(self, audio: AsyncIterator[bytes]) -> None:
        async for chunk in audio:
            self.recorded_output.append(chunk)

    async def pause_outbound(self) -> None:
        self.outbound_log.append("pause_outbound")

    async def resume_outbound(self) -> None:
        self.outbound_log.append("resume_outbound")

    async def close(self) -> None:
        self.closed = True
        self._input_done.set()


class _OrchSTT:
    """STTService fake — scripted Transcript list."""

    def __init__(self, transcripts: list[Transcript]) -> None:
        self._transcripts = list(transcripts)
        self.last_kwargs: dict[str, Any] = {}
        self.call_count: int = 0

    async def stream_transcribe(
        self,
        audio_chunks: AsyncIterator[bytes],
        **kwargs: Any,
    ) -> AsyncIterator[Transcript]:
        self.last_kwargs = dict(kwargs)
        self.call_count += 1
        # Drain one frame so the transport's input_stream parks.
        async for _ in audio_chunks:
            break
        for t in self._transcripts:
            yield t
            await asyncio.sleep(0)


class _NoKwargSTT:
    """Legacy-shape STT fake: ``stream_transcribe`` raises TypeError on any
    keyword argument, mirroring an STT impl that predates the Plan 04-04
    ``transport=`` kwarg.
    """

    def __init__(self, transcripts: list[Transcript]) -> None:
        self._transcripts = list(transcripts)
        self.kwarg_call_count: int = 0
        self.fallback_call_count: int = 0

    def stream_transcribe(
        self,
        audio_chunks: AsyncIterator[bytes],
        **kwargs: Any,
    ) -> AsyncIterator[Transcript]:
        if kwargs:
            self.kwarg_call_count += 1
            raise TypeError(
                f"stream_transcribe() got unexpected keyword arguments: "
                f"{list(kwargs)}"
            )
        self.fallback_call_count += 1
        return self._impl(audio_chunks)

    async def _impl(
        self, audio_chunks: AsyncIterator[bytes]
    ) -> AsyncIterator[Transcript]:
        async for _ in audio_chunks:
            break
        for t in self._transcripts:
            yield t
            await asyncio.sleep(0)


class _OrchTTS:
    """TTSService fake — records every TextChunk + emits a fixed audio blob."""

    output_sample_rate: int = 24000
    output_encoding: AudioEncoding = "pcm_s16le"

    def __init__(
        self,
        recorder: list[tuple[str, TextChunk]] | None = None,
        channel_label: str = "?",
        audio_blob: bytes = b"AUDIO",
    ) -> None:
        self.received_chunks: list[TextChunk] = []
        self._recorder = recorder
        self.channel_label = channel_label
        self._audio_blob = audio_blob

    async def stream_synthesize(
        self, text_chunks: AsyncIterator[TextChunk]
    ) -> AsyncIterator[bytes]:
        async for c in text_chunks:
            self.received_chunks.append(c)
            if self._recorder is not None:
                self._recorder.append((self.channel_label, c))
        yield self._audio_blob


# ---------------------------------------------------------------------------
# Chunk helpers
# ---------------------------------------------------------------------------


def _final_transcript(text: str, lang: str = "zh") -> Transcript:
    return Transcript(
        text=text,
        is_final=True,
        confidence=0.95,
        start_time=0.0,
        end_time=1.0,
        utterance_id=1,
        language=lang,
    )


def _td(t: str) -> TextDelta:
    return TextDelta(text=t)


def _tcd(idx: int, tc_id: str | None, name: str | None, args_delta: str) -> ToolCallDelta:
    return ToolCallDelta(
        tool_call_index=idx,
        tool_call_id=tc_id,
        name=name,
        arguments_delta=args_delta,
    )


def _tool_call_chunks(
    idx: int, tc_id: str, name: str, arguments: dict[str, Any]
) -> list[Any]:
    """One-shot ToolCallDelta + FinishChunk(reason='tool_calls')."""
    return [
        _tcd(idx, tc_id, name, json.dumps(arguments)),
        FinishChunk(reason="tool_calls"),
    ]


def _text_chunks(text: str, *, reason: str = "stop") -> list[Any]:
    return [_td(text), FinishChunk(reason=reason)]  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Task-planner script helpers
# ---------------------------------------------------------------------------


def _task_planner_script() -> list[Any]:
    """Return chunk list for a successful task_planner emit_task_schema call."""
    return _tool_call_chunks(0, "call_tp", "emit_task_schema", {
        "task_category": "restaurant-booking",
        "slots_schema": [
            {"name": "merchant_lang", "description_zh": "商家语言", "description_en": "Merchant language", "criticality": "H", "expected_type": "enum", "enum_values": ["zh", "en"]},
            {"name": "restaurant_name", "description_zh": "餐厅名称", "description_en": "Restaurant name", "criticality": "H", "expected_type": "string"},
            {"name": "date", "description_zh": "日期", "description_en": "Date", "criticality": "H", "expected_type": "date"},
            {"name": "time", "description_zh": "时间", "description_en": "Time", "criticality": "H", "expected_type": "string"},
            {"name": "headcount", "description_zh": "人数", "description_en": "Headcount", "criticality": "H", "expected_type": "number"},
        ],
        "optional_slots_schema": [],
        "conversation_goals": ["Book the restaurant"],
        "merchant_etiquette_notes": "Be polite",
        "readiness_criteria_text": "All H slots filled",
        "relay_strategy": "verbatim translation",
        "reasoning": "Standard restaurant booking",
    })


def _refused_task_planner_script() -> list[Any]:
    """Return chunk list for a refused task_planner call."""
    return _tool_call_chunks(0, "call_tp_refuse", "emit_task_schema", {
        "task_category": "refused",
        "slots_schema": [],
        "conversation_goals": [],
        "readiness_criteria_text": "",
        "relay_strategy": "",
        "reasoning": "This task is not supported",
    })


# ---------------------------------------------------------------------------
# Scenario builder — assembles a full orchestrator graph with fakes.
#
# preflight short-circuits via dial-now phrase on the user_channel. The
# first LLM script is consumed by task_planner (Layer 1); the remaining
# scripts drive the merchant channel.
# ---------------------------------------------------------------------------


def _build_orchestrator(
    *,
    state: TaskState,
    user_dial_now_phrase: str,
    user_lang: Literal["zh", "en"],
    merchant_lang: Literal["zh", "en"],
    merchant_transcripts: list[Transcript],
    llm_scripts: list[list[Any]],
    tts_recorder: list[tuple[str, TextChunk]],
    task_planner_script: list[Any] | None = None,
    skip_task_planner_script: bool = False,
    wait_for_handover: Callable[[], Awaitable[None]] | None = None,
    consume_user_hints: Callable[[], list[tuple[str, str]]] | None = None,
    merchant_speak: Callable[[str, str], Awaitable[None]] | None = None,
) -> tuple[
    DialogueOrchestrator,
    _OrchTransport,
    _OrchTransport,
    Any,
    _OrchTTS,
    _OrchTTS,
]:
    """Wire up two pipelines + fakes + an orchestrator.

    Unless ``skip_task_planner_script`` is True, the task_planner script is
    prepended to ``llm_scripts`` so it is consumed by
    ``generate_task_schema()`` during ``run()``.

    Returns the orchestrator and recorders for assertions:
    - user transport / merchant transport (recorded_output + outbound_log)
    - shared LLM (scripted; .calls captures messages per stream_chat)
    - user TTS / merchant TTS
    """
    state.user_lang = user_lang
    state.merchant_lang = merchant_lang

    user_transport = _OrchTransport("user")
    merchant_transport = _OrchTransport("merchant")

    # User STT: unused in text-user topology (preflight reads from user_channel).
    user_stt = _OrchSTT([])
    merchant_stt = _OrchSTT(merchant_transcripts)

    # Optionally prepend task_planner + initial preflight scripts. Most tests
    # below focus on merchant execution after a queued "dial now" phrase, so
    # the seeded preflight turn gets a no-op text response.
    if skip_task_planner_script:
        llm = make_scripted_llm(*llm_scripts)
    else:
        tp_script = task_planner_script if task_planner_script is not None else _task_planner_script()
        llm = make_scripted_llm(*([tp_script, _text_chunks("")] + list(llm_scripts)))

    user_tts = _OrchTTS(recorder=tts_recorder, channel_label="user_tts")
    merchant_tts = _OrchTTS(recorder=tts_recorder, channel_label="merchant_tts")

    user_pipeline = VoicePipeline(
        transport=user_transport,
        stt=user_stt,
        llm=llm,
        tts=user_tts,
        system_prompt="user-system-prompt",
        default_language=user_lang,
    )
    merchant_pipeline = VoicePipeline(
        transport=merchant_transport,
        stt=merchant_stt,
        llm=llm,
        tts=merchant_tts,
        system_prompt="merchant-system-prompt",
        default_language=merchant_lang,
    )

    class _FUC:
        def __init__(self) -> None:
            self.queued_replies: list[str] = []
            self.requests: list[tuple[str, str, float, str | None]] = []
            # Pre-queue the dial-now phrase so preflight short-circuits.
            self.queued_inputs: list[tuple[str, str]] = [
                (user_dial_now_phrase, user_lang),
            ]
            self.spoken: list[tuple[str, str]] = []

        async def request_clarification(
            self,
            prompt: str,
            lang: str,
            timeout_s: float,
            field: str | None = None,
        ) -> Any:
            from vocalize.dialogue.user_channel import ClarificationReply

            self.requests.append((prompt, lang, timeout_s, field))
            if not self.queued_replies:
                raise asyncio.TimeoutError("no queued reply")
            answer = self.queued_replies.pop(0)
            return ClarificationReply(
                answer=answer, user_lang=lang, received_at=0.0  # type: ignore[arg-type]
            )

        async def push_event(self, event: dict[str, Any]) -> None:
            pass

        async def receive_text(self) -> tuple[str, str]:
            if not self.queued_inputs:
                raise EOFError("queued_inputs exhausted")
            return self.queued_inputs.pop(0)

        async def speak_text(self, text: str, *, lang: str) -> None:
            self.spoken.append((text, lang))

    user_channel = _FUC()

    orch = DialogueOrchestrator(
        state=state,
        user_pipeline=user_pipeline,
        merchant_pipeline=merchant_pipeline,
        user_channel=user_channel,
        wait_for_handover=wait_for_handover,
        consume_user_hints=consume_user_hints,
        merchant_speak=merchant_speak,
    )
    # Stash for test access.
    orch._test_user_channel = user_channel  # type: ignore[attr-defined]
    return orch, user_transport, merchant_transport, llm, user_tts, merchant_tts


def _build_minimal_orchestrator_for_transcript_test(
    *,
    state: TaskState,
    pushed: list[dict[str, Any]],
    emitted_events: list[dict[str, Any]] | None = None,
    cache_calls: list[dict[str, str]] | None = None,
) -> DialogueOrchestrator:
    class _UserChannel:
        async def push_event(self, event: dict[str, Any]) -> None:
            pushed.append(event)

    class _Merchant:
        lang = state.merchant_lang or "zh"

    orch: Any = DialogueOrchestrator.__new__(DialogueOrchestrator)
    orch._state = state
    orch._user_channel = _UserChannel()
    orch._merchant = _Merchant()
    orch._llm = object()
    orch._relay_tasks = set()

    async def _emit(event: dict[str, Any]) -> None:
        if emitted_events is not None:
            emitted_events.append(event)

    orch._emit = _emit
    if cache_calls is None:
        orch._cache_merchant_transcript = None
    else:
        def _cache_merchant_transcript(**kwargs: str) -> None:
            cache_calls.append(kwargs)

        orch._cache_merchant_transcript = _cache_merchant_transcript
    return cast(DialogueOrchestrator, orch)


async def _drain_relay_tasks(orch: DialogueOrchestrator) -> None:
    await orch._finish_relay_tasks(timeout=1.0)


async def test_compute_best_guess_default_uses_schema_types() -> None:
    state = TaskState(
        session_id="best-guess",
        slots_schema=[
            SlotDef(
                name="party_size",
                description_zh="人数",
                description_en="Party size",
                criticality="H",
                expected_type="number",
            ),
            SlotDef(
                name="booking_date",
                description_zh="日期",
                description_en="Date",
                criticality="H",
                expected_type="date",
            ),
            SlotDef(
                name="area",
                description_zh="区域",
                description_en="Area",
                criticality="H",
                expected_type="enum",
                enum_values=("indoor", "patio"),
            ),
        ],
        optional_slots_schema=[
            SlotDef(
                name="notes",
                description_zh="备注",
                description_en="Notes",
                criticality="L",
                expected_type="string",
            ),
            SlotDef(
                name="callback_phone",
                description_zh="电话",
                description_en="Phone",
                criticality="M",
                expected_type="phone",
            ),
        ],
    )
    orch, *_ = _build_orchestrator(
        state=state,
        user_dial_now_phrase="dial now",
        user_lang="en",
        merchant_lang="en",
        merchant_transcripts=[],
        llm_scripts=[],
        tts_recorder=[],
        skip_task_planner_script=True,
    )

    assert orch._compute_best_guess_default("party_size") == 0
    assert orch._compute_best_guess_default("booking_date") == date.today().isoformat()
    assert orch._compute_best_guess_default("area") == "indoor"
    assert orch._compute_best_guess_default("notes") == ""
    assert orch._compute_best_guess_default("callback_phone") == ""
    assert orch._compute_best_guess_default("missing") is None


@pytest.mark.asyncio
async def test_cross_lingual_merchant_utterance_emits_translation_pair(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from vocalize.dialogue.relay import RelayResult

    relay_calls: list[tuple[str, str, str]] = []

    async def fake_relay(text: str, *, src: str, dst: str, llm: Any) -> RelayResult:
        relay_calls.append((text, src, dst))
        return RelayResult(translated="你好", skipped=False)

    monkeypatch.setattr(
        "vocalize.dialogue.orchestrator.merchant_text_to_user_lang",
        fake_relay,
        raising=False,
    )
    state = TaskState(
        session_id="relay-pair",
        user_task_description="t",
        phase=TaskPhase.EXECUTION_ACTIVE,
        user_lang="zh",
        merchant_lang="en",
        auto_translate_merchant=True,
    )
    pushed: list[dict[str, Any]] = []
    cache_calls: list[dict[str, str]] = []
    orch = _build_minimal_orchestrator_for_transcript_test(
        state=state,
        pushed=pushed,
        cache_calls=cache_calls,
    )

    await orch._emit_merchant_transcript("Hello")
    await _drain_relay_tasks(orch)

    originals = [
        event for event in pushed
        if event["event"] == "transcript_update"
        and event["subtype"] == "original"
        and event["role"] == "merchant_to_ai"
    ]
    translations = [
        event for event in pushed
        if event["event"] == "transcript_update"
        and event["subtype"] == "translation"
    ]
    assert len(originals) == 1
    assert len(translations) == 1
    assert translations[0]["parent_id"] == originals[0]["id"]
    assert translations[0]["role"] == "ai_to_user"
    assert translations[0]["text"] == "你好"
    assert translations[0]["lang"] == "zh"
    assert relay_calls == [("Hello", "en", "zh")]
    assert cache_calls == [{
        "id": originals[0]["id"],
        "text": "Hello",
        "lang": "en",
    }]


@pytest.mark.asyncio
async def test_cross_lingual_translation_does_not_block_merchant_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from vocalize.dialogue.relay import RelayResult

    relay_can_finish = asyncio.Event()
    relay_started = asyncio.Event()

    async def fake_relay(text: str, *, src: str, dst: str, llm: Any) -> RelayResult:
        relay_started.set()
        await relay_can_finish.wait()
        return RelayResult(translated="你好")

    monkeypatch.setattr(
        "vocalize.dialogue.orchestrator.merchant_text_to_user_lang",
        fake_relay,
        raising=False,
    )
    state = TaskState(
        session_id="relay-nonblocking",
        user_task_description="t",
        phase=TaskPhase.EXECUTION_ACTIVE,
        user_lang="zh",
        merchant_lang="en",
        auto_translate_merchant=True,
    )
    pushed: list[dict[str, Any]] = []
    orch = _build_minimal_orchestrator_for_transcript_test(
        state=state,
        pushed=pushed,
    )

    await orch._emit_merchant_transcript("Hello")
    await asyncio.wait_for(relay_started.wait(), timeout=1.0)

    assert [
        event for event in pushed
        if event["event"] == "transcript_update"
        and event["subtype"] == "translation"
    ] == []

    relay_can_finish.set()
    await _drain_relay_tasks(orch)

    assert [
        event for event in pushed
        if event["event"] == "transcript_update"
        and event["subtype"] == "translation"
    ]


@pytest.mark.asyncio
async def test_finish_relay_tasks_cancels_slow_translation_on_shutdown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from vocalize.dialogue.relay import RelayResult

    relay_started = asyncio.Event()

    async def fake_relay(text: str, *, src: str, dst: str, llm: Any) -> RelayResult:
        relay_started.set()
        await asyncio.Event().wait()
        return RelayResult(translated="你好")

    monkeypatch.setattr(
        "vocalize.dialogue.orchestrator.merchant_text_to_user_lang",
        fake_relay,
        raising=False,
    )
    state = TaskState(
        session_id="relay-shutdown",
        user_task_description="t",
        phase=TaskPhase.EXECUTION_ACTIVE,
        user_lang="zh",
        merchant_lang="en",
        auto_translate_merchant=True,
    )
    pushed: list[dict[str, Any]] = []
    orch = _build_minimal_orchestrator_for_transcript_test(
        state=state,
        pushed=pushed,
    )

    await orch._emit_merchant_transcript("Hello")
    await asyncio.wait_for(relay_started.wait(), timeout=1.0)
    await orch._finish_relay_tasks(timeout=0.01)

    assert not orch._relay_tasks
    assert [
        event for event in pushed
        if event["event"] == "transcript_update"
        and event["subtype"] == "translation"
    ] == []


@pytest.mark.asyncio
async def test_same_lang_merchant_utterance_no_translation_frame(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from vocalize.dialogue.relay import RelayResult

    relay_calls = 0

    async def fake_relay(text: str, *, src: str, dst: str, llm: Any) -> RelayResult:
        nonlocal relay_calls
        relay_calls += 1
        return RelayResult(translated=text, skipped=True)

    monkeypatch.setattr(
        "vocalize.dialogue.orchestrator.merchant_text_to_user_lang",
        fake_relay,
        raising=False,
    )
    state = TaskState(
        session_id="relay-same-lang",
        user_task_description="t",
        phase=TaskPhase.EXECUTION_ACTIVE,
        user_lang="zh",
        merchant_lang="zh",
        auto_translate_merchant=True,
    )
    pushed: list[dict[str, Any]] = []
    orch = _build_minimal_orchestrator_for_transcript_test(
        state=state,
        pushed=pushed,
    )

    await orch._emit_merchant_transcript("你好")

    transcripts = [
        event for event in pushed if event["event"] == "transcript_update"
    ]
    assert len(transcripts) == 1
    assert transcripts[0]["subtype"] == "original"
    assert relay_calls == 0
    assert not orch._relay_tasks


@pytest.mark.asyncio
async def test_auto_translate_off_skips_relay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from vocalize.dialogue.relay import RelayResult

    relay_calls = 0

    async def fake_relay(text: str, *, src: str, dst: str, llm: Any) -> RelayResult:
        nonlocal relay_calls
        relay_calls += 1
        return RelayResult(translated="x")

    monkeypatch.setattr(
        "vocalize.dialogue.orchestrator.merchant_text_to_user_lang",
        fake_relay,
        raising=False,
    )
    state = TaskState(
        session_id="relay-off",
        user_task_description="t",
        phase=TaskPhase.EXECUTION_ACTIVE,
        user_lang="zh",
        merchant_lang="en",
        auto_translate_merchant=False,
    )
    pushed: list[dict[str, Any]] = []
    orch = _build_minimal_orchestrator_for_transcript_test(
        state=state,
        pushed=pushed,
    )

    await orch._emit_merchant_transcript("Hello")

    transcripts = [
        event for event in pushed if event["event"] == "transcript_update"
    ]
    assert len(transcripts) == 1
    assert transcripts[0]["subtype"] == "original"
    assert relay_calls == 0
    assert not orch._relay_tasks


@pytest.mark.asyncio
async def test_relay_failure_does_not_break_conversation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from vocalize.dialogue.relay import RelayResult
    relay_can_finish = asyncio.Event()
    relay_started = asyncio.Event()

    async def fake_relay(text: str, *, src: str, dst: str, llm: Any) -> RelayResult:
        relay_started.set()
        await relay_can_finish.wait()
        return RelayResult(translated=None, failed=True)

    monkeypatch.setattr(
        "vocalize.dialogue.orchestrator.merchant_text_to_user_lang",
        fake_relay,
        raising=False,
    )
    state = TaskState(
        session_id="relay-failed",
        user_task_description="t",
        phase=TaskPhase.EXECUTION_ACTIVE,
        user_lang="zh",
        merchant_lang="en",
        auto_translate_merchant=True,
    )
    pushed: list[dict[str, Any]] = []
    emitted_events: list[dict[str, Any]] = []
    orch = _build_minimal_orchestrator_for_transcript_test(
        state=state,
        pushed=pushed,
        emitted_events=emitted_events,
    )

    await orch._emit_merchant_transcript("Hello")
    await asyncio.wait_for(relay_started.wait(), timeout=1.0)
    relay_can_finish.set()
    await orch._finish_relay_tasks(timeout=1.0)

    transcripts = [
        event for event in pushed if event["event"] == "transcript_update"
    ]
    assert len(transcripts) == 1
    assert transcripts[0]["subtype"] == "original"
    assert not orch._relay_tasks
    assert any(
        event.get("event") == "state_update"
        and event.get("diff", {}).get("relay_failed") is True
        and event.get("diff", {}).get("original_id") == transcripts[0]["id"]
        for event in emitted_events
    )


async def test_merchant_turn_absorbs_pending_user_hints(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pending_hints: list[tuple[str, str]] = [
        ("they have a private room", "en"),
        ("we want one", "en"),
    ]

    def consume_hints() -> list[tuple[str, str]]:
        out = list(pending_hints)
        pending_hints.clear()
        return out

    seen_user_text: list[str | None] = []

    async def fake_drive_turn(
        self: DialogueOrchestrator,
        channel: Channel,
        user_text: str | None = None,
    ) -> None:
        seen_user_text.append(user_text)
        self._state.transition(TaskPhase.COMPLETED, reason="test complete")

    monkeypatch.setattr(DialogueOrchestrator, "_drive_turn", fake_drive_turn)
    state = TaskState(session_id="hint-absorption")
    state.user_lang = "en"
    state.merchant_lang = "en"
    state.auto_translate_merchant = False
    orch, *_ = _build_orchestrator(
        state=state,
        user_dial_now_phrase="dial now",
        user_lang="en",
        merchant_lang="en",
        merchant_transcripts=[_final_transcript("Hello from merchant", lang="en")],
        llm_scripts=[],
        tts_recorder=[],
        consume_user_hints=consume_hints,
    )

    await asyncio.wait_for(orch.run("book a table"), timeout=10.0)

    assert len(seen_user_text) == 1
    full = seen_user_text[0]
    assert full is not None
    assert full.startswith("[USER HINT")
    assert "private room" in full
    assert "we want one" in full
    assert full.endswith("Hello from merchant")
    assert pending_hints == []


async def test_merchant_turn_queues_ai_output_during_user_takeover() -> None:
    state = TaskState(session_id="takeover-queue")
    state.user_lang = "en"
    state.merchant_lang = "en"
    state.phase = TaskPhase.EXECUTION_ACTIVE
    state.user_takeover_active = True
    pending_outputs: list[tuple[str, str]] = []

    async def merchant_speak(text: str, lang: str) -> None:
        if state.user_takeover_active:
            pending_outputs.append((text, lang))
            return
        raise AssertionError("takeover-active output should not synthesize")

    orch, _user_t, merchant_t, _llm, _user_tts, merchant_tts = _build_orchestrator(
        state=state,
        user_dial_now_phrase="dial now",
        user_lang="en",
        merchant_lang="en",
        merchant_transcripts=[],
        llm_scripts=[_text_chunks("I can help with that.")],
        tts_recorder=[],
        skip_task_planner_script=True,
        merchant_speak=merchant_speak,
    )

    await orch._drive_turn(orch._merchant, user_text="hello")

    assert pending_outputs == [("I can help with that.", "en")]
    assert merchant_t.recorded_output == []
    assert merchant_tts.received_chunks == []


async def test_merchant_turn_emits_ai_to_merchant_transcript() -> None:
    state = TaskState(session_id="merchant-ai-transcript")
    state.user_lang = "zh"
    state.merchant_lang = "zh"
    state.phase = TaskPhase.EXECUTION_ACTIVE
    pushed: list[dict[str, Any]] = []

    orch, _user_t, _merchant_t, _llm, _user_tts, merchant_tts = _build_orchestrator(
        state=state,
        user_dial_now_phrase="现在打吧",
        user_lang="zh",
        merchant_lang="zh",
        merchant_transcripts=[],
        llm_scripts=[_text_chunks("您好，我想预订今晚七点四位。")],
        tts_recorder=[],
        skip_task_planner_script=True,
    )

    async def push_event(event: dict[str, Any]) -> None:
        pushed.append(event)

    orch._user_channel.push_event = push_event  # type: ignore[method-assign]

    await orch._drive_turn(orch._merchant, user_text="你好")

    ai_to_merchant = [
        event for event in pushed
        if event.get("event") == "transcript_update"
        and event.get("role") == "ai_to_merchant"
    ]
    assert ai_to_merchant
    assert ai_to_merchant[0]["text"] == "您好，我想预订今晚七点四位。"
    assert [chunk.text for chunk in merchant_tts.received_chunks] == [
        "您好，我想预订今晚七点四位。"
    ]


async def test_merchant_collect_user_intent_cannot_overwrite_user_slot() -> None:
    state = TaskState(
        session_id="merchant-slot-change",
        phase=TaskPhase.EXECUTION_ACTIVE,
        slots_schema=[
            SlotDef(
                name="time",
                description_zh="时间",
                description_en="Time",
                criticality="H",
                expected_type="string",
            ),
        ],
        slots={"time": "19:00"},
    )
    orch, *_ = _build_orchestrator(
        state=state,
        user_dial_now_phrase="现在打吧",
        user_lang="zh",
        merchant_lang="zh",
        merchant_transcripts=[],
        llm_scripts=[],
        tts_recorder=[],
        skip_task_planner_script=True,
    )

    result = await orch._dispatch_one_tool(
        orch._merchant,
        ToolCall(
            id="tc-change-time",
            name="collect_user_intent",
            arguments=json.dumps({"slot": "time", "value": "21:00"}),
        ),
    )

    assert result["ok"] is False
    assert "request_user_clarification" in result["error"]
    assert state.slots["time"] == "19:00"


async def test_user_to_merchant_relay_uses_merchant_speak_hook() -> None:
    state = TaskState(session_id="test-relay-hook")
    state.user_lang = "zh"
    state.merchant_lang = "en"
    tts_recorder: list[tuple[str, TextChunk]] = []
    merchant_spoken: list[tuple[str, str]] = []

    async def merchant_speak(text: str, lang: str) -> None:
        merchant_spoken.append((text, lang))

    llm_scripts = [
        _tool_call_chunks(
            0, "call_relay_hook", "relay_to_user",
            {"text": "请安排靠窗位置。", "target_lang": "en"},
        ),
        [_td("Please arrange a window seat."), FinishChunk(reason="stop")],
        [FinishChunk(reason="stop")],
    ]

    orch, *_ = _build_orchestrator(
        state=state,
        user_dial_now_phrase="ignored",
        user_lang="zh",
        merchant_lang="en",
        merchant_transcripts=[],
        llm_scripts=llm_scripts,
        tts_recorder=tts_recorder,
        skip_task_planner_script=True,
        merchant_speak=merchant_speak,
    )

    await orch._drive_turn(orch._user, user_text=None)

    assert merchant_spoken == [("Please arrange a window seat.", "en")]
    assert [label for label, _ in tts_recorder if label == "merchant_tts"] == []


# ---------------------------------------------------------------------------
# Cross-context isolation — D-14 invariant
# ---------------------------------------------------------------------------
async def test_orchestrator_dual_pipeline_isolation() -> None:
    """orch._user.messages is not orch._merchant.messages — separate list
    INSTANCES per D-14 ('TaskState is the only cross-channel data path').
    """
    state = TaskState(session_id="test-isolation")
    state.user_lang = "zh"
    state.merchant_lang = "zh"

    user_transport = _OrchTransport("user")
    merchant_transport = _OrchTransport("merchant")
    user_stt = _OrchSTT([])
    merchant_stt = _OrchSTT([])
    llm = make_scripted_llm()  # unused
    user_tts = _OrchTTS()
    merchant_tts = _OrchTTS()

    user_pipeline = VoicePipeline(
        user_transport, user_stt, llm, user_tts, "user-prompt"
    )
    merchant_pipeline = VoicePipeline(
        merchant_transport, merchant_stt, llm, merchant_tts, "merchant-prompt"
    )

    class _FUC:
        async def request_clarification(self, *a: Any, **k: Any) -> Any: ...
        async def push_event(self, *a: Any, **k: Any) -> None: ...
        async def receive_text(self) -> tuple[str, str]:
            return ("现在打吧", "zh")
        async def speak_text(self, text: str, *, lang: str) -> None: ...

    orch = DialogueOrchestrator(
        state=state,
        user_pipeline=user_pipeline,
        merchant_pipeline=merchant_pipeline,
        user_channel=_FUC(),
    )

    assert orch._user.messages is not orch._merchant.messages
    assert isinstance(orch._user, Channel)
    assert isinstance(orch._merchant, Channel)
    # System prompts loaded from the renderer for each channel.
    assert orch._user.messages[0].role == "system"
    assert orch._merchant.messages[0].role == "system"
    assert orch._user.messages[0].content != orch._merchant.messages[0].content


async def test_no_cross_context_bleed(scenario_loader) -> None:
    """Run zh-en scenario; assert no merchant transcript verbatim text appears
    in user-side messages and vice versa. Translated forms via relay are
    acceptable (D-15)."""
    scenarios = {s["id"]: s for s in scenario_loader()}
    scenario = scenarios["zh-en-allergy-clarification"]

    state = TaskState(session_id="test-bleed")
    if scenario.get("prefill_state"):
        for k, v in scenario["prefill_state"].items():
            setattr(state, k, v)
    state.auto_translate_merchant = False

    merchant_transcripts = [_final_transcript("Hi, this is Joy Sushi.", lang="en")]
    llm_scripts = [
        _tool_call_chunks(
            0, "call_fb1", "finalize_task",
            {"success": True, "summary": "booked", "outcomes": {}},
        ),
    ]

    orch, user_t, merchant_t, llm, user_tts, merchant_tts = _build_orchestrator(
        state=state,
        user_dial_now_phrase="现在打吧",
        user_lang="zh",
        merchant_lang="en",
        merchant_transcripts=merchant_transcripts,
        llm_scripts=llm_scripts,
        tts_recorder=[],
    )

    await asyncio.wait_for(orch.run("book a restaurant"), timeout=10.0)

    # Serialize each side's messages to JSON; assert none of the *opposite*
    # side's verbatim transcripts are present.
    user_blob = json.dumps([m.__dict__ for m in orch._user.messages], default=str)
    merchant_blob = json.dumps([m.__dict__ for m in orch._merchant.messages], default=str)

    assert "Joy Sushi" not in user_blob, (
        "merchant transcript verbatim leaked into user messages — D-14 broken"
    )
    # User dial-now phrase ("现在打吧") doesn't go through LLM (override path),
    # so it should never appear in either messages list.
    assert "现在打吧" not in merchant_blob


# ---------------------------------------------------------------------------
# End-to-end scenarios — REQ-dialogue-orchestrator criteria 2 + 3
# ---------------------------------------------------------------------------
async def test_zh_zh_happy_path(scenario_loader) -> None:
    """Run zh-zh-happy-path: dial-now override on user side (preflight
    short-circuits), then merchant LLM emits finalize_task on the first
    merchant turn → state.phase == COMPLETED.
    """
    scenarios = {s["id"]: s for s in scenario_loader()}
    scenario = scenarios["zh-zh-happy-path"]

    state = TaskState(session_id="test-zh-zh")
    # Prefill critical slots so they appear in the state after task_planner.
    state.slots["restaurant_name"] = "海底捞"
    state.slots["date"] = "2026-05-04"
    state.slots["time"] = "19:00"
    state.slots["headcount"] = 4

    merchant_transcripts = [_final_transcript("您好，海底捞", lang="zh")]
    llm_scripts = [
        _tool_call_chunks(
            0, "call_fb_zh", "finalize_task",
            {"success": True, "summary": "已订位", "outcomes": {}},
        ),
    ]

    orch, *_ = _build_orchestrator(
        state=state,
        user_dial_now_phrase="现在打吧",
        user_lang="zh",
        merchant_lang="zh",
        merchant_transcripts=merchant_transcripts,
        llm_scripts=llm_scripts,
        tts_recorder=[],
    )

    await asyncio.wait_for(orch.run("book a restaurant"), timeout=10.0)

    assert state.phase == TaskPhase.COMPLETED
    assert scenario["expected_terminal_state"] == "COMPLETED"


async def test_en_en_happy_path(scenario_loader) -> None:
    """Run en-en-happy-path: same shape, English."""
    scenarios = {s["id"]: s for s in scenario_loader()}
    scenario = scenarios["en-en-happy-path"]

    state = TaskState(session_id="test-en-en")
    state.slots["restaurant_name"] = "Joy Sushi"
    state.slots["date"] = "2026-05-04"
    state.slots["time"] = "19:00"
    state.slots["headcount"] = 4

    merchant_transcripts = [_final_transcript("Hello, Joy Sushi.", lang="en")]
    llm_scripts = [
        _tool_call_chunks(
            0, "call_fb_en", "finalize_task",
            {"success": True, "summary": "booked", "outcomes": {}},
        ),
    ]

    orch, *_ = _build_orchestrator(
        state=state,
        user_dial_now_phrase="call now",
        user_lang="en",
        merchant_lang="en",
        merchant_transcripts=merchant_transcripts,
        llm_scripts=llm_scripts,
        tts_recorder=[],
    )

    await asyncio.wait_for(orch.run("book a restaurant"), timeout=10.0)

    assert state.phase == TaskPhase.COMPLETED
    assert scenario["expected_terminal_state"] == "COMPLETED"


# ---------------------------------------------------------------------------
# B-3 bidirectional cross-lingual relay — D-15
# ---------------------------------------------------------------------------
async def test_relay_to_user_bidirectional() -> None:
    """B-3: relay_to_user works from BOTH the merchant channel AND the user
    channel. Direction = (calling_channel, args.target_lang); the translated
    text is spoken via the OPPOSITE channel's pipeline.

    - merchant-channel emit relay_to_user(target_lang='zh') → speaks via
      user_pipeline (target = zh user).
    - user-channel emit relay_to_user(target_lang='en') → speaks via
      merchant_pipeline (target = en merchant).
    """
    state = TaskState(session_id="test-relay-a")
    state.user_lang = "zh"
    state.merchant_lang = "en"
    state.auto_translate_merchant = False
    state.slots["restaurant_name"] = "Joy Sushi"
    state.slots["date"] = "2026-05-10"
    state.slots["time"] = "19:00"
    state.slots["headcount"] = 4

    tts_recorder: list[tuple[str, TextChunk]] = []

    # Direction A: merchant channel emits relay_to_user(target_lang='zh') with
    # an English source string; orchestrator should load relay_en_to_zh.md
    # prompt and speak the translated text on the USER pipeline.
    merchant_transcripts = [_final_transcript("Sure, see you Sunday.", lang="en")]
    llm_scripts = [
        # Turn 1: merchant emits relay_to_user(target_lang='zh').
        _tool_call_chunks(
            0, "call_relay1", "relay_to_user",
            {"text": "See you Sunday.", "target_lang": "zh"},
        ),
        # Relay's one-shot stream_chat: pure TextDelta translation.
        [_td("周日见。"), FinishChunk(reason="stop")],
        # Turn 2 (after relay tool result returned): merchant emits
        # finalize_task to terminate the run.
        _tool_call_chunks(
            1, "call_fb_relay", "finalize_task",
            {"success": True, "summary": "booked", "outcomes": {}},
        ),
    ]

    orch, user_t, merchant_t, llm, user_tts, merchant_tts = _build_orchestrator(
        state=state,
        user_dial_now_phrase="现在打吧",
        user_lang="zh",
        merchant_lang="en",
        merchant_transcripts=merchant_transcripts,
        llm_scripts=llm_scripts,
        tts_recorder=tts_recorder,
    )

    await asyncio.wait_for(orch.run("book a restaurant"), timeout=10.0)

    # Direction A assertion: USER tts received the translated zh text.
    user_chunks = [c for label, c in tts_recorder if label == "user_tts"]
    assert any(
        c.text == "周日见。" and c.language == "zh" for c in user_chunks
    ), f"user_tts did not receive relay translation; got {[c.text for c in user_chunks]}"

    # The relay must NOT mutate either channel.messages — D-14.
    user_blob = json.dumps([m.__dict__ for m in orch._user.messages], default=str)
    merchant_blob = json.dumps(
        [m.__dict__ for m in orch._merchant.messages], default=str
    )
    assert "周日见。" not in user_blob
    assert "周日见。" not in merchant_blob

    # Direction B: invoke _drive_turn directly on the user channel with a
    # fresh LLM script that emits relay_to_user(target_lang='en').
    state2 = TaskState(session_id="test-relay-b")
    state2.user_lang = "zh"
    state2.merchant_lang = "en"

    tts_recorder2: list[tuple[str, TextChunk]] = []
    llm_scripts2 = [
        # _drive_turn(user) call 1: user channel emits relay_to_user(en).
        _tool_call_chunks(
            0, "call_relay_b", "relay_to_user",
            {"text": "请安排靠窗位置。", "target_lang": "en"},
        ),
        # Relay one-shot: zh→en translation.
        [_td("Please arrange a window seat."), FinishChunk(reason="stop")],
        # After tool result returns, LLM yields a stop to exit the loop.
        [FinishChunk(reason="stop")],
    ]

    orch2, user_t2, merchant_t2, llm2, user_tts2, merchant_tts2 = _build_orchestrator(
        state=state2,
        user_dial_now_phrase="ignored",
        user_lang="zh",
        merchant_lang="en",
        merchant_transcripts=[],
        llm_scripts=llm_scripts2,
        tts_recorder=tts_recorder2,
        skip_task_planner_script=True,  # Direction B calls _drive_turn directly, not run()
    )

    # Drive a single user-channel turn directly (skip full run()).
    await orch2._drive_turn(orch2._user, user_text=None)

    merchant_chunks2 = [c for label, c in tts_recorder2 if label == "merchant_tts"]
    assert any(
        c.text == "Please arrange a window seat." and c.language == "en"
        for c in merchant_chunks2
    ), (
        "merchant_tts did not receive relay translation for user→en direction; "
        f"got {[c.text for c in merchant_chunks2]}"
    )


# ---------------------------------------------------------------------------
# Plan 04-10 regression guards: orchestrator wires Plan 04-04 client-side
# VAD EOS by passing ``transport=`` into ``stream_transcribe``.
# ---------------------------------------------------------------------------
async def test_orchestrator_passes_transport_kwarg_to_merchant_stt() -> None:
    """orchestrator.run() must call ``merchant_pipeline._stt.stream_transcribe``
    with ``transport=merchant_pipeline._transport`` so SenseVoiceClient can
    register its client-side webrtcvad EOS handler.
    """
    state = TaskState(session_id="test-transport")
    state.auto_translate_merchant = False
    merchant_transcripts = [_final_transcript("Hi, this is Joy Sushi.", lang="en")]
    llm_scripts = [
        _tool_call_chunks(
            0, "call_fb1", "finalize_task",
            {"success": True, "summary": "booked", "outcomes": {}},
        ),
    ]

    orch, _user_t, merchant_t, _llm, _user_tts, _merchant_tts = _build_orchestrator(
        state=state,
        user_dial_now_phrase="现在打吧",
        user_lang="zh",
        merchant_lang="en",
        merchant_transcripts=merchant_transcripts,
        llm_scripts=llm_scripts,
        tts_recorder=[],
    )

    await asyncio.wait_for(orch.run("book a restaurant"), timeout=10.0)

    merchant_stt = orch._merchant.pipeline._stt
    assert isinstance(merchant_stt, _OrchSTT)
    assert merchant_stt.call_count == 1, (
        f"expected exactly one stream_transcribe call on merchant STT, "
        f"got {merchant_stt.call_count}"
    )
    assert "transport" in merchant_stt.last_kwargs, (
        f"orchestrator did NOT pass transport= kwarg into merchant STT — "
        f"Plan 04-04 client-side EOS hook is unwired and the 11s "
        f"perceived-latency regression has returned. "
        f"Got kwargs: {list(merchant_stt.last_kwargs)}"
    )
    assert merchant_stt.last_kwargs["transport"] is merchant_t, (
        "transport kwarg must be the merchant pipeline's own transport, "
        "not some other instance"
    )


async def test_orchestrator_falls_back_when_stt_rejects_transport_kwarg() -> None:
    """If the STT impl predates the ``transport=`` kwarg and raises TypeError
    on it, orchestrator.run() must retry without the kwarg and continue —
    legacy STTs (and test fakes that don't accept kwargs) keep working.
    """
    state = TaskState(session_id="test-fallback")
    state.user_lang = "zh"
    state.merchant_lang = "en"
    state.auto_translate_merchant = False

    user_transport = _OrchTransport("user")
    merchant_transport = _OrchTransport("merchant")
    user_stt = _OrchSTT([])
    merchant_stt = _NoKwargSTT(
        [_final_transcript("Hi, this is Joy Sushi.", lang="en")]
    )
    # First script: task_planner; second: seeded preflight no-op; third:
    # merchant finalize_task.
    llm = make_scripted_llm(
        _task_planner_script(),
        _text_chunks(""),
        _tool_call_chunks(
            0, "call_fb1", "finalize_task",
            {"success": True, "summary": "booked", "outcomes": {}},
        ),
    )
    user_tts = _OrchTTS()
    merchant_tts = _OrchTTS()

    user_pipeline = VoicePipeline(
        user_transport, user_stt, llm, user_tts, "user-system-prompt",
        default_language="zh",
    )
    merchant_pipeline = VoicePipeline(
        merchant_transport, merchant_stt, llm, merchant_tts, "merchant-system-prompt",
        default_language="en",
    )

    class _FUC:
        async def request_clarification(self, *a: Any, **k: Any) -> Any: ...
        async def push_event(self, *a: Any, **k: Any) -> None: ...
        async def receive_text(self) -> tuple[str, str]:
            return ("现在打吧", "zh")
        async def speak_text(self, text: str, *, lang: str) -> None: ...

    orch = DialogueOrchestrator(
        state=state,
        user_pipeline=user_pipeline,
        merchant_pipeline=merchant_pipeline,
        user_channel=_FUC(),
    )

    await asyncio.wait_for(orch.run("book a restaurant"), timeout=10.0)

    assert merchant_stt.kwarg_call_count == 1, (
        "expected one TypeError-raising kwarg call (the initial attempt)"
    )
    assert merchant_stt.fallback_call_count == 1, (
        "expected one no-kwarg call (the legacy fallback path)"
    )


# ---------------------------------------------------------------------------
# Plan 04-10 next-step #2: orchestrator merchant turn must populate
# ``merchant_pipeline._last_turn_timing``.
# ---------------------------------------------------------------------------
async def test_orchestrator_records_last_turn_timing_on_merchant_turn() -> None:
    """After orch.run() drives a merchant turn, merchant_pipeline must expose
    ``_last_turn_timing`` with at least ``user_text`` + ``final_at`` populated.
    """
    state = TaskState(session_id="test-timing")
    state.auto_translate_merchant = False
    merchant_transcripts = [_final_transcript("Hi, this is Joy Sushi.", lang="en")]
    llm_scripts = [
        _tool_call_chunks(
            0, "call_fb1", "finalize_task",
            {"success": True, "summary": "booked", "outcomes": {}},
        ),
    ]

    orch, _user_t, _merchant_t, _llm, _user_tts, _merchant_tts = _build_orchestrator(
        state=state,
        user_dial_now_phrase="现在打吧",
        user_lang="zh",
        merchant_lang="en",
        merchant_transcripts=merchant_transcripts,
        llm_scripts=llm_scripts,
        tts_recorder=[],
    )

    await asyncio.wait_for(orch.run("book a restaurant"), timeout=10.0)

    timing = getattr(orch._merchant.pipeline, "_last_turn_timing", None)
    assert timing is not None, (
        "orchestrator did NOT stash TurnTiming on merchant_pipeline — demo "
        "_TimingCollector will report 'insufficient samples'"
    )
    assert timing.user_text == "Hi, this is Joy Sushi."
    assert timing.final_at > 0, "final_at must be a monotonic timestamp"


# ---------------------------------------------------------------------------
# v1 Core Engine: task_planner integration tests
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_orchestrator_calls_task_planner_first():
    """run() invokes task_planner before COLLECTING — verify schema is
    populated and state has moved past DRAFT and TASK_PLANNING.
    """
    state = TaskState(session_id="test-tp-first")
    state.user_lang = "zh"
    state.merchant_lang = "zh"

    user_transport = _OrchTransport("user")
    merchant_transport = _OrchTransport("merchant")
    user_stt = _OrchSTT([])
    merchant_stt = _OrchSTT([])
    llm = make_scripted_llm(_task_planner_script(), _text_chunks(""))
    user_tts = _OrchTTS()
    merchant_tts = _OrchTTS()

    user_pipeline = VoicePipeline(
        user_transport, user_stt, llm, user_tts, "user-prompt"
    )
    merchant_pipeline = VoicePipeline(
        merchant_transport, merchant_stt, llm, merchant_tts, "merchant-prompt"
    )

    class _FUC:
        async def request_clarification(self, *a: Any, **k: Any) -> Any: ...
        async def push_event(self, *a: Any, **k: Any) -> None: ...
        async def receive_text(self) -> tuple[str, str]:
            return ("现在打吧", "zh")
        async def speak_text(self, text: str, *, lang: str) -> None: ...

    orch = DialogueOrchestrator(
        state=state,
        user_pipeline=user_pipeline,
        merchant_pipeline=merchant_pipeline,
        user_channel=_FUC(),
    )

    # Run orchestrator with dial-now override (preflight short-circuits).
    # This will: DRAFT → TASK_PLANNING (task_planner runs) → COLLECTING →
    # dial-now → READY_TO_DIAL → EXECUTION_ACTIVE.
    await asyncio.wait_for(orch.run("book a restaurant"), timeout=10.0)

    # Verify state moved past DRAFT and task_planner populated the schema.
    assert state.phase != TaskPhase.DRAFT, (
        f"expected phase to advance past DRAFT, got {state.phase}"
    )
    # Phase should be EXECUTION_ACTIVE (dial-now override → READY_TO_DIAL → EXECUTION_ACTIVE)
    # or COMPLETED/FAILED if merchant loop terminated.
    assert state.phase in (TaskPhase.EXECUTION_ACTIVE, TaskPhase.COMPLETED, TaskPhase.FAILED), (
        f"expected terminal-ish phase after dial-now, got {state.phase}"
    )
    # Verify schema was applied by task_planner.
    assert state.task_category == "restaurant-booking", (
        f"expected task_category 'restaurant-booking', got {state.task_category!r}"
    )
    assert len(state.slots_schema) == 5  # 5 H slots from fixture
    # Verify audit log contains the expected phase transitions.
    phase_sequence = [entry.to_phase.value for entry in state.audit_log]
    assert "task_planning" in phase_sequence, (
        f"expected TASK_PLANNING in audit log, got {phase_sequence}"
    )
    assert "collecting" in phase_sequence, (
        f"expected COLLECTING in audit log, got {phase_sequence}"
    )


@pytest.mark.asyncio
async def test_orchestrator_prompts_from_initial_task_text_before_second_user_turn():
    """The live page's first WS text_input is the task description.

    After task planning, that same text must drive the first preflight LLM turn;
    otherwise the UI waits silently until the user sends a duplicate second
    message.
    """
    state = TaskState(session_id="test-initial-task-prompt")
    state.user_lang = "zh"
    state.merchant_lang = "zh"

    user_transport = _OrchTransport("user")
    merchant_transport = _OrchTransport("merchant")
    user_stt = _OrchSTT([])
    merchant_stt = _OrchSTT([])
    prompt = "我先确认一下，您要预订哪一家海底捞？"
    llm = make_scripted_llm(_task_planner_script(), _text_chunks(prompt))
    user_tts = _OrchTTS()
    merchant_tts = _OrchTTS()

    user_pipeline = VoicePipeline(
        user_transport, user_stt, llm, user_tts, "user-prompt"
    )
    merchant_pipeline = VoicePipeline(
        merchant_transport, merchant_stt, llm, merchant_tts, "merchant-prompt"
    )

    class _FUC:
        def __init__(self) -> None:
            self.spoken: list[tuple[str, str]] = []
            self.receive_calls = 0

        async def request_clarification(self, *a: Any, **k: Any) -> Any: ...
        async def push_event(self, *a: Any, **k: Any) -> None: ...

        async def receive_text(self) -> tuple[str, str]:
            self.receive_calls += 1
            raise EOFError("no second user turn")

        async def speak_text(self, text: str, *, lang: str) -> None:
            self.spoken.append((text, lang))

    user_channel = _FUC()
    orch = DialogueOrchestrator(
        state=state,
        user_pipeline=user_pipeline,
        merchant_pipeline=merchant_pipeline,
        user_channel=user_channel,
    )

    await asyncio.wait_for(orch.run("帮我订海底捞"), timeout=10.0)

    assert user_channel.spoken == [(prompt, "zh")]
    assert user_channel.receive_calls == 1


@pytest.mark.asyncio
async def test_orchestrator_emits_phase_change_when_preflight_reaches_ready_to_dial():
    state = TaskState(session_id="test-ready-phase-event")
    state.user_lang = "zh"
    state.merchant_lang = "zh"

    orch, *_ = _build_orchestrator(
        state=state,
        user_dial_now_phrase="现在打吧",
        user_lang="zh",
        merchant_lang="zh",
        merchant_transcripts=[],
        llm_scripts=[],
        tts_recorder=[],
    )

    await asyncio.wait_for(orch.run("你好我要预定海底捞"), timeout=10.0)

    events: list[dict[str, Any]] = []
    while not orch._events_queue.empty():
        events.append(orch._events_queue.get_nowait())

    assert any(
        event.get("event") == "phase_change"
        and event.get("previous") == "collecting"
        and event.get("current") == "ready_to_dial"
        for event in events
    )


@pytest.mark.asyncio
async def test_orchestrator_waits_for_handover_gate_before_execution() -> None:
    state = TaskState(session_id="test-handover-gate")
    state.user_lang = "zh"
    state.merchant_lang = "zh"
    gate = asyncio.Event()

    orch, *_ = _build_orchestrator(
        state=state,
        user_dial_now_phrase="现在打吧",
        user_lang="zh",
        merchant_lang="zh",
        merchant_transcripts=[],
        llm_scripts=[],
        tts_recorder=[],
        wait_for_handover=gate.wait,
    )

    task = asyncio.create_task(orch.run("book a restaurant"))
    await asyncio.sleep(0.1)

    assert state.phase == TaskPhase.READY_TO_DIAL
    assert any(entry.to_phase == TaskPhase.READY_TO_DIAL for entry in state.audit_log)
    assert not any(
        entry.to_phase == TaskPhase.EXECUTION_ACTIVE for entry in state.audit_log
    )

    gate.set()
    await asyncio.wait_for(task, timeout=10.0)
    assert any(
        entry.to_phase == TaskPhase.EXECUTION_ACTIVE for entry in state.audit_log
    )


@pytest.mark.asyncio
async def test_ready_to_dial_supplement_regresses_to_collecting() -> None:
    state = TaskState(
        session_id="test-ready-regression",
        user_task_description="book a restaurant",
        phase=TaskPhase.READY_TO_DIAL,
        user_lang="en",
        merchant_lang="en",
        slots_schema=[
            SlotDef(
                name="date",
                description_zh="日期",
                description_en="Date",
                criticality="H",
                expected_type="date",
            )
        ],
        slots={"date": "2026-05-08"},
        readiness=ReadinessVerdict(
            missing_critical=[],
            confidence=0.9,
            override=False,
        ),
    )

    llm_scripts = [
        _tool_call_chunks(
            0,
            "call_collect_bad_date",
            "collect_user_intent",
            {"slot": "date", "value": "next Friday"},
        ),
        _tool_call_chunks(
            0,
            "call_readiness_regressed",
            "assess_readiness_to_dial",
            {
                "missing_critical": ["date"],
                "confidence": 0.4,
                "rationale": "The updated date is not normalized.",
            },
        ),
        _text_chunks("Please provide an exact date."),
    ]

    orch, *_ = _build_orchestrator(
        state=state,
        user_dial_now_phrase="ignored",
        user_lang="en",
        merchant_lang="en",
        merchant_transcripts=[],
        llm_scripts=llm_scripts,
        tts_recorder=[],
        skip_task_planner_script=True,
    )

    regressed = await orch._process_ready_to_dial_hints([
        ("Actually change the date to next Friday", "en")
    ])
    emitted = []
    while not orch._events_queue.empty():
        emitted.append(orch._events_queue.get_nowait())

    assert regressed is True
    assert state.phase is TaskPhase.COLLECTING
    assert state.readiness is not None
    assert state.readiness.passed is False
    assert {
        "event": "transition",
        "from": "READY_TO_DIAL",
        "to": "COLLECTING",
    } in emitted
    assert any(
        event.get("event") == "readiness_change"
        and event.get("passed") is False
        and event.get("missing_critical") == ["date"]
        for event in emitted
    )


@pytest.mark.asyncio
async def test_ready_to_dial_supplement_not_skipped_when_handover_also_ready() -> None:
    state = TaskState(
        session_id="test-ready-regression-race",
        user_task_description="book a restaurant",
        phase=TaskPhase.READY_TO_DIAL,
        user_lang="en",
        merchant_lang="en",
        slots_schema=[
            SlotDef(
                name="date",
                description_zh="日期",
                description_en="Date",
                criticality="H",
                expected_type="date",
            )
        ],
        slots={"date": "2026-05-08"},
        readiness=ReadinessVerdict(
            missing_critical=[],
            confidence=0.9,
            override=False,
        ),
    )
    pending_hints = [("Actually change the date to next Friday", "en")]
    consume_calls = 0

    def consume_user_hints() -> list[tuple[str, str]]:
        nonlocal consume_calls
        consume_calls += 1
        if consume_calls == 1:
            return []
        out = list(pending_hints)
        pending_hints.clear()
        return out

    async def wait_for_handover() -> None:
        await asyncio.sleep(0)

    llm_scripts = [
        _tool_call_chunks(
            0,
            "call_collect_bad_date",
            "collect_user_intent",
            {"slot": "date", "value": "next Friday"},
        ),
        _tool_call_chunks(
            0,
            "call_readiness_regressed",
            "assess_readiness_to_dial",
            {
                "missing_critical": ["date"],
                "confidence": 0.4,
                "rationale": "The updated date is not normalized.",
            },
        ),
        _text_chunks("Please provide an exact date."),
    ]

    orch, *_ = _build_orchestrator(
        state=state,
        user_dial_now_phrase="ignored",
        user_lang="en",
        merchant_lang="en",
        merchant_transcripts=[],
        llm_scripts=llm_scripts,
        tts_recorder=[],
        skip_task_planner_script=True,
        consume_user_hints=consume_user_hints,
        wait_for_handover=wait_for_handover,
    )

    regressed = await orch._wait_for_handover_or_readiness_regression()

    assert regressed is True
    assert pending_hints == []
    assert state.phase is TaskPhase.COLLECTING


@pytest.mark.asyncio
async def test_orchestrator_handles_refused_task():
    """If task_planner refuses, state goes to FAILED without preflight."""
    state = TaskState(session_id="test-refused")
    state.user_lang = "zh"
    state.merchant_lang = "zh"

    user_transport = _OrchTransport("user")
    merchant_transport = _OrchTransport("merchant")
    user_stt = _OrchSTT([])
    merchant_stt = _OrchSTT([])
    llm = make_scripted_llm(_refused_task_planner_script())
    user_tts = _OrchTTS()
    merchant_tts = _OrchTTS()

    user_pipeline = VoicePipeline(
        user_transport, user_stt, llm, user_tts, "user-prompt"
    )
    merchant_pipeline = VoicePipeline(
        merchant_transport, merchant_stt, llm, merchant_tts, "merchant-prompt"
    )

    class _FUC:
        async def request_clarification(self, *a: Any, **k: Any) -> Any: ...
        async def push_event(self, *a: Any, **k: Any) -> None: ...
        async def receive_text(self) -> tuple[str, str]:
            return ("现在打吧", "zh")
        async def speak_text(self, text: str, *, lang: str) -> None: ...

    orch = DialogueOrchestrator(
        state=state,
        user_pipeline=user_pipeline,
        merchant_pipeline=merchant_pipeline,
        user_channel=_FUC(),
    )

    await asyncio.wait_for(orch.run("harass someone"), timeout=10.0)

    assert state.phase == TaskPhase.FAILED, (
        f"expected FAILED after refusal, got {state.phase}"
    )
    # Verify schema was NOT populated (refusal leaves fields empty).
    assert state.task_category == ""
    assert state.slots_schema == []


# ---------------------------------------------------------------------------
# merchant.lang sync after preflight (P1 — round-2 Codex fix)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_orchestrator_syncs_merchant_lang_from_state_after_preflight() -> None:
    """If preflight collects merchant_lang (mutating state), orchestrator
    must propagate it to ``self._merchant.lang`` before merchant execution
    so ``is_cross_lingual`` and relay direction read the fresh value.

    Topology: orchestrator built with merchant_lang='zh' initially. We
    mutate ``state.merchant_lang='en'`` between construction and run() to
    simulate preflight collecting it. After run(), merchant channel lang
    should be 'en'.
    """
    state = TaskState(session_id="test-merchant-lang-sync")
    orch, _, _, _, _, _ = _build_orchestrator(
        state=state,
        user_dial_now_phrase="现在打吧",
        user_lang="zh",
        merchant_lang="zh",  # initial — orch._merchant.lang captures this
        # No merchant transcripts → merchant loop exits immediately when
        # the input stream stalls (test-only behavior; production has STT).
        merchant_transcripts=[],
        # Merchant LLM emits finalize_task on first turn so run() returns
        # quickly. Empty list also works since merchant_transcripts=[]
        # never advances the merchant loop, but we provide a finalize
        # script in case the loop briefly wakes.
        llm_scripts=[
            _tool_call_chunks(0, "fin", "finalize_task", {
                "success": True,
                "summary": "done",
                "outcomes": {},
            }),
        ],
        tts_recorder=[],
    )

    # Sanity: at construction, merchant.lang reflects initial state.
    assert orch._merchant.lang == "zh"

    # Simulate preflight collecting merchant_lang='en' mid-flight.
    state.merchant_lang = "en"

    try:
        await asyncio.wait_for(orch.run("book sushi"), timeout=10.0)
    except (DialogueOrchestratorError, asyncio.TimeoutError):
        # The merchant loop may exit via various paths in this minimal
        # fake setup; what we care about is the post-preflight sync.
        pass

    # Post-preflight, merchant channel language must reflect collected
    # state, not the stale initial value.
    assert orch._merchant.lang == "en", (
        f"merchant.lang should sync to state.merchant_lang='en', "
        f"got {orch._merchant.lang!r}"
    )


@pytest.mark.asyncio
async def test_orchestrator_persists_fallback_merchant_lang_to_state() -> None:
    state = TaskState(session_id="test-merchant-lang-fallback", user_lang="en")
    orch, _, _, _, _, _ = _build_orchestrator(
        state=state,
        user_dial_now_phrase="dial now",
        user_lang="en",
        merchant_lang="zh",
        merchant_transcripts=[],
        llm_scripts=[
            _tool_call_chunks(0, "fin", "finalize_task", {
                "success": True,
                "summary": "done",
                "outcomes": {},
            }),
        ],
        tts_recorder=[],
    )
    state.merchant_lang = None

    try:
        await asyncio.wait_for(orch.run("Book sushi"), timeout=10.0)
    except (DialogueOrchestratorError, asyncio.TimeoutError):
        pass

    assert state.merchant_lang == "en"
    assert orch._merchant.lang == "en"


# ---------------------------------------------------------------------------
# Merchant clarification filler (P2 — round-2 Codex fix)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_merchant_clarification_speaks_default_filler_when_preceding_empty(
    monkeypatch,
) -> None:
    """When the LLM emits ``request_user_clarification`` with no preceding
    text, orchestrator must speak a default hold/filler to the merchant
    before the clarification wait begins, so the merchant doesn't hear
    silence (P2 round-2 Codex fix at orchestrator.py merchant clarification
    branch).
    """
    from vocalize.dialogue import orchestrator as orchestrator_module
    from vocalize.llm.base import ToolCall

    state = TaskState(session_id="test-clarif-filler")
    state.user_lang = "zh"
    state.merchant_lang = "en"
    state.phase = TaskPhase.EXECUTION_ACTIVE  # bypass state-machine for unit test

    orch, _, _, _, _, merchant_tts = _build_orchestrator(
        state=state,
        user_dial_now_phrase="现在打吧",
        user_lang="zh",
        merchant_lang="en",
        merchant_transcripts=[],
        llm_scripts=[],
        tts_recorder=[],
        skip_task_planner_script=True,
    )

    # Stub clarification.request_clarification so the merchant branch
    # returns immediately with a fixed answer, isolating the test to the
    # filler-speak path.
    async def _fake_request_clarification(**kwargs: Any) -> str:
        return "ok"

    monkeypatch.setattr(
        orchestrator_module.clarification,
        "request_clarification",
        _fake_request_clarification,
    )

    # Build a synthetic merchant-side request_user_clarification ToolCall.
    tc = ToolCall(
        id="tc1",
        name="request_user_clarification",
        arguments=json.dumps({
            "field_name": "special_requirements",
            "question_text": "Any allergies?",
            "target_lang": "zh",
            "urgency": "normal",
        }),
    )

    result = await orch._dispatch_one_tool(
        orch._merchant, tc, preceding_message="",
    )
    assert result["ok"] is True

    spoken = [c.text for c in merchant_tts.received_chunks]
    # English default filler since merchant.lang == 'en'.
    assert any("One moment" in s for s in spoken), (
        f"expected default English filler in merchant TTS chunks, got {spoken}"
    )


@pytest.mark.asyncio
async def test_merchant_clarification_speaks_llm_filler_when_preceding_present(
    monkeypatch,
) -> None:
    """If the LLM emits a non-empty preceding message, orchestrator must
    speak *that* (rather than the default) before the clarification wait.

    The LLM's text would otherwise be silently dropped because
    ``_run_llm_turn`` only routes assistant text to TTS on
    ``FinishChunk(reason='stop')``, not on ``reason='tool_calls'``.
    """
    from vocalize.dialogue import orchestrator as orchestrator_module
    from vocalize.llm.base import ToolCall

    state = TaskState(session_id="test-clarif-llmfiller")
    state.user_lang = "zh"
    state.merchant_lang = "en"
    state.phase = TaskPhase.EXECUTION_ACTIVE  # bypass state-machine for unit test

    orch, _, _, _, _, merchant_tts = _build_orchestrator(
        state=state,
        user_dial_now_phrase="现在打吧",
        user_lang="zh",
        merchant_lang="en",
        merchant_transcripts=[],
        llm_scripts=[],
        tts_recorder=[],
        skip_task_planner_script=True,
    )

    async def _fake_request_clarification(**kwargs: Any) -> str:
        return "ok"

    monkeypatch.setattr(
        orchestrator_module.clarification,
        "request_clarification",
        _fake_request_clarification,
    )

    tc = ToolCall(
        id="tc1",
        name="request_user_clarification",
        arguments=json.dumps({
            "field_name": "special_requirements",
            "question_text": "Any allergies?",
            "target_lang": "zh",
            "urgency": "normal",
        }),
    )

    await orch._dispatch_one_tool(
        orch._merchant, tc, preceding_message="Let me check on that.",
    )

    spoken = [c.text for c in merchant_tts.received_chunks]
    # The LLM's own filler text must be spoken; the default must NOT also
    # fire (avoid double-speech).
    assert any("Let me check on that" in s for s in spoken), (
        f"expected LLM-provided filler to be spoken, got {spoken}"
    )
    assert not any("One moment please" in s for s in spoken), (
        f"default filler should not fire when LLM provided one, got {spoken}"
    )


@pytest.mark.asyncio
async def test_orchestrator_emits_filler_frame_before_speak_merchant(
    monkeypatch,
) -> None:
    from vocalize.dialogue import orchestrator as orchestrator_module
    from vocalize.llm.base import ToolCall

    state = TaskState(session_id="test-clarif-filler-order")
    state.user_lang = "zh"
    state.merchant_lang = "en"
    state.phase = TaskPhase.EXECUTION_ACTIVE

    orch, *_ = _build_orchestrator(
        state=state,
        user_dial_now_phrase="现在打吧",
        user_lang="zh",
        merchant_lang="en",
        merchant_transcripts=[],
        llm_scripts=[],
        tts_recorder=[],
        skip_task_planner_script=True,
    )
    orch._current_segment_id = "seg-order"
    order: list[tuple[str, Any]] = []

    async def _fake_request_clarification(**_kwargs: Any) -> str:
        return "ok"

    async def _fake_push_event(event: dict[str, Any]) -> None:
        if event.get("event") == "transcript_update":
            order.append(("event", event))

    async def _fake_speak(text: str, _lang: str, **_kwargs: Any) -> None:
        order.append(("speak", text))

    monkeypatch.setattr(
        orchestrator_module.clarification,
        "request_clarification",
        _fake_request_clarification,
    )
    monkeypatch.setattr(orch, "_speak_merchant", _fake_speak)
    orch._user_channel.push_event = _fake_push_event  # type: ignore[method-assign]

    tc = ToolCall(
        id="tc1",
        name="request_user_clarification",
        arguments=json.dumps({
            "field_name": "special_requirements",
            "question_text": "Any allergies?",
            "target_lang": "zh",
        }),
    )

    await orch._dispatch_one_tool(
        orch._merchant,
        tc,
        preceding_message="Let me check on that.",
    )

    assert order[0][0] == "event"
    assert order[0][1]["subtype"] == "filler"
    assert order[0][1]["segment_id"] == "seg-order"
    assert order[1] == ("speak", "Let me check on that.")


@pytest.mark.asyncio
async def test_orchestrator_attaches_current_segment_id_to_ai_to_merchant_transcripts() -> None:
    state = TaskState(session_id="test-ai-merchant-segment")
    state.user_lang = "zh"
    state.merchant_lang = "zh"
    state.phase = TaskPhase.EXECUTION_ACTIVE
    pushed: list[dict[str, Any]] = []

    orch, *_ = _build_orchestrator(
        state=state,
        user_dial_now_phrase="现在打吧",
        user_lang="zh",
        merchant_lang="zh",
        merchant_transcripts=[],
        llm_scripts=[],
        tts_recorder=[],
        skip_task_planner_script=True,
    )
    orch._current_segment_id = "seg-current"

    async def push_event(event: dict[str, Any]) -> None:
        pushed.append(event)

    orch._user_channel.push_event = push_event  # type: ignore[method-assign]

    await orch._emit_ai_to_merchant_transcript("您好")

    assert pushed[-1]["role"] == "ai_to_merchant"
    assert pushed[-1]["segment_id"] == "seg-current"


@pytest.mark.asyncio
async def test_reactive_holding_filler_emits_subtype_filler() -> None:
    from vocalize.dialogue.reactive_holding import ReactiveHolding

    state = TaskState(session_id="test-reactive-filler-event")
    events: list[dict[str, Any]] = []
    spoken: list[str] = []

    async def emit_filler(text: str) -> None:
        events.append({
            "event": "transcript_update",
            "role": "ai_to_merchant",
            "text": text,
            "subtype": "filler",
        })

    async def speak(text: str) -> None:
        spoken.append(text)

    holding = ReactiveHolding(
        state=state,
        merchant_speak=speak,
        lang="zh",
        current_slot="party_size",
        current_question="几位？",
        default_value=4,
        emit_filler=emit_filler,
    )

    await holding.on_interruption()

    assert events
    assert events[0]["subtype"] == "filler"
    assert spoken == [events[0]["text"]]
    assert state.clarification_holds_used == 1


@pytest.mark.asyncio
async def test_orchestrator_call_site_passes_timeout_20() -> None:
    from vocalize.llm.base import ToolCall

    state = TaskState(session_id="test-clarif-field-timeout")
    state.user_lang = "zh"
    state.merchant_lang = "en"
    state.phase = TaskPhase.EXECUTION_ACTIVE

    orch, _, _, _, _, _ = _build_orchestrator(
        state=state,
        user_dial_now_phrase="现在打吧",
        user_lang="zh",
        merchant_lang="en",
        merchant_transcripts=[],
        llm_scripts=[],
        tts_recorder=[],
        skip_task_planner_script=True,
    )
    user_channel = orch._test_user_channel  # type: ignore[attr-defined]
    user_channel.queued_replies.append("no allergies")

    tc = ToolCall(
        id="tc1",
        name="request_user_clarification",
        arguments=json.dumps({
            "field_name": "allergies",
            "question_text": "Any allergies?",
            "target_lang": "zh",
            "urgency": "normal",
        }),
    )

    await orch._dispatch_one_tool(
        orch._merchant, tc, preceding_message="Let me check on that.",
    )

    assert user_channel.requests[-1] == (
        "Any allergies?",
        "zh",
        20.0,
        "allergies",
    )


@pytest.mark.asyncio
async def test_clarification_timeout_emits_ai_to_merchant_callback_intent_line(
    monkeypatch,
) -> None:
    from vocalize.dialogue import clarification as clarification_module
    from vocalize.llm.base import ToolCall

    state = TaskState(session_id="test-clarif-timeout-callback-line")
    state.user_lang = "zh"
    state.merchant_lang = "en"
    state.phase = TaskPhase.NEEDS_CLARIFICATION

    orch, _, _, _, _, _ = _build_orchestrator(
        state=state,
        user_dial_now_phrase="现在打吧",
        user_lang="zh",
        merchant_lang="en",
        merchant_transcripts=[],
        llm_scripts=[],
        tts_recorder=[],
        skip_task_planner_script=True,
    )
    pushed: list[dict[str, Any]] = []
    emitted: list[dict[str, Any]] = []

    async def push_event(event: dict[str, Any]) -> None:
        pushed.append(event)

    async def emit(event: dict[str, Any]) -> None:
        emitted.append(event)

    orch._user_channel.push_event = push_event  # type: ignore[method-assign]
    orch._emit = emit  # type: ignore[method-assign]

    async def fake_request_clarification(**kwargs: Any) -> str:
        assumption = state.record_uncertain_assumption(
            slot=kwargs["slot_name"],
            question=kwargs["merchant_question"],
            assumed_value=4,
            source="user_timeout",
        )
        raise clarification_module.ClarificationTimedOut(
            assumption_id=assumption.id,
            fallback_answer="4",
        )

    monkeypatch.setattr(
        clarification_module,
        "request_clarification",
        fake_request_clarification,
    )
    tc = ToolCall(
        id="tc-timeout",
        name="request_user_clarification",
        arguments=json.dumps({
            "field_name": "party_size",
            "question_text": "How many?",
            "target_lang": "zh",
        }),
    )

    await orch._dispatch_one_tool(
        orch._merchant, tc, preceding_message="Let me check.",
    )

    callback_line = next(
        event for event in pushed
        if event.get("event") == "transcript_update"
        and event.get("role") == "ai_to_merchant"
        and "call you back" in str(event.get("text", "")).lower()
    )
    timeout_event = next(
        event for event in emitted
        if event.get("event") == "clarification_timed_out"
    )
    assert callback_line["subtype"] == "original"
    assert timeout_event["assumption_id"] == state.uncertain_assumptions[-1].id


@pytest.mark.asyncio
async def test_orchestrator_emits_uncertain_assumption_added_after_clarification(
    monkeypatch,
) -> None:
    from datetime import datetime, timezone

    from vocalize.dialogue import orchestrator as orchestrator_module
    from vocalize.dialogue.state import SlotAssumption
    from vocalize.llm.base import ToolCall

    state = TaskState(session_id="test-timeout-assumption-event")
    state.user_lang = "zh"
    state.merchant_lang = "en"
    state.phase = TaskPhase.EXECUTION_ACTIVE
    seeded_assumption = SlotAssumption(
        id="a-99",
        slot="party_size",
        question="how many?",
        assumed_value=4,
        source="user_timeout",
        created_at=datetime.now(timezone.utc),
    )

    async def _fake_request_clarification(**kwargs: Any) -> str:
        kwargs["state"].uncertain_assumptions.append(seeded_assumption)
        return "4"

    monkeypatch.setattr(
        orchestrator_module.clarification,
        "request_clarification",
        _fake_request_clarification,
    )

    orch, _, _, _, _, _ = _build_orchestrator(
        state=state,
        user_dial_now_phrase="现在打吧",
        user_lang="zh",
        merchant_lang="en",
        merchant_transcripts=[],
        llm_scripts=[],
        tts_recorder=[],
        skip_task_planner_script=True,
    )
    tc = ToolCall(
        id="tc1",
        name="request_user_clarification",
        arguments=json.dumps({
            "field_name": "party_size",
            "question_text": "how many?",
            "target_lang": "zh",
            "urgency": "normal",
        }),
    )

    result = await orch._dispatch_one_tool(orch._merchant, tc, preceding_message="")

    events: list[dict[str, Any]] = []
    while not orch._events_queue.empty():
        events.append(orch._events_queue.get_nowait())

    assumption_events = [
        event for event in events
        if event["event"] == "uncertain_assumption_added"
    ]

    assert result == {"ok": True, "answer": "4"}
    assert [event["event"] for event in events] == [
        "clarification_started",
        "uncertain_assumption_added",
        "clarification_resolved",
    ]
    assert len(assumption_events) == 1
    assert assumption_events[0]["assumption"]["id"] == "a-99"
    assert assumption_events[0]["assumption"]["source"] == "user_timeout"


@pytest.mark.asyncio
async def test_orchestrator_handles_merchant_impatience_escalation(
    monkeypatch,
) -> None:
    from datetime import datetime, timezone

    from vocalize.dialogue import orchestrator as orchestrator_module
    from vocalize.dialogue.clarification import MerchantImpatienceError
    from vocalize.dialogue.state import SlotAssumption
    from vocalize.llm.base import ToolCall

    state = TaskState(session_id="test-merchant-impatience")
    state.user_lang = "zh"
    state.merchant_lang = "en"
    state.phase = TaskPhase.EXECUTION_ACTIVE
    seeded_assumption = SlotAssumption(
        id="a-1",
        slot="party_size",
        question="how many?",
        assumed_value=4,
        source="merchant_impatience",
        created_at=datetime.now(timezone.utc),
    )

    async def _raising_request_clarification(**kwargs: Any) -> str:
        kwargs["state"].uncertain_assumptions.append(seeded_assumption)
        kwargs["state"].clarification_holds_used = 3
        raise MerchantImpatienceError(slot="party_size")

    monkeypatch.setattr(
        orchestrator_module.clarification,
        "request_clarification",
        _raising_request_clarification,
    )

    orch, _, _, _, _, _ = _build_orchestrator(
        state=state,
        user_dial_now_phrase="现在打吧",
        user_lang="zh",
        merchant_lang="en",
        merchant_transcripts=[],
        llm_scripts=[],
        tts_recorder=[],
        skip_task_planner_script=True,
    )
    tc = ToolCall(
        id="tc1",
        name="request_user_clarification",
        arguments=json.dumps({
            "field_name": "party_size",
            "question_text": "how many?",
            "target_lang": "zh",
            "urgency": "normal",
        }),
    )

    with pytest.raises(MerchantImpatienceError):
        await orch._dispatch_one_tool(orch._merchant, tc, preceding_message="")

    events: list[dict[str, Any]] = []
    while not orch._events_queue.empty():
        events.append(orch._events_queue.get_nowait())

    assert any(
        event["event"] == "uncertain_assumption_added"
        and event["assumption"]["source"] == "merchant_impatience"
        for event in events
    )
    assert any(
        event["event"] == "escalation_warning"
        and event["reason"] == "merchant_impatience"
        and event["holds_used"] == 3
        for event in events
    )
    assert any(
        event["event"] == "phase_change"
        and event["previous"] == "execution_active"
        and event["current"] == "post_call_review"
        for event in events
    )
    assert state.phase == TaskPhase.POST_CALL_REVIEW


@pytest.mark.asyncio
async def test_orchestrator_preserves_impatience_error_when_assumption_missing(
    monkeypatch,
) -> None:
    from vocalize.dialogue import orchestrator as orchestrator_module
    from vocalize.dialogue.clarification import MerchantImpatienceError
    from vocalize.llm.base import ToolCall

    state = TaskState(session_id="test-merchant-impatience-empty")
    state.user_lang = "zh"
    state.merchant_lang = "en"
    state.phase = TaskPhase.EXECUTION_ACTIVE

    async def _raising_request_clarification(**_kwargs: Any) -> str:
        raise MerchantImpatienceError(slot="party_size")

    monkeypatch.setattr(
        orchestrator_module.clarification,
        "request_clarification",
        _raising_request_clarification,
    )
    orch, _, _, _, _, _ = _build_orchestrator(
        state=state,
        user_dial_now_phrase="现在打吧",
        user_lang="zh",
        merchant_lang="en",
        merchant_transcripts=[],
        llm_scripts=[],
        tts_recorder=[],
        skip_task_planner_script=True,
    )
    tc = ToolCall(
        id="tc1",
        name="request_user_clarification",
        arguments=json.dumps({
            "field_name": "party_size",
            "question_text": "how many?",
            "target_lang": "zh",
            "urgency": "normal",
        }),
    )

    with pytest.raises(MerchantImpatienceError):
        await orch._dispatch_one_tool(orch._merchant, tc, preceding_message="")

    assert state.phase == TaskPhase.EXECUTION_ACTIVE
    assert state.uncertain_assumptions == []


@pytest.mark.asyncio
async def test_merchant_clarification_pauses_and_resumes_transport(
    monkeypatch,
) -> None:
    """The orchestrator must pause the merchant transport for the duration
    of clarification and resume it afterward, so a real call leg actually
    enters its hold state (P1 — round-3 Codex fix).
    """
    from vocalize.llm.base import ToolCall

    state = TaskState(session_id="test-clarif-hold")
    state.user_lang = "zh"
    state.merchant_lang = "en"
    state.phase = TaskPhase.EXECUTION_ACTIVE

    orch, _, merchant_transport, _, _, _ = _build_orchestrator(
        state=state,
        user_dial_now_phrase="现在打吧",
        user_lang="zh",
        merchant_lang="en",
        merchant_transcripts=[],
        llm_scripts=[],
        tts_recorder=[],
        skip_task_planner_script=True,
    )

    # Stub user_channel.request_clarification with a quick reply so the
    # real clarification.request_clarification runs pause/resume around it.
    monkeypatch.setattr(
        orch._user_channel,
        "request_clarification",
        lambda prompt, lang, timeout_s, field=None: _quick_reply(),
    )

    tc = ToolCall(
        id="tc1",
        name="request_user_clarification",
        arguments=json.dumps({
            "field_name": "special_requirements",
            "question_text": "Any allergies?",
            "target_lang": "zh",
            "urgency": "normal",
        }),
    )

    await orch._dispatch_one_tool(
        orch._merchant, tc, preceding_message="Let me check on that.",
    )

    assert merchant_transport.outbound_log == [
        "pause_outbound", "resume_outbound",
    ], (
        f"expected pause then resume, got {merchant_transport.outbound_log}"
    )


async def _quick_reply():
    """Minimal awaitable returning a ClarificationReply-shaped object."""
    from vocalize.dialogue.user_channel import ClarificationReply
    return ClarificationReply(answer="ok", user_lang="zh", received_at=0.0)


# ---------------------------------------------------------------------------
# Relay language-pair drift guard (P1 — round-4 Codex fix)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_assistant_tool_call_message_has_no_content() -> None:
    """When the LLM emits text + tool_call together, the assistant
    message recorded in channel.messages must carry empty content (not
    the LLM's preceding text) so the OpenAI-compat serializer emits
    ``content: null`` alongside ``tool_calls`` — DeepSeek/OpenAI strict
    mode rejects the combination of non-null content + tool_calls on a
    single assistant message.
    """
    state = TaskState(session_id="test-tc-content-null")
    state.user_lang = "zh"
    state.merchant_lang = "zh"

    orch, _, _, _, _, merchant_tts = _build_orchestrator(
        state=state,
        user_dial_now_phrase="现在打吧",
        user_lang="zh",
        merchant_lang="zh",
        merchant_transcripts=[
            _final_transcript("您好", lang="zh"),
        ],
        # Merchant turn: LLM emits a polite closing text +
        # finalize_task tool call together.
        llm_scripts=[
            [
                _td("好的，谢谢。再见。"),
                _tcd(0, "tc1", "finalize_task", json.dumps({
                    "success": True, "summary": "done", "outcomes": {},
                })),
                FinishChunk(reason="tool_calls"),
            ],
        ],
        tts_recorder=[],
    )

    try:
        await asyncio.wait_for(orch.run("订位"), timeout=10.0)
    except (DialogueOrchestratorError, asyncio.TimeoutError):
        pass

    # Locate the assistant message that carries the tool_call. Its
    # content must be empty so serialization yields content: null.
    tc_assistant_msgs = [
        m for m in orch._merchant.messages
        if m.role == "assistant" and m.tool_calls
    ]
    assert tc_assistant_msgs, "no assistant tool-call message recorded"
    for m in tc_assistant_msgs:
        assert not m.content, (
            f"assistant tool-call message must have empty content, "
            f"got {m.content!r}"
        )

    assert [chunk.text for chunk in merchant_tts.received_chunks] == [
        "好的，谢谢。再见。"
    ]


@pytest.mark.asyncio
async def test_merchant_clarification_recovers_from_bad_args() -> None:
    """Missing required fields (or non-object args) on the merchant
    clarification path must surface as a recoverable ``{ok: False}`` —
    not raise ``KeyError`` and abort the turn."""
    from vocalize.llm.base import ToolCall

    state = TaskState(session_id="test-clarif-bad-args")
    state.user_lang = "zh"
    state.merchant_lang = "en"
    state.phase = TaskPhase.EXECUTION_ACTIVE

    orch, _, _, _, _, _ = _build_orchestrator(
        state=state,
        user_dial_now_phrase="现在打吧",
        user_lang="zh",
        merchant_lang="en",
        merchant_transcripts=[],
        llm_scripts=[],
        tts_recorder=[],
        skip_task_planner_script=True,
    )

    # Missing required ``target_lang`` field.
    tc_missing_field = ToolCall(
        id="tc1",
        name="request_user_clarification",
        arguments=json.dumps({
            "field_name": "x",
            "question_text": "y",
            # target_lang missing
            "urgency": "normal",
        }),
    )
    result = await orch._dispatch_one_tool(orch._merchant, tc_missing_field)
    assert result["ok"] is False
    assert "target_lang" in result["error"]

    # Non-object args.
    tc_non_object = ToolCall(
        id="tc2",
        name="request_user_clarification",
        arguments="[]",
    )
    result = await orch._dispatch_one_tool(orch._merchant, tc_non_object)
    assert result["ok"] is False
    assert "JSON object" in result["error"]


@pytest.mark.asyncio
async def test_orchestrator_falls_back_to_user_lang_when_merchant_lang_unset() -> None:
    """If preflight short-circuits before merchant_lang is collected, the
    post-preflight sync must fall back to user_lang for the merchant
    channel — not leave a stale constructor default that mismatches the
    user's language and breaks cross-lingual routing.
    """
    state = TaskState(session_id="test-merchant-lang-fallback")
    state.user_lang = "en"
    # state.merchant_lang stays None — the dial-now path won't collect it.

    orch, _, _, _, _, _ = _build_orchestrator(
        state=state,
        user_dial_now_phrase="dial now",
        user_lang="en",
        merchant_lang="zh",  # constructor seeds zh; sync should override
        merchant_transcripts=[],
        llm_scripts=[
            _tool_call_chunks(0, "fin", "finalize_task", {
                "success": True, "summary": "done", "outcomes": {},
            }),
        ],
        tts_recorder=[],
    )
    # _build_orchestrator overwrites merchant_lang on state; reset so the
    # dial-now path reaches post-preflight with merchant_lang still None.
    state.merchant_lang = None  # type: ignore[assignment]

    try:
        await asyncio.wait_for(orch.run("Book a table"), timeout=10.0)
    except (DialogueOrchestratorError, asyncio.TimeoutError):
        pass

    assert orch._merchant.lang == "en", (
        f"merchant.lang should fall back to user_lang='en' when "
        f"state.merchant_lang is None, got {orch._merchant.lang!r}"
    )


@pytest.mark.asyncio
async def test_orchestrator_detects_user_lang_from_task_description() -> None:
    """If state.user_lang is unset, run() must infer it from the task
    description so Layer 1 is prompted in the right language. An English
    task on a fresh state should produce user_lang='en' (not the 'zh'
    default from the constructor)."""
    state = TaskState(session_id="test-detect-lang")
    # Deliberately leave state.user_lang unset.
    state.user_lang = None  # type: ignore[assignment]

    orch, _, _, _, _, _ = _build_orchestrator(
        state=state,
        user_dial_now_phrase="dial now",
        user_lang="zh",  # constructor seeds the channel; run() overrides
        merchant_lang="en",
        merchant_transcripts=[],
        llm_scripts=[
            _tool_call_chunks(0, "fin", "finalize_task", {
                "success": True, "summary": "done", "outcomes": {},
            }),
        ],
        tts_recorder=[],
    )
    # _build_orchestrator sets state.user_lang via assignment; revert so
    # run() exercises the detection path.
    state.user_lang = None  # type: ignore[assignment]

    try:
        await asyncio.wait_for(
            orch.run("Help me book a table for two at Joy Sushi"),
            timeout=10.0,
        )
    except (DialogueOrchestratorError, asyncio.TimeoutError):
        pass

    assert state.user_lang == "en", (
        f"expected user_lang='en' detected from English task, "
        f"got {state.user_lang!r}"
    )
    assert orch._user.lang == "en", (
        f"expected user channel synced to en, got {orch._user.lang!r}"
    )


@pytest.mark.asyncio
async def test_run_relay_falls_back_when_target_lang_matches_source() -> None:
    """If the LLM emits a same-language target_lang (e.g. en→en), there's no
    relay_en_to_en template — the orchestrator must fall back to echoing the
    source text instead of raising FileNotFoundError.
    """
    state = TaskState(session_id="test-relay-samelang")
    state.user_lang = "en"
    state.merchant_lang = "en"

    orch, _, _, _, _, _ = _build_orchestrator(
        state=state,
        user_dial_now_phrase="dial now",
        user_lang="en",
        merchant_lang="en",
        merchant_transcripts=[],
        llm_scripts=[],
        tts_recorder=[],
        skip_task_planner_script=True,
    )

    # Drive _run_relay directly with a same-language pair; it should not
    # try to load a non-existent template.
    result = await orch._run_relay(
        calling_channel=orch._merchant,
        target_lang="en",  # drift: same as merchant.lang
        source_text="Hello there.",
    )
    assert result == "Hello there.", (
        f"expected echo fallback, got {result!r}"
    )


# ---------------------------------------------------------------------------
# event_stream() — terminal-event contract (post-merge audit gap)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_event_stream_terminates_with_failed_when_no_finalize() -> None:
    """``event_stream()`` is the orchestrator's primary success-signal
    contract for callers. When the merchant loop exits without a
    ``finalize_task`` transition (e.g. STT stream exhausted), the
    terminal event must be ``"failed"`` — not ``"completed"`` — so
    consumers don't mistakenly report success.
    """
    state = TaskState(session_id="test-evt-stream-failed")
    orch, _, _, _, _, _ = _build_orchestrator(
        state=state,
        user_dial_now_phrase="dial now",
        user_lang="en",
        merchant_lang="en",
        # No merchant transcripts → merchant loop sees no input → exits.
        merchant_transcripts=[],
        llm_scripts=[],
        tts_recorder=[],
    )

    events: list[dict] = []

    async def _consume_events() -> None:
        async for ev in orch.event_stream():
            events.append(ev)

    consumer = asyncio.create_task(_consume_events())
    try:
        await asyncio.wait_for(orch.run("Book a table"), timeout=10.0)
    except (DialogueOrchestratorError, asyncio.TimeoutError):
        pass
    await asyncio.wait_for(consumer, timeout=5.0)

    assert events, "event_stream produced no events"
    terminal = events[-1]
    assert terminal.get("event") in ("completed", "failed"), (
        f"final event must be terminal, got {terminal!r}"
    )
    # No finalize_task fired → must be "failed".
    assert terminal["event"] == "failed", (
        f"expected 'failed' when no finalize_task ran, got {terminal!r}"
    )


@pytest.mark.asyncio
async def test_event_stream_terminates_with_post_call_review_on_impatience(
    monkeypatch,
) -> None:
    from datetime import datetime, timezone

    from vocalize.dialogue import orchestrator as orchestrator_module
    from vocalize.dialogue.clarification import MerchantImpatienceError
    from vocalize.dialogue.state import SlotAssumption

    state = TaskState(session_id="test-evt-stream-post-call-review")
    seeded_assumption = SlotAssumption(
        id="a-1",
        slot="party_size",
        question="how many?",
        assumed_value=4,
        source="merchant_impatience",
        created_at=datetime.now(timezone.utc),
    )

    async def _raising_request_clarification(**kwargs: Any) -> str:
        kwargs["state"].uncertain_assumptions.append(seeded_assumption)
        kwargs["state"].clarification_holds_used = 3
        raise MerchantImpatienceError(slot="party_size")

    monkeypatch.setattr(
        orchestrator_module.clarification,
        "request_clarification",
        _raising_request_clarification,
    )

    orch, _, _, _, _, _ = _build_orchestrator(
        state=state,
        user_dial_now_phrase="dial now",
        user_lang="en",
        merchant_lang="en",
        merchant_transcripts=[_final_transcript("How many?", lang="en")],
        llm_scripts=[
            _tool_call_chunks(0, "clarify", "request_user_clarification", {
                "field_name": "party_size",
                "question_text": "how many?",
                "target_lang": "en",
                "urgency": "normal",
            }),
        ],
        tts_recorder=[],
    )
    events: list[dict[str, Any]] = []

    async def _consume_events() -> None:
        async for event in orch.event_stream():
            events.append(event)

    consumer = asyncio.create_task(_consume_events())
    await asyncio.wait_for(orch.run("Book a table"), timeout=10.0)
    await asyncio.wait_for(consumer, timeout=5.0)

    assert events[-1]["event"] == "post_call_review"
    assert events[-1]["phase"] == "post_call_review"
    assert not any(event["event"] == "failed" for event in events)
    assert state.phase == TaskPhase.POST_CALL_REVIEW


@pytest.mark.asyncio
async def test_event_stream_emits_completed_on_finalize_success() -> None:
    """When merchant LLM emits ``finalize_task(success=True)``, the
    terminal event must be ``"completed"`` and ``state.phase`` must be
    ``COMPLETED``."""
    state = TaskState(session_id="test-evt-stream-completed")
    orch, _, _, _, _, _ = _build_orchestrator(
        state=state,
        user_dial_now_phrase="dial now",
        user_lang="en",
        merchant_lang="en",
        merchant_transcripts=[
            _final_transcript("Hello", lang="en"),
        ],
        llm_scripts=[
            _tool_call_chunks(0, "fin", "finalize_task", {
                "success": True, "summary": "booked", "outcomes": {},
            }),
        ],
        tts_recorder=[],
    )

    events: list[dict] = []

    async def _consume_events() -> None:
        async for ev in orch.event_stream():
            events.append(ev)

    consumer = asyncio.create_task(_consume_events())
    try:
        await asyncio.wait_for(orch.run("Book a table"), timeout=10.0)
    except (DialogueOrchestratorError, asyncio.TimeoutError):
        pass
    await asyncio.wait_for(consumer, timeout=5.0)

    assert events[-1]["event"] == "completed", (
        f"expected 'completed' on finalize_task success, got {events[-1]!r}"
    )
    assert state.phase == TaskPhase.COMPLETED


# ---------------------------------------------------------------------------
# Transport hold contract (post-merge audit gap)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_clarification_pause_resume_called_on_real_path(
    monkeypatch,
) -> None:
    """End-to-end: a merchant ``request_user_clarification`` going through
    the real ``clarification.request_clarification`` coordinator must
    call ``pause_outbound`` before the user wait and ``resume_outbound``
    after — verifying the hold contract on the production code path."""
    from vocalize.llm.base import ToolCall

    state = TaskState(session_id="test-hold-real-path")
    state.user_lang = "zh"
    state.merchant_lang = "en"
    state.phase = TaskPhase.EXECUTION_ACTIVE

    orch, _, merchant_transport, _, _, _ = _build_orchestrator(
        state=state,
        user_dial_now_phrase="现在打吧",
        user_lang="zh",
        merchant_lang="en",
        merchant_transcripts=[],
        llm_scripts=[],
        tts_recorder=[],
        skip_task_planner_script=True,
    )
    monkeypatch.setattr(
        orch._user_channel,
        "request_clarification",
        lambda prompt, lang, timeout_s, field=None: _quick_reply(),
    )

    tc = ToolCall(
        id="tc1",
        name="request_user_clarification",
        arguments=json.dumps({
            "field_name": "special_requirements",
            "question_text": "Any allergies?",
            "target_lang": "zh",
            "urgency": "normal",
        }),
    )
    await orch._dispatch_one_tool(orch._merchant, tc, preceding_message="hold on")

    assert merchant_transport.outbound_log == ["pause_outbound", "resume_outbound"], (
        f"hold contract violated: outbound_log={merchant_transport.outbound_log}"
    )
