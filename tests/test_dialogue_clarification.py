"""dialogue.clarification + dialogue.user_channel tests.

Three test groups live in this file:

1. ``user_channel`` / ``clarification_reply`` / ``local_mic`` tests (Plan 04-07,
   Wave 2 sub-wave 2a) — exercise the ``UserChannel`` Protocol and its only
   Phase 4 concrete impl ``LocalMicUserChannel``. Filled in by Plan 04-07.
2. ``clarification`` tests proper — pause/ask/resume cross-channel coordination
   (Plan 04-08), refactored for TaskState + dynamic schema + 12s keepalive.
3. End-to-end scenario test ``test_zh_en_allergy_clarification`` — skipped
   pending orchestrator update (Task 15) which will wire the new
   ``request_clarification`` callback-based API.

Pattern (v1 core engine): clarification.py uses ``asyncio.create_task`` for
keepalive filler loop + ``asyncio.wait_for`` for user reply timeout. Tests
assert slot storage in ``state.slots`` dict, phase transitions, and
merchant_held flag.
"""
from __future__ import annotations

import asyncio
import inspect
import logging
import time
from collections.abc import AsyncIterator

import pytest

from vocalize.stt.base import Transcript
from vocalize.transports.base import AudioEncoding
from vocalize.tts.base import TextChunk

# pytest-asyncio is configured with ``asyncio_mode = "auto"`` in pyproject.toml,
# so async test functions are auto-marked. We deliberately do NOT set a
# module-level ``pytestmark = pytest.mark.asyncio`` because it would emit
# warnings on the sync tests below (Plan 04-07: dataclass / signature smoke
# tests are sync; only LocalMicUserChannel I/O tests are async).

# Module-level production imports — wrap so collection succeeds in Wave 0.
try:
    from vocalize.dialogue.clarification import (  # noqa: F401
        ClarificationTimedOut,
        request_clarification,
    )
    from vocalize.dialogue.state import (  # noqa: F401
        DialogueOrchestratorError,
        TaskPhase,
        TaskState,
    )

    _CLARIFICATION_AVAILABLE = True
except ImportError:
    _CLARIFICATION_AVAILABLE = False


# ---------------------------------------------------------------------------
# Fakes for UserChannel tests (Plan 04-07).
#
# The recording_audio_transport conftest fixture tracks pause_outbound /
# resume_outbound (consumed by Plan 04-08). For Plan 04-07 we need a transport
# whose ``input_stream`` actually yields bytes so the FakeSTT below can
# iterate, and whose ``output_stream`` blocks until the TTS iterator is fully
# drained (the half-duplex contract — input gate releases AFTER output drains).
#
# Keep these fakes local to this file so they stay scoped to user_channel
# tests; the conftest fixture is the broader cross-plan fake.
# ---------------------------------------------------------------------------


def test_clarification_default_timeout_is_20() -> None:
    assert (
        inspect.signature(request_clarification)
        .parameters["timeout_s"]
        .default
    ) == 20.0


class _UCFakeTransport:
    """AudioTransport-shape fake for LocalMicUserChannel tests.

    ``input_stream`` yields a single short audio frame then awaits closure,
    enough to satisfy the STT iterator pattern; ``output_stream`` records
    every chunk written by the TTS pump.
    """

    sample_rate: int = 16000
    channels: int = 1
    encoding: AudioEncoding = "pcm_s16le"

    def __init__(self) -> None:
        self.recorded_output: list[bytes] = []
        self.closed = False
        self._input_done = asyncio.Event()
        self.outbound_log: list[str] = []

    async def input_stream(self) -> AsyncIterator[bytes]:
        # One frame so the STT iterator is non-empty; then park.
        yield b"\x00" * 960
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


class _UCFakeSTT:
    """STTService fake — yields a scripted transcript list and stops.

    Iterates over the input audio iterator first (mirrors real-world
    behaviour where STT consumes audio before yielding) so the transport's
    output drain is actually observed before any final transcript appears.
    """

    def __init__(self, transcripts: list[Transcript], drain_input: bool = True) -> None:
        self._transcripts = transcripts
        self._drain_input = drain_input
        self.audio_chunks_seen: list[bytes] = []

    async def stream_transcribe(
        self, audio_chunks: AsyncIterator[bytes]
    ) -> AsyncIterator[Transcript]:
        if self._drain_input:
            async for chunk in audio_chunks:
                self.audio_chunks_seen.append(chunk)
                break  # one frame is enough — _UCFakeTransport then parks.
        for t in self._transcripts:
            yield t
            await asyncio.sleep(0)


class _UCFakeTTS:
    """TTSService fake — records the TextChunk(s) handed in, emits fixed audio."""

    output_sample_rate: int = 24000
    output_encoding: AudioEncoding = "pcm_s16le"

    def __init__(self, audio: list[bytes] | None = None) -> None:
        self._audio = list(audio) if audio is not None else [b"TTSAUDIO"]
        self.received_chunks: list[TextChunk] = []

    async def stream_synthesize(
        self, text_chunks: AsyncIterator[TextChunk]
    ) -> AsyncIterator[bytes]:
        async for c in text_chunks:
            self.received_chunks.append(c)
        for b in self._audio:
            yield b


def _final_transcript(text: str, lang: str | None = "zh") -> Transcript:
    return Transcript(
        text=text,
        is_final=True,
        confidence=0.95,
        start_time=0.0,
        end_time=1.0,
        utterance_id=1,
        language=lang,
    )


class _LoopMerchantAudio:
    """Test fake: yields configured PCM blocks and then parks."""

    def __init__(self, blocks: list[bytes], *, park_after: bool = True) -> None:
        self._blocks = list(blocks)
        self._park_after = park_after

    async def input_stream(self) -> AsyncIterator[bytes]:
        for block in self._blocks:
            yield block
            await asyncio.sleep(0)
        if self._park_after:
            await asyncio.Event().wait()


class _RecordingKeepalive:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.stopped = False
        self.reactive_filler_notes = 0
        self._stop_event = asyncio.Event()

    async def run(self) -> None:
        self.started.set()
        await self._stop_event.wait()

    def stop(self) -> None:
        self.stopped = True
        self._stop_event.set()

    def note_reactive_filler(self) -> None:
        self.reactive_filler_notes += 1


# ---------------------------------------------------------------------------
# Plan 04-07: UserChannel Protocol + ClarificationReply + LocalMicUserChannel
# ---------------------------------------------------------------------------


def test_clarification_reply_dataclass_construct() -> None:
    """Smoke: ClarificationReply is a dataclass with answer/user_lang/received_at."""
    from vocalize.dialogue.user_channel import ClarificationReply

    reply = ClarificationReply(answer="hi", user_lang="zh", received_at=123.0)
    assert reply.answer == "hi"
    assert reply.user_lang == "zh"
    assert reply.received_at == 123.0


def test_user_channel_protocol_runtime_check() -> None:
    """``UserChannel`` is runtime_checkable; LocalMicUserChannel satisfies it.

    Also verifies a non-conforming object (str) is rejected so we know the
    Protocol actually has structural members (vs. an empty Protocol that
    matches everything).
    """
    from vocalize.dialogue.user_channel import LocalMicUserChannel, UserChannel

    transport = _UCFakeTransport()
    stt = _UCFakeSTT([])
    tts = _UCFakeTTS()
    channel = LocalMicUserChannel(transport, stt, tts)
    assert isinstance(channel, UserChannel)
    assert not isinstance("not a channel", UserChannel)


def test_local_mic_user_channel_init_signature() -> None:
    """B-2 / D-03: __init__ signature is FROZEN to (self, transport, stt, tts).

    The Plan 10 demo wires `LocalMicUserChannel(transport, stt, tts)` literally;
    any drift breaks the demo. This test locks the contract in place.
    """
    from vocalize.dialogue.user_channel import LocalMicUserChannel

    params = inspect.signature(LocalMicUserChannel.__init__).parameters
    non_self = [n for n in params if n != "self"]
    assert non_self == ["transport", "stt", "tts"], f"signature drift: {non_self}"


async def test_local_mic_request_clarification_happy_path() -> None:
    """Speak the prompt via TTS, capture the first final transcript, return reply."""
    from vocalize.dialogue.user_channel import (
        ClarificationReply,
        LocalMicUserChannel,
    )

    transport = _UCFakeTransport()
    stt = _UCFakeSTT([_final_transcript("没有过敏", lang="zh")])
    tts = _UCFakeTTS(audio=[b"TTS-PROMPT-AUDIO"])
    channel = LocalMicUserChannel(transport, stt, tts)

    before = time.monotonic()
    reply = await channel.request_clarification("过敏吗？", "zh", 5.0)
    after = time.monotonic()

    assert isinstance(reply, ClarificationReply)
    assert reply.answer == "没有过敏"
    assert reply.user_lang == "zh"
    assert before <= reply.received_at <= after

    # TTS prompt content was a single final-segment chunk in the requested lang.
    assert len(tts.received_chunks) == 1
    chunk = tts.received_chunks[0]
    assert chunk.text == "过敏吗？"
    assert chunk.language == "zh"
    assert chunk.is_final_segment is True
    # TTS audio actually pumped through the transport.
    assert transport.recorded_output == [b"TTS-PROMPT-AUDIO"]


async def test_local_mic_request_clarification_skips_partials_until_final() -> None:
    """Partial transcripts (is_final=False) are ignored; only first final wins."""
    from vocalize.dialogue.user_channel import LocalMicUserChannel

    partial = Transcript(
        text="没有",
        is_final=False,
        confidence=0.5,
        start_time=0.0,
        end_time=0.5,
        utterance_id=1,
        language="zh",
    )
    final = _final_transcript("没有过敏的东西", lang="zh")
    transport = _UCFakeTransport()
    stt = _UCFakeSTT([partial, final])
    tts = _UCFakeTTS()
    channel = LocalMicUserChannel(transport, stt, tts)

    reply = await channel.request_clarification("过敏吗？", "zh", 5.0)
    assert reply.answer == "没有过敏的东西"


async def test_local_mic_request_clarification_timeout() -> None:
    """No final transcript within timeout_s → asyncio.TimeoutError per T-04-10."""
    from vocalize.dialogue.user_channel import LocalMicUserChannel

    transport = _UCFakeTransport()
    stt = _UCFakeSTT([])  # never yields → wait_for trips
    tts = _UCFakeTTS()
    channel = LocalMicUserChannel(transport, stt, tts)

    with pytest.raises(asyncio.TimeoutError):
        await channel.request_clarification("过敏吗？", "zh", 0.05)


async def test_local_mic_push_event_logs(caplog: pytest.LogCaptureFixture) -> None:
    """push_event(dict) emits an INFO log and returns None."""
    from vocalize.dialogue.user_channel import LocalMicUserChannel

    transport = _UCFakeTransport()
    stt = _UCFakeSTT([])
    tts = _UCFakeTTS()
    channel = LocalMicUserChannel(transport, stt, tts)

    with caplog.at_level(logging.INFO, logger="vocalize.dialogue.user_channel"):
        result = await channel.push_event({"type": "clarif_started", "field": "phone"})

    assert result is None
    assert any("clarif_started" in rec.message for rec in caplog.records)


# ---------------------------------------------------------------------------
# TextUserChannel — stdin/stdout-backed UserChannel for the demo (post-04-10
# topology fix). Mirrors LocalMicUserChannel semantics but with no audio deps.
# ---------------------------------------------------------------------------


async def test_text_user_channel_satisfies_protocol() -> None:
    """TextUserChannel implements UserChannel Protocol (runtime_checkable)
    AND has no transport / stt / tts constructor dependencies — it is the
    demo's text I/O implementation per the post-04-10 topology fix.
    """
    from vocalize.dialogue.user_channel import TextUserChannel, UserChannel

    channel = TextUserChannel()  # zero-arg ctor — no audio deps
    assert isinstance(channel, UserChannel)


async def test_text_user_channel_happy_path(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """request_clarification prints prompt with [AI → 用户] prefix to stdout
    and reads one line from stdin (via asyncio.to_thread(input, ...)),
    returning a ClarificationReply with the trimmed answer.
    """
    from vocalize.dialogue.user_channel import (
        ClarificationReply,
        TextUserChannel,
    )

    # Stub the builtin input(prompt) that asyncio.to_thread will dispatch to.
    captured_input_prompts: list[str] = []

    def _fake_input(prompt: str = "") -> str:
        captured_input_prompts.append(prompt)
        return "  没有过敏  "  # whitespace deliberately added — must be stripped

    monkeypatch.setattr("builtins.input", _fake_input)

    channel = TextUserChannel()
    before = time.monotonic()
    reply = await channel.request_clarification("过敏吗？", "zh", 5.0)
    after = time.monotonic()

    assert isinstance(reply, ClarificationReply)
    assert reply.answer == "没有过敏"  # stripped
    assert reply.user_lang == "zh"
    assert before <= reply.received_at <= after

    # Prompt printed to stdout with the [AI → 用户] prefix.
    out = capsys.readouterr().out
    assert "[AI → 用户] 过敏吗？" in out

    # The [用户 → AI] input cue is printed to stdout (NOT delivered via
    # input(prompt) — Mac libedit writes input's prompt arg to stderr,
    # which would lose it under `2> log` redirection).
    assert "[用户 → AI]" in out
    # input() itself is called with empty string (prompt printed separately).
    assert captured_input_prompts == [""]


async def test_text_user_channel_empty_line_raises_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty stdin line (user just hits Enter) is treated as 'no reply'
    and surfaces as asyncio.TimeoutError — matches LocalMicUserChannel's
    'no final transcript' branch so callers (clarification.py) can use a
    single timeout-handling code path regardless of channel impl.
    """
    from vocalize.dialogue.user_channel import TextUserChannel

    monkeypatch.setattr("builtins.input", lambda prompt="": "   ")  # whitespace only

    channel = TextUserChannel()
    with pytest.raises(asyncio.TimeoutError):
        await channel.request_clarification("过敏吗？", "zh", 5.0)


async def test_text_user_channel_push_event_logs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """push_event(dict) emits an INFO log and returns None — same Phase 4
    behaviour as LocalMicUserChannel.push_event.
    """
    from vocalize.dialogue.user_channel import TextUserChannel

    channel = TextUserChannel()

    with caplog.at_level(logging.INFO, logger="vocalize.dialogue.user_channel"):
        result = await channel.push_event({"type": "clarif_started", "field": "phone"})

    assert result is None
    assert any("clarif_started" in rec.message for rec in caplog.records)


# ---------------------------------------------------------------------------
# v1 core engine helpers
# ---------------------------------------------------------------------------


def _make_execution_active_state() -> TaskState:
    """TaskState with phase walked to EXECUTION_ACTIVE (the only legal entry
    phase for clarification per LEGAL_TASK_TRANSITIONS).

    Path: DRAFT → TASK_PLANNING → COLLECTING → READY_TO_DIAL → EXECUTION_ACTIVE.
    """
    state = TaskState(session_id="t")
    state.transition(TaskPhase.TASK_PLANNING, reason="test setup")
    state.transition(TaskPhase.COLLECTING, reason="test setup")
    state.transition(TaskPhase.READY_TO_DIAL, reason="test setup")
    state.transition(TaskPhase.EXECUTION_ACTIVE, reason="test setup")
    return state


# ---------------------------------------------------------------------------
# v1 core engine clarification tests (callback-based API)
# ---------------------------------------------------------------------------


async def test_clarification_stores_slot_and_transitions_back() -> None:
    """Happy path: user_channel_request_fn returns answer → stored in
    state.slots, pending_clarifications recorded, phase restored.
    """
    state = _make_execution_active_state()
    state.merchant_lang = "zh"

    requests_log: list[tuple[str, str, str]] = []

    async def user_fn(slot_name: str, question: str, target_lang: str) -> str:
        requests_log.append((slot_name, question, target_lang))
        return "没有过敏"

    merchant_spoken: list[str] = []

    async def merchant_speak(text: str) -> None:
        merchant_spoken.append(text)

    answer = await request_clarification(
        state=state,
        slot_name="allergy",
        merchant_question="过敏吗？",
        target_lang="zh",
        user_channel_request_fn=user_fn,
        merchant_speak_fn=merchant_speak,
        timeout_s=5.0,
    )

    assert answer == "没有过敏"
    assert state.slots["allergy"] == "没有过敏"
    assert state.merchant_held is False
    assert len(state.pending_clarifications) == 1
    item = state.pending_clarifications[0]
    assert item.field == "allergy"
    assert item.question == "过敏吗？"
    assert item.answer == "没有过敏"
    # State machine returned to EXECUTION_ACTIVE after the resume.
    assert state.phase == TaskPhase.EXECUTION_ACTIVE
    # The user_channel_request_fn was called exactly once with expected args.
    assert requests_log == [("allergy", "过敏吗？", "zh")]


async def test_clarification_timeout_raises_DialogueOrchestratorError() -> None:
    """User reply timeout → DialogueOrchestratorError; state still restored
    via finally block (T-04-12 mitigation).
    """
    state = _make_execution_active_state()

    async def slow_user_fn(slot_name: str, question: str, target_lang: str) -> str:
        await asyncio.sleep(10)  # way longer than timeout
        return ""  # unreachable

    async def merchant_speak(text: str) -> None:
        pass

    with pytest.raises(DialogueOrchestratorError, match="clarification timeout"):
        await request_clarification(
            state=state,
            slot_name="phone",
            merchant_question="电话号码？",
            target_lang="zh",
            user_channel_request_fn=slow_user_fn,
            merchant_speak_fn=merchant_speak,
            timeout_s=0.05,
        )

    # CRITICAL invariant: state restored even on timeout (T-04-12).
    assert state.merchant_held is False
    # State machine returned to EXECUTION_ACTIVE even on the failure path.
    assert state.phase == TaskPhase.EXECUTION_ACTIVE
    # Slot NOT set — timeout means no answer.
    assert state.slots.get("phone") is None


async def test_clarification_timeout_records_user_timeout_assumption() -> None:
    state = _make_execution_active_state()
    state.merchant_lang = "zh"
    spoken: list[str] = []

    async def merchant_speak(text: str) -> None:
        spoken.append(text)

    async def user_request(_slot: str, _question: str, _lang: str) -> str:
        await asyncio.sleep(0.5)
        return ""

    with pytest.raises(ClarificationTimedOut) as exc_info:
        await request_clarification(
            state=state,
            slot_name="party_size",
            merchant_question="how many?",
            target_lang="en",
            user_channel_request_fn=user_request,
            merchant_speak_fn=merchant_speak,
            timeout_s=0.05,
            assumed_value=4,
        )

    answer = exc_info.value.fallback_answer
    assert answer == "4"
    assert state.phase == TaskPhase.EXECUTION_ACTIVE
    assert state.slots["party_size"] == "4"
    assert state.uncertain_assumptions[-1].source == "user_timeout"
    assert state.uncertain_assumptions[-1].assumed_value == 4
    assert any("再确认一下" in s and "回您电话" in s for s in spoken), spoken


async def test_timeout_announces_to_merchant_en() -> None:
    state = _make_execution_active_state()
    state.merchant_lang = "en"
    spoken: list[str] = []

    async def merchant_speak(text: str) -> None:
        spoken.append(text)

    async def user_request(_slot: str, _question: str, _lang: str) -> str:
        await asyncio.sleep(0.5)
        return ""

    with pytest.raises(ClarificationTimedOut) as exc_info:
        await request_clarification(
            state=state,
            slot_name="x",
            merchant_question="?",
            target_lang="en",
            user_channel_request_fn=user_request,
            merchant_speak_fn=merchant_speak,
            timeout_s=0.05,
            assumed_value="default",
        )

    answer = exc_info.value.fallback_answer
    assert answer == "default"
    assert state.phase == TaskPhase.EXECUTION_ACTIVE
    assert state.uncertain_assumptions[-1].source == "user_timeout"
    assert any(
        "double-check" in s.lower() or "call you back" in s.lower()
        for s in spoken
    ), spoken


async def test_timeout_with_null_assumption_does_not_store_none_string() -> None:
    state = _make_execution_active_state()
    state.merchant_lang = "en"
    spoken: list[str] = []

    async def merchant_speak(text: str) -> None:
        spoken.append(text)

    async def user_request(_slot: str, _question: str, _lang: str) -> str:
        await asyncio.sleep(0.5)
        return ""

    with pytest.raises(ClarificationTimedOut) as exc_info:
        await request_clarification(
            state=state,
            slot_name="unknown_field",
            merchant_question="?",
            target_lang="en",
            user_channel_request_fn=user_request,
            merchant_speak_fn=merchant_speak,
            timeout_s=0.05,
            assumed_value=None,
        )

    answer = exc_info.value.fallback_answer
    assert answer == ""
    assert state.slots["unknown_field"] == ""
    assert state.uncertain_assumptions[-1].source == "user_timeout"
    assert state.uncertain_assumptions[-1].assumed_value is None
    assert any("call you back" in s.lower() for s in spoken), spoken


async def test_user_channel_timeout_uses_assumed_value_path() -> None:
    state = _make_execution_active_state()
    state.merchant_lang = "en"
    spoken: list[str] = []

    async def merchant_speak(text: str) -> None:
        spoken.append(text)

    async def user_request(_slot: str, _question: str, _lang: str) -> str:
        raise asyncio.TimeoutError

    with pytest.raises(ClarificationTimedOut) as exc_info:
        await request_clarification(
            state=state,
            slot_name="missing",
            merchant_question="?",
            target_lang="en",
            user_channel_request_fn=user_request,
            merchant_speak_fn=merchant_speak,
            timeout_s=0.5,
            assumed_value=None,
        )

    answer = exc_info.value.fallback_answer
    assert answer == ""
    assert state.slots["missing"] == ""
    assert state.uncertain_assumptions[-1].source == "user_timeout"
    assert state.uncertain_assumptions[-1].assumed_value is None
    assert any("call you back" in s.lower() for s in spoken), spoken


async def test_clarification_state_transitions_audit_log() -> None:
    """state.audit_log records BOTH transitions:

    - EXECUTION_ACTIVE → NEEDS_CLARIFICATION ('merchant asked unknown field')
    - NEEDS_CLARIFICATION → EXECUTION_ACTIVE ('resumed after clarification')
    """
    state = _make_execution_active_state()
    audit_len_before = len(state.audit_log)

    async def user_fn(slot_name: str, question: str, target_lang: str) -> str:
        return "13800000000"

    async def merchant_speak(text: str) -> None:
        pass

    await request_clarification(
        state=state,
        slot_name="phone",
        merchant_question="电话号码？",
        target_lang="zh",
        user_channel_request_fn=user_fn,
        merchant_speak_fn=merchant_speak,
        timeout_s=5.0,
    )

    new_entries = state.audit_log[audit_len_before:]
    assert len(new_entries) == 2
    out_entry, in_entry = new_entries
    assert out_entry.from_phase == TaskPhase.EXECUTION_ACTIVE
    assert out_entry.to_phase == TaskPhase.NEEDS_CLARIFICATION
    assert "merchant" in out_entry.reason  # 'merchant asked unknown field'
    assert out_entry.evidence.get("slot") == "phone"
    assert in_entry.from_phase == TaskPhase.NEEDS_CLARIFICATION
    assert in_entry.to_phase == TaskPhase.EXECUTION_ACTIVE
    assert "resumed" in in_entry.reason
    assert in_entry.evidence.get("slot") == "phone"


async def test_clarification_dynamic_slot_stored_in_slots_dict() -> None:
    """Any slot_name (including arbitrary strings not in KNOWN_SLOTS) is
    stored in state.slots dict — dynamic schema has no allow/deny list.
    """
    state = _make_execution_active_state()

    async def user_fn(slot_name: str, question: str, target_lang: str) -> str:
        return "whatever"

    async def merchant_speak(text: str) -> None:
        pass

    answer = await request_clarification(
        state=state,
        slot_name="custom_field",
        merchant_question="random?",
        target_lang="zh",
        user_channel_request_fn=user_fn,
        merchant_speak_fn=merchant_speak,
        timeout_s=5.0,
    )

    assert answer == "whatever"
    # Dynamic slot stored in slots dict
    assert state.slots["custom_field"] == "whatever"
    # Clarification recorded in audit log
    assert len(state.pending_clarifications) == 1
    assert state.pending_clarifications[0].field == "custom_field"
    assert state.pending_clarifications[0].answer == "whatever"
    # Phase restored
    assert state.phase == TaskPhase.EXECUTION_ACTIVE
    assert state.merchant_held is False


async def test_clarification_phase_transition_before_merchant_held() -> None:
    """state.transition(NEEDS_CLARIFICATION) is called BEFORE
    state.merchant_held is set to True — verified via call-order recording
    inside the user_channel_request_fn callback.
    """
    state = _make_execution_active_state()

    observed: list[tuple[str, TaskPhase, bool]] = []

    async def user_fn(slot_name: str, question: str, target_lang: str) -> str:
        # When the user callback fires, state should already be
        # NEEDS_CLARIFICATION and merchant_held should be True.
        observed.append(("user_called", state.phase, state.merchant_held))
        return "ok"

    async def merchant_speak(text: str) -> None:
        pass

    await request_clarification(
        state=state,
        slot_name="allergy",
        merchant_question="过敏吗？",
        target_lang="zh",
        user_channel_request_fn=user_fn,
        merchant_speak_fn=merchant_speak,
        timeout_s=5.0,
    )

    # When the user callback was invoked, phase was already NEEDS_CLARIFICATION
    # and merchant_held was already True.
    assert observed[0] == ("user_called", TaskPhase.NEEDS_CLARIFICATION, True)
    # After return, state is restored.
    assert state.phase == TaskPhase.EXECUTION_ACTIVE
    assert state.merchant_held is False


async def test_reactive_holding_third_interrupt_raises_impatience_error() -> None:
    from tests.test_dialogue_merchant_vad import _make_pcm_loud, _make_pcm_silence
    from vocalize.dialogue.clarification import MerchantImpatienceError
    from vocalize.dialogue.reactive_holding import ReactiveHolding

    state = TaskState(
        session_id="s",
        user_task_description="t",
        phase=TaskPhase.EXECUTION_ACTIVE,
    )
    spoken: list[str] = []

    async def merchant_speak(text: str) -> None:
        spoken.append(text)

    answered = asyncio.Event()

    async def user_request(_slot: str, _question: str, _lang: str) -> str:
        await answered.wait()
        return ""

    loud = _make_pcm_loud(700)
    silence = _make_pcm_silence(120)
    audio = _LoopMerchantAudio([loud, silence, loud, silence, loud])
    reactive_holding = ReactiveHolding(
        state=state,
        merchant_speak=merchant_speak,
        lang="zh",
        current_slot="party_size",
        current_question="how many?",
        default_value=4,
    )
    keepalive = _RecordingKeepalive()

    with pytest.raises(MerchantImpatienceError):
        await request_clarification(
            state=state,
            slot_name="party_size",
            merchant_question="how many?",
            target_lang="en",
            user_channel_request_fn=user_request,
            merchant_speak_fn=merchant_speak,
            timeout_s=5.0,
            reactive_holding=reactive_holding,
            keepalive_timer=keepalive,
            merchant_audio_source=audio,
        )

    assert state.clarification_holds_used == 3
    assert len(state.uncertain_assumptions) == 1
    assert state.uncertain_assumptions[0].source == "merchant_impatience"
    assert state.phase == TaskPhase.EXECUTION_ACTIVE
    assert state.merchant_held is False
    assert keepalive.stopped is True
    assert keepalive.reactive_filler_notes == 3


async def test_listener_normal_end_still_waits_for_user_answer() -> None:
    from tests.test_dialogue_merchant_vad import _make_pcm_silence
    from vocalize.dialogue.reactive_holding import ReactiveHolding

    state = _make_execution_active_state()
    keepalive = _RecordingKeepalive()

    async def merchant_speak(_text: str) -> None:
        pass

    async def user_request(_slot: str, _question: str, _lang: str) -> str:
        await asyncio.sleep(0.05)
        return "window seat"

    reactive_holding = ReactiveHolding(
        state=state,
        merchant_speak=merchant_speak,
        lang="zh",
        current_slot="seat",
        current_question="where?",
        default_value=None,
    )
    audio = _LoopMerchantAudio([_make_pcm_silence(30)], park_after=False)

    answer = await request_clarification(
        state=state,
        slot_name="seat",
        merchant_question="where?",
        target_lang="en",
        user_channel_request_fn=user_request,
        merchant_speak_fn=merchant_speak,
        timeout_s=0.5,
        reactive_holding=reactive_holding,
        keepalive_timer=keepalive,
        merchant_audio_source=audio,
    )

    assert answer == "window seat"
    assert state.slots["seat"] == "window seat"
    assert keepalive.stopped is True
    assert state.phase == TaskPhase.EXECUTION_ACTIVE
    assert state.merchant_held is False


async def test_continuous_loud_audio_counts_as_one_interruption() -> None:
    from tests.test_dialogue_merchant_vad import _make_pcm_loud
    from vocalize.dialogue.reactive_holding import ReactiveHolding

    state = _make_execution_active_state()
    keepalive = _RecordingKeepalive()

    async def merchant_speak(_text: str) -> None:
        pass

    async def user_request(_slot: str, _question: str, _lang: str) -> str:
        await asyncio.sleep(0.05)
        return "ok"

    loud = _make_pcm_loud(700)
    reactive_holding = ReactiveHolding(
        state=state,
        merchant_speak=merchant_speak,
        lang="zh",
        current_slot="party_size",
        current_question="how many?",
        default_value=4,
    )

    await request_clarification(
        state=state,
        slot_name="party_size",
        merchant_question="how many?",
        target_lang="en",
        user_channel_request_fn=user_request,
        merchant_speak_fn=merchant_speak,
        timeout_s=0.5,
        reactive_holding=reactive_holding,
        keepalive_timer=keepalive,
        merchant_audio_source=_LoopMerchantAudio([loud, loud, loud]),
    )

    assert state.clarification_holds_used == 1
    assert state.uncertain_assumptions == []
    assert keepalive.reactive_filler_notes == 1


async def test_injected_keepalive_timer_stops_on_success() -> None:
    state = _make_execution_active_state()
    keepalive = _RecordingKeepalive()

    async def user_request(_slot: str, _question: str, _lang: str) -> str:
        await keepalive.started.wait()
        return "none"

    async def merchant_speak(_text: str) -> None:
        pass

    answer = await request_clarification(
        state=state,
        slot_name="allergy",
        merchant_question="any allergies?",
        target_lang="en",
        user_channel_request_fn=user_request,
        merchant_speak_fn=merchant_speak,
        timeout_s=0.5,
        keepalive_timer=keepalive,
    )

    assert answer == "none"
    assert keepalive.stopped is True
    assert state.phase == TaskPhase.EXECUTION_ACTIVE
    assert state.merchant_held is False


async def test_injected_keepalive_timer_stops_on_timeout() -> None:
    state = _make_execution_active_state()
    keepalive = _RecordingKeepalive()
    user_cancelled = asyncio.Event()

    async def user_request(_slot: str, _question: str, _lang: str) -> str:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            user_cancelled.set()
            raise
        raise AssertionError("unreachable")

    async def merchant_speak(_text: str) -> None:
        pass

    with pytest.raises(DialogueOrchestratorError, match="clarification timeout"):
        await request_clarification(
            state=state,
            slot_name="phone",
            merchant_question="phone?",
            target_lang="en",
            user_channel_request_fn=user_request,
            merchant_speak_fn=merchant_speak,
            timeout_s=0.02,
            keepalive_timer=keepalive,
        )

    assert keepalive.stopped is True
    assert user_cancelled.is_set()
    assert state.phase == TaskPhase.EXECUTION_ACTIVE
    assert state.merchant_held is False


# ---------------------------------------------------------------------------
# Keepalive — 12s filler synthesis during long user responses
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_keepalive_synthesizes_filler_during_long_clarification() -> None:
    """Long clarification (>12s) triggers keepalive synthesis."""
    merchant_synthesized: list[str] = []

    async def merchant_speak(text: str) -> None:
        merchant_synthesized.append(text)

    async def slow_user_channel(
        _slot_name: str,
        _question: str,
        _target_lang: str,
    ) -> str:
        await asyncio.sleep(0.05)  # simulate "user typing"
        return "answer"

    state = TaskState(session_id="t", phase=TaskPhase.EXECUTION_ACTIVE)
    state.merchant_lang = "zh"

    await request_clarification(
        state=state,
        slot_name="allergy",
        merchant_question="您有过敏吗",
        target_lang="zh",
        user_channel_request_fn=slow_user_channel,
        merchant_speak_fn=merchant_speak,
        keepalive_interval_s=0.02,
        timeout_s=0.5,
    )

    assert any("稍等" in s for s in merchant_synthesized), (
        f"expected keepalive filler in {merchant_synthesized}"
    )
