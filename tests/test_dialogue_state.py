"""dialogue.state tests — BookingState, state machine, schema readiness.

Wave 0 scaffolding promoted to Wave 2 implementation tests (per
04-VALIDATION.md "Wave 0 Requirements" + 04-05 PLAN). The fakes
(FakeTransport / FakeSTT / FakeLLM / FakeTTS) live in
tests/test_pipeline.py:33-128 — these tests do not need them since
state.py is pure data + transition logic.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

import pytest

# Module-level production import — fails today, will resolve after Wave 2.
# Wrap so collection succeeds without ImportError; individual tests still
# pytest.skip() so they show up in the report as skipped (not errored).
try:
    from vocalize.dialogue.state import (  # noqa: F401
        LEGAL_TASK_TRANSITIONS,
        LEGAL_TRANSITIONS,
        BookingAuditEntry,
        BookingPhase,
        BookingState,
        CallSegment,
        CallbackEntry,
        ClarificationItem,
        DialogueOrchestratorError,
        ReadinessVerdict,
        SlotAssumption,
        SlotDef,
        TaskAuditEntry,
        TaskPhase,
        TaskState,
        TranscriptMessage,
        _schema_check,
    )

    _DIALOGUE_STATE_AVAILABLE = True
except ImportError:
    _DIALOGUE_STATE_AVAILABLE = False
    pytestmark = pytest.mark.skip(
        reason="awaits Wave 2 implementation of vocalize.dialogue.state"
    )


def _make_state(**overrides: Any) -> Any:
    """Mirror test_pipeline.py:122-128 _td/_fin helper style for BookingState."""
    state = BookingState()  # type: ignore[name-defined]
    for k, v in overrides.items():
        setattr(state, k, v)
    return state


# ---------------------------------------------------------------------------
# Schema readiness — D-10 stage 1
# ---------------------------------------------------------------------------
def test_readiness_schema_blocks_when_headcount_missing() -> None:
    """headcount=None / 0 / -1 → 'headcount' in missing_critical."""
    for bad in (None, 0, -1):
        state = _make_state(
            restaurant_name="海底捞",
            date="2026-05-10",
            time="19:00",
            headcount=bad,
        )
        missing = _schema_check(state)
        assert "headcount" in missing, f"headcount={bad!r} should be flagged"


def test_readiness_schema_blocks_when_date_invalid() -> None:
    """date='not-a-date' → 'date' in missing_critical."""
    for bad in ("not-a-date", "2026-13-99", "", None):
        state = _make_state(
            restaurant_name="海底捞",
            date=bad,
            time="19:00",
            headcount=4,
        )
        missing = _schema_check(state)
        assert "date" in missing, f"date={bad!r} should be flagged"


def test_readiness_schema_blocks_when_time_invalid() -> None:
    """time='25:99' → 'time' in missing_critical."""
    for bad in ("25:99", "abc", "", None, "9:00"):
        state = _make_state(
            restaurant_name="海底捞",
            date="2026-05-10",
            time=bad,
            headcount=4,
        )
        missing = _schema_check(state)
        assert "time" in missing, f"time={bad!r} should be flagged"


def test_readiness_schema_passes_when_all_critical_set() -> None:
    """Full state with valid restaurant/date/time/headcount → missing == []."""
    state = _make_state(
        restaurant_name="海底捞",
        date="2026-05-10",
        time="19:00",
        headcount=4,
    )
    assert _schema_check(state) == []


def test_readiness_schema() -> None:
    """Alias of test_readiness_schema_passes_when_all_critical_set so the
    exact pytest node ID 'tests/test_dialogue_state.py::test_readiness_schema'
    referenced from 04-VALIDATION.md resolves verbatim.
    """
    test_readiness_schema_passes_when_all_critical_set()


def test_readiness_schema_blocks_empty_restaurant() -> None:
    """restaurant_name='' or None → 'restaurant_name' in missing_critical."""
    for bad in (None, ""):
        state = _make_state(
            restaurant_name=bad,
            date="2026-05-10",
            time="19:00",
            headcount=4,
        )
        missing = _schema_check(state)
        assert "restaurant_name" in missing, f"restaurant_name={bad!r} should be flagged"


# ---------------------------------------------------------------------------
# State machine transitions
# ---------------------------------------------------------------------------
def test_state_machine_legal_transition_collecting_to_ready() -> None:
    """COLLECTING → READY_TO_DIAL is in LEGAL_TRANSITIONS; transition() must succeed."""
    state = BookingState()
    assert state.phase is BookingPhase.COLLECTING
    state.transition(
        BookingPhase.READY_TO_DIAL,
        reason="schema check passed",
        evidence={"missing_critical": [], "confidence": 0.82},
    )
    assert state.phase is BookingPhase.READY_TO_DIAL
    assert len(state.audit_log) == 1


def test_state_machine_illegal_transition_raises() -> None:
    """COLLECTING → IN_CALL skips DIALING; must raise DialogueOrchestratorError."""
    state = BookingState()
    with pytest.raises(DialogueOrchestratorError) as exc:
        state.transition(BookingPhase.IN_CALL, reason="boom")
    assert "illegal transition" in str(exc.value)
    # phase must NOT have changed on failure
    assert state.phase is BookingPhase.COLLECTING
    # audit log must NOT have an entry for the failed attempt
    assert state.audit_log == []


def test_state_machine_audit_log_appended() -> None:
    """Each transition appends a BookingAuditEntry with reason+evidence."""
    state = BookingState()
    state.transition(
        BookingPhase.READY_TO_DIAL,
        reason="schema ok",
        evidence={"missing_critical": []},
    )
    state.transition(
        BookingPhase.DIALING,
        reason="user confirmed",
        evidence={"confirm": True},
    )
    assert len(state.audit_log) == 2
    e0, e1 = state.audit_log
    assert isinstance(e0, BookingAuditEntry)
    assert e0.from_phase is BookingPhase.COLLECTING
    assert e0.to_phase is BookingPhase.READY_TO_DIAL
    assert e0.reason == "schema ok"
    assert e0.evidence == {"missing_critical": []}
    assert e0.timestamp > 0.0
    assert e1.from_phase is BookingPhase.READY_TO_DIAL
    assert e1.to_phase is BookingPhase.DIALING


def test_state_machine_terminal_states_have_empty_legal_set() -> None:
    """COMPLETED + FAILED are terminal — LEGAL_TRANSITIONS maps to empty set."""
    assert LEGAL_TRANSITIONS[BookingPhase.COMPLETED] == set()
    assert LEGAL_TRANSITIONS[BookingPhase.FAILED] == set()


def test_state_machine_has_seven_phases() -> None:
    """BookingPhase enum has exactly 7 members per refactor-plan L374."""
    assert len(BookingPhase) == 7
    expected = {
        "collecting",
        "ready_to_dial",
        "dialing",
        "in_call",
        "needs_clarification",
        "completed",
        "failed",
    }
    assert {p.value for p in BookingPhase} == expected


# ---------------------------------------------------------------------------
# ReadinessVerdict.passed semantics — D-10 / D-11
# ---------------------------------------------------------------------------
def test_readiness_verdict_passed_when_override_true() -> None:
    """override=True forces passed=True regardless of slots/confidence."""
    v = ReadinessVerdict(missing_critical=["restaurant_name"], confidence=0.0, override=True)
    assert v.passed is True


def test_readiness_verdict_passed_requires_confidence_ge_0_7() -> None:
    """confidence=0.69 with no override + no missing → passed=False;
    confidence=0.7 → passed=True."""
    v_below = ReadinessVerdict(missing_critical=[], confidence=0.69)
    assert v_below.passed is False
    v_at = ReadinessVerdict(missing_critical=[], confidence=0.7)
    assert v_at.passed is True
    v_above = ReadinessVerdict(missing_critical=[], confidence=0.99)
    assert v_above.passed is True


def test_readiness_verdict_blocked_when_missing_present() -> None:
    """Missing critical slots → passed=False even with high confidence
    (unless override)."""
    v = ReadinessVerdict(missing_critical=["headcount"], confidence=0.99)
    assert v.passed is False


# ---------------------------------------------------------------------------
# BookingState defaults
# ---------------------------------------------------------------------------
def test_booking_state_defaults() -> None:
    """Default-constructed BookingState matches refactor-plan L368-407 contract."""
    s = BookingState()
    # slots
    assert s.restaurant_name is None
    assert s.date is None
    assert s.time is None
    assert s.headcount is None
    assert s.phone is None
    assert s.special_requirements is None
    # languages
    assert s.user_lang is None
    assert s.merchant_lang is None
    # state machine
    assert s.phase is BookingPhase.COLLECTING
    assert s.audit_log == []
    # mid-call protocol
    assert s.pending_clarifications == []
    assert s.merchant_held is False
    assert s.readiness is None


def test_clarification_item_dataclass() -> None:
    """ClarificationItem has the expected fields per CONTEXT D-09."""
    item = ClarificationItem(field="phone", question="phone?", answer=None, ts=1.0)
    assert item.field == "phone"
    assert item.question == "phone?"
    assert item.answer is None
    assert item.ts == 1.0


# ---------------------------------------------------------------------------
# v1 Core Engine — SlotDef / TaskPhase / LEGAL_TASK_TRANSITIONS
# ---------------------------------------------------------------------------
def test_slot_def_is_frozen() -> None:
    s = SlotDef(
        name="restaurant",
        description_zh="餐厅名称",
        description_en="restaurant name",
        criticality="H",
        expected_type="string",
    )
    with pytest.raises(AttributeError):
        s.name = "other"


def test_slot_def_enum_values_optional() -> None:
    s = SlotDef(
        name="cuisine",
        description_zh="菜系",
        description_en="cuisine",
        criticality="M",
        expected_type="enum",
        enum_values=("sichuan", "cantonese", "northern"),
    )
    assert s.enum_values == ("sichuan", "cantonese", "northern")


def test_legal_task_transitions_terminal_states() -> None:
    assert LEGAL_TASK_TRANSITIONS[TaskPhase.COMPLETED] == set()
    assert LEGAL_TASK_TRANSITIONS[TaskPhase.FAILED] == set()


def test_legal_task_transitions_collecting_to_ready() -> None:
    assert TaskPhase.READY_TO_DIAL in LEGAL_TASK_TRANSITIONS[TaskPhase.COLLECTING]


def test_legal_task_transitions_no_skip_collecting() -> None:
    # Cannot go DRAFT → COLLECTING without TASK_PLANNING
    assert TaskPhase.COLLECTING not in LEGAL_TASK_TRANSITIONS[TaskPhase.DRAFT]


def test_taskphase_has_v1rc_phases() -> None:
    assert TaskPhase.AWAIT_USER_CLARIFICATION.value == "await_user_clarification"
    assert TaskPhase.POST_CALL_REVIEW.value == "post_call_review"
    assert TaskPhase.CALLBACK_ACTIVE.value == "callback_active"


def test_legal_transitions_for_v1rc_phases() -> None:
    assert TaskPhase.POST_CALL_REVIEW in LEGAL_TASK_TRANSITIONS[TaskPhase.EXECUTION_ACTIVE]
    assert TaskPhase.AWAIT_USER_CLARIFICATION in LEGAL_TASK_TRANSITIONS[TaskPhase.EXECUTION_ACTIVE]
    assert TaskPhase.EXECUTION_ACTIVE in LEGAL_TASK_TRANSITIONS[TaskPhase.AWAIT_USER_CLARIFICATION]
    assert TaskPhase.POST_CALL_REVIEW in LEGAL_TASK_TRANSITIONS[TaskPhase.AWAIT_USER_CLARIFICATION]
    assert TaskPhase.CALLBACK_ACTIVE in LEGAL_TASK_TRANSITIONS[TaskPhase.POST_CALL_REVIEW]
    assert TaskPhase.COMPLETED in LEGAL_TASK_TRANSITIONS[TaskPhase.POST_CALL_REVIEW]
    assert TaskPhase.POST_CALL_REVIEW in LEGAL_TASK_TRANSITIONS[TaskPhase.CALLBACK_ACTIVE]
    assert LEGAL_TASK_TRANSITIONS[TaskPhase.COMPLETED] == set()
    assert LEGAL_TASK_TRANSITIONS[TaskPhase.FAILED] == set()


def test_transition_into_post_call_review_is_legal() -> None:
    state = TaskState(
        session_id="s",
        user_task_description="t",
        phase=TaskPhase.EXECUTION_ACTIVE,
    )
    state.transition(TaskPhase.POST_CALL_REVIEW, reason="hangup")
    assert state.phase == TaskPhase.POST_CALL_REVIEW


def test_illegal_transition_from_terminal_phase_raises() -> None:
    state = TaskState(
        session_id="s",
        user_task_description="t",
        phase=TaskPhase.COMPLETED,
    )
    with pytest.raises(DialogueOrchestratorError):
        state.transition(TaskPhase.POST_CALL_REVIEW, reason="invalid back-edge")


# ---------------------------------------------------------------------------
# v1 Core Engine — TaskState
# ---------------------------------------------------------------------------
def test_task_state_default_phase():
    ts = TaskState(session_id="s1")
    assert ts.phase == TaskPhase.DRAFT
    assert ts.slots == {}
    assert ts.preferred_voice_id is None
    assert ts.mode == "phone"


def test_taskstate_v1rc_fields_have_safe_defaults() -> None:
    state = TaskState(session_id="s", user_task_description="t")
    assert state.auto_translate_merchant is True
    assert state.uncertain_assumptions == []
    assert state.pending_callbacks == []
    assert state.clarification_holds_used == 0
    assert state.user_takeover_active is False


def test_taskstate_v1rc_fields_are_independent_per_instance() -> None:
    a = TaskState(session_id="a", user_task_description="x")
    b = TaskState(session_id="b", user_task_description="y")
    a.uncertain_assumptions.append("placeholder")  # type: ignore[arg-type]
    assert b.uncertain_assumptions == []


def test_taskstate_record_uncertain_assumption_appends_and_returns_id() -> None:
    state = TaskState(session_id="s", user_task_description="t")
    sa = state.record_uncertain_assumption(
        slot="party_size",
        question="What size is your group?",
        assumed_value=4,
        source="user_timeout",
    )
    assert sa.id
    assert sa.slot == "party_size"
    assert sa.status == "pending_review"
    assert state.uncertain_assumptions == [sa]


def test_taskstate_confirm_assumption_correct_marks_confirmed() -> None:
    state = TaskState(session_id="s", user_task_description="t")
    sa = state.record_uncertain_assumption(
        slot="x", question="?", assumed_value=1, source="user_timeout",
    )
    state.confirm_assumption(sa.id, choice="correct", correction=None)
    assert state.uncertain_assumptions[0].status == "confirmed"
    assert state.pending_callbacks == []


def test_taskstate_confirm_assumption_wrong_creates_pending_callback() -> None:
    state = TaskState(session_id="s", user_task_description="t")
    sa = state.record_uncertain_assumption(
        slot="x", question="?", assumed_value=1, source="user_timeout",
    )
    cb = state.confirm_assumption(sa.id, choice="wrong", correction="2")
    assert cb is not None
    assert state.uncertain_assumptions[0].status == "corrected"
    assert state.uncertain_assumptions[0].correction == "2"
    assert state.uncertain_assumptions[0].callback_id == cb.id
    assert state.pending_callbacks == [cb]
    assert cb.assumption_id == sa.id


def test_taskstate_confirm_assumption_wrong_reuses_open_callback() -> None:
    state = TaskState(session_id="s", user_task_description="t")
    sa = state.record_uncertain_assumption(
        slot="x", question="?", assumed_value=1, source="user_timeout",
    )
    cb = state.confirm_assumption(sa.id, choice="wrong", correction="2")
    again = state.confirm_assumption(sa.id, choice="wrong", correction="3")

    assert again is cb
    assert len(state.pending_callbacks) == 1
    assert cb.correction == "3"
    assert state.uncertain_assumptions[0].callback_id == cb.id


def test_taskstate_confirm_assumption_unknown_id_raises() -> None:
    state = TaskState(session_id="s", user_task_description="t")
    with pytest.raises(KeyError):
        state.confirm_assumption("missing", choice="correct", correction=None)


def test_taskstate_confirm_assumption_carries_note_to_callback() -> None:
    state = TaskState(session_id="s", user_task_description="t")
    sa = state.record_uncertain_assumption(
        slot="x", question="?", assumed_value=1, source="user_timeout",
    )
    cb = state.confirm_assumption(
        sa.id, choice="wrong", correction="2", note="user added context",
    )
    assert cb is not None
    assert cb.note == "user added context"
    assert state.uncertain_assumptions[0].note == "user added context"


def test_taskstate_find_assumption_by_id_returns_match_or_none() -> None:
    state = TaskState(session_id="s", user_task_description="t")
    sa = state.record_uncertain_assumption(
        slot="x", question="?", assumed_value=1, source="user_timeout",
    )
    assert state.find_assumption_by_id(sa.id) is sa
    assert state.find_assumption_by_id("nope") is None


def test_taskstate_set_user_takeover_toggles_active_flag() -> None:
    state = TaskState(session_id="s", user_task_description="t")

    state.set_user_takeover(active=True)
    assert state.user_takeover_active is True

    state.set_user_takeover(active=False)
    assert state.user_takeover_active is False


def test_taskstate_reset_clarification_holds_zeros_counter() -> None:
    state = TaskState(session_id="s", user_task_description="t")
    state.clarification_holds_used = 2
    state.reset_clarification_holds()
    assert state.clarification_holds_used == 0


def test_call_segment_lifecycle() -> None:
    state = TaskState(session_id="s", user_task_description="t")

    first = state.start_call_segment()
    second = state.start_call_segment()

    assert first.index == 1
    assert first.started_at.tzinfo is timezone.utc
    assert first.ended_at is None
    assert first.interrupted is False
    assert second.index == 2
    assert state.call_segments == [first, second]


def test_end_current_segment_marks_interrupted() -> None:
    state = TaskState(session_id="s", user_task_description="t")
    state.start_call_segment()

    state.end_current_segment(interrupted=True, reason="ws_close")

    segment = state.call_segments[-1]
    assert segment.ended_at is not None
    assert segment.ended_at.tzinfo is timezone.utc
    assert segment.interrupted is True
    assert segment.interrupt_reason == "ws_close"


def test_mark_current_segment_interrupted_is_idempotent_on_already_ended() -> None:
    state = TaskState(session_id="s", user_task_description="t")
    state.start_call_segment()

    state.mark_current_segment_interrupted(reason="ws_close")
    first_ended_at = state.call_segments[-1].ended_at
    state.mark_current_segment_interrupted(reason="user_hangup")

    segment = state.call_segments[-1]
    assert segment.ended_at == first_ended_at
    assert segment.interrupted is True
    assert segment.interrupt_reason == "ws_close"


def test_needs_clarification_to_post_call_review_is_legal() -> None:
    state = TaskState(
        session_id="s",
        user_task_description="t",
        phase=TaskPhase.NEEDS_CLARIFICATION,
    )

    state.transition(TaskPhase.POST_CALL_REVIEW, reason="ws disconnect during clarification")

    assert state.phase is TaskPhase.POST_CALL_REVIEW


def test_call_segment_added_frame_round_trip() -> None:
    from vocalize.server.frames import CallSegmentAddedFrame, serialize_server_frame

    segment = CallSegment.new(
        index=1,
        started_at=datetime.now(timezone.utc),
    )

    raw = serialize_server_frame(
        CallSegmentAddedFrame(segment=segment.model_dump(mode="json"))
    )
    payload = json.loads(raw)

    assert payload["type"] == "call_segment_added"
    assert payload["segment"]["id"] == segment.id
    assert payload["segment"]["index"] == 1
    assert payload["segment"]["ended_at"] is None
    assert payload["segment"]["interrupted"] is False


def test_segment_interrupted_frame_round_trip() -> None:
    from vocalize.server.frames import SegmentInterruptedFrame, serialize_server_frame

    raw = serialize_server_frame(
        SegmentInterruptedFrame(segment_id="seg-1", reason="ws_close")
    )
    payload = json.loads(raw)

    assert payload == {
        "type": "segment_interrupted",
        "segment_id": "seg-1",
        "reason": "ws_close",
    }


def test_task_state_transition_legal():
    ts = TaskState(session_id="s1")
    ts.transition(TaskPhase.TASK_PLANNING, reason="user submitted task")
    assert ts.phase == TaskPhase.TASK_PLANNING
    assert len(ts.audit_log) == 1
    assert ts.audit_log[0].from_phase == TaskPhase.DRAFT
    assert ts.audit_log[0].to_phase == TaskPhase.TASK_PLANNING


def test_task_state_transition_illegal_raises():
    ts = TaskState(session_id="s1")
    with pytest.raises(DialogueOrchestratorError) as exc:
        ts.transition(TaskPhase.EXECUTION_ACTIVE, reason="skip ahead")
    assert "draft → execution_active" in str(exc.value)


def test_task_state_critical_slots_missing():
    ts = TaskState(
        session_id="s1",
        slots_schema=[
            SlotDef(name="a", description_zh="A", description_en="A", criticality="H", expected_type="string"),
            SlotDef(name="b", description_zh="B", description_en="B", criticality="H", expected_type="string"),
            SlotDef(name="c", description_zh="C", description_en="C", criticality="M", expected_type="string"),
        ],
        slots={"a": "filled"},
    )
    assert ts.critical_slots_missing() == ["b"]


# ---------------------------------------------------------------------------
# Language routing — vocalize.dialogue.language (D-09 + D-15)
#
# These tests are gated on language.py importability separately from state.py
# so a partial implementation surfaces clearly.
# ---------------------------------------------------------------------------
try:
    from vocalize.dialogue.language import (  # noqa: F401
        Lang,
        detect_user_lang,
        is_cross_lingual,
    )

    _DIALOGUE_LANGUAGE_AVAILABLE = True
except ImportError:
    _DIALOGUE_LANGUAGE_AVAILABLE = False


_skip_lang = pytest.mark.skipif(
    not _DIALOGUE_LANGUAGE_AVAILABLE,
    reason="awaits Wave 2 implementation of vocalize.dialogue.language",
)


@_skip_lang
def test_detect_user_lang_zh_variants() -> None:
    """'zh' and 'zh-CN' both map to 'zh' (startswith match)."""
    assert detect_user_lang("zh") == "zh"
    assert detect_user_lang("zh-CN") == "zh"


@_skip_lang
def test_detect_user_lang_en_variants() -> None:
    """'en' and 'en-US' both map to 'en'."""
    assert detect_user_lang("en") == "en"
    assert detect_user_lang("en-US") == "en"


@_skip_lang
def test_detect_user_lang_default_on_none() -> None:
    """None → default; default override works."""
    assert detect_user_lang(None) == "zh"
    assert detect_user_lang(None, default="en") == "en"
    # Unknown language falls back to default
    assert detect_user_lang("ja") == "zh"
    assert detect_user_lang("ja", default="en") == "en"


@_skip_lang

@_skip_lang

@_skip_lang
def test_is_cross_lingual() -> None:
    """user_lang != merchant_lang → True; equal → False."""
    assert is_cross_lingual("zh", "en") is True
    assert is_cross_lingual("en", "zh") is True
    assert is_cross_lingual("zh", "zh") is False
    assert is_cross_lingual("en", "en") is False


# ---------------------------------------------------------------------------
# Phase 4 Plan 06: dialogue.tools — TOOLS dict + dispatch_tool + per-channel
# allowlists. Gated separately so a partial Wave 2 surfaces clearly.
# ---------------------------------------------------------------------------
try:
    from vocalize.dialogue.tools import (  # noqa: F401
        MERCHANT_CHANNEL_TOOLS,
        TOOLS,
        USER_CHANNEL_TOOLS,
        dispatch_tool,
    )
    from vocalize.llm.base import ToolCall, ToolDef  # noqa: F401

    _DIALOGUE_TOOLS_AVAILABLE = True
except ImportError:
    _DIALOGUE_TOOLS_AVAILABLE = False


_skip_tools = pytest.mark.skipif(
    not _DIALOGUE_TOOLS_AVAILABLE,
    reason="awaits Wave 2 implementation of vocalize.dialogue.tools",
)


def _tc(name: str, **arguments: Any) -> Any:
    """Build a ToolCall whose ``arguments`` is the JSON of the kwargs."""
    return ToolCall(id=f"call_{name}", name=name, arguments=json.dumps(arguments))







# ---------------------------------------------------------------------------
# Phase 4 Plan 06: dialogue.prompts — load_prompt + 10 .md files +
# D-08 booking-domain framing + D-14 isolation + D-15 facts-only enforcement.
# ---------------------------------------------------------------------------
try:
    from vocalize.dialogue.prompts import load_prompt  # noqa: F401

    _DIALOGUE_PROMPTS_AVAILABLE = True
except ImportError:
    _DIALOGUE_PROMPTS_AVAILABLE = False


_skip_prompts = pytest.mark.skipif(
    not _DIALOGUE_PROMPTS_AVAILABLE,
    reason="awaits Wave 2 implementation of vocalize.dialogue.prompts",
)


@_skip_prompts
def test_load_prompt_unknown_raises() -> None:
    """Loading a non-existent prompt raises FileNotFoundError."""
    with pytest.raises(FileNotFoundError):
        load_prompt("nonexistent_prompt_file")


def test_slot_assumption_constructs_with_required_fields() -> None:
    sa = SlotAssumption(
        id="a-1",
        slot="party_size",
        question="What size is your group?",
        assumed_value=4,
        source="user_timeout",
        created_at=datetime(2026, 5, 7, tzinfo=timezone.utc),
        status="pending_review",
        correction=None,
        note=None,
        callback_id=None,
    )
    assert sa.slot == "party_size"
    assert sa.assumed_value == 4
    assert sa.status == "pending_review"


def test_slot_assumption_serializes_json_friendly_values() -> None:
    sa = SlotAssumption(
        id="a-1",
        slot="party_size",
        question="What size is your group?",
        assumed_value=4,
        source="user_timeout",
        created_at=datetime(2026, 5, 7, tzinfo=timezone.utc),
    )
    dumped = sa.model_dump(mode="json")
    assert dumped["id"] == "a-1"
    assert dumped["slot"] == "party_size"
    assert dumped["created_at"] == "2026-05-07T00:00:00Z"


def test_slot_assumption_rejects_invalid_source() -> None:
    import pydantic
    with pytest.raises(pydantic.ValidationError):
        SlotAssumption(
            id="a-2",
            slot="x",
            question="?",
            assumed_value=None,
            source="invalid_source",  # not in Literal
            created_at=datetime.now(timezone.utc),
            status="pending_review",
            correction=None,
            note=None,
            callback_id=None,
        )


def test_callback_entry_status_transitions_are_typed() -> None:
    ce = CallbackEntry(
        id="cb-1",
        assumption_id="a-1",
        correction="actually 6",
        note=None,
        status="queued",
        created_at=datetime.now(timezone.utc),
        started_at=None,
        completed_at=None,
        transcript_segment_id=None,
    )
    assert ce.status == "queued"


def test_transcript_message_supports_translation_link() -> None:
    tm = TranscriptMessage(
        id="t-1",
        role="ai_to_merchant",
        text="Hello",
        lang="en",
        is_final=True,
        subtype="translation",
        parent_id="t-0",
        segment_id=None,
        created_at=datetime.now(timezone.utc),
    )
    assert tm.parent_id == "t-0"
    assert tm.subtype == "translation"
