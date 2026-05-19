"""dialogue.tools — TaskState-aware tool dispatch tests."""
from __future__ import annotations

import json

import pytest

from vocalize.dialogue.state import (
    ReadinessVerdict,
    SlotDef,
    TaskPhase,
    TaskState,
)
from vocalize.dialogue.tools import dispatch_tool
from vocalize.llm.base import ToolCall


def _make_state(slots_schema=None, slots=None) -> TaskState:
    return TaskState(
        session_id="t",
        user_task_description="test",
        task_category="test-category",
        slots_schema=slots_schema or [],
        slots=slots or {},
        phase=TaskPhase.COLLECTING,
    )


def _tc(name: str, args: dict) -> ToolCall:
    return ToolCall(id="tc1", name=name, arguments=json.dumps(args))


@pytest.mark.asyncio
async def test_collect_user_intent_writes_to_slots():
    sch = SlotDef(name="restaurant", description_zh="餐厅", description_en="restaurant", criticality="H", expected_type="string")
    state = _make_state(slots_schema=[sch])
    result = await dispatch_tool(_tc("collect_user_intent", {"slot": "restaurant", "value": "海底捞"}), state)
    assert result["ok"] is True
    assert state.slots["restaurant"] == "海底捞"


@pytest.mark.asyncio
async def test_collect_user_intent_rejects_unknown_slot():
    state = _make_state(slots_schema=[])
    result = await dispatch_tool(_tc("collect_user_intent", {"slot": "ghost", "value": "x"}), state)
    assert result["ok"] is False
    assert "not in" in result["error"]


@pytest.mark.asyncio
async def test_collect_user_intent_validates_enum():
    sch = SlotDef(name="merchant_lang", description_zh="商家语言", description_en="merchant lang", criticality="H", expected_type="enum", enum_values=("zh", "en"))
    state = _make_state(slots_schema=[sch])
    bad = await dispatch_tool(_tc("collect_user_intent", {"slot": "merchant_lang", "value": "fr"}), state)
    assert bad["ok"] is False
    good = await dispatch_tool(_tc("collect_user_intent", {"slot": "merchant_lang", "value": "zh"}), state)
    assert good["ok"] is True
    assert state.merchant_lang == "zh"


@pytest.mark.asyncio
async def test_dispatch_tool_returns_recoverable_error_on_non_object_args():
    """Valid JSON but non-object top-level (``[]``, ``null``, string)
    must surface as ``{ok: False}`` instead of AttributeError."""
    state = _make_state(slots_schema=[])
    for raw in ("[]", "null", "\"hello\"", "42"):
        bad = ToolCall(id="tc1", name="collect_user_intent", arguments=raw)
        result = await dispatch_tool(bad, state)
        assert result["ok"] is False, f"raw={raw!r} should fail"
        assert "JSON object" in result["error"], (
            f"raw={raw!r} expected JSON-object error, got {result['error']!r}"
        )


@pytest.mark.asyncio
async def test_dispatch_tool_returns_recoverable_error_on_malformed_json():
    """Malformed/truncated tool-call arguments must surface as
    ``{ok: False, error: ...}`` so a single bad call doesn't abort the
    session — the LLM can recover on the next turn."""
    state = _make_state(slots_schema=[])
    bad_tc = ToolCall(id="tc1", name="collect_user_intent", arguments='{"slot": "x", "value":')  # truncated
    result = await dispatch_tool(bad_tc, state)
    assert result["ok"] is False
    assert "not valid JSON" in result["error"]


@pytest.mark.asyncio
async def test_collect_user_intent_rejects_enum_slot_without_enum_values():
    """A schema where expected_type='enum' but enum_values is empty has
    no contract to enforce — collect_user_intent must refuse the write
    rather than accept any string."""
    sch = SlotDef(
        name="merchant_lang",
        description_zh="lang", description_en="lang",
        criticality="H", expected_type="enum",
        enum_values=None,  # malformed schema
    )
    state = _make_state(slots_schema=[sch])
    bad = await dispatch_tool(_tc("collect_user_intent", {"slot": "merchant_lang", "value": "anything"}), state)
    assert bad["ok"] is False
    assert "enum_values" in bad["error"]
    assert "merchant_lang" not in state.slots


@pytest.mark.asyncio
async def test_collect_user_intent_validates_date_format():
    """Free-text dates like 'next Friday' must NOT pass — only ISO YYYY-MM-DD."""
    sch = SlotDef(name="booking_date", description_zh="日期", description_en="date", criticality="H", expected_type="date")
    state = _make_state(slots_schema=[sch])

    bad_freetext = await dispatch_tool(_tc("collect_user_intent", {"slot": "booking_date", "value": "next Friday"}), state)
    assert bad_freetext["ok"] is False
    assert "ISO date" in bad_freetext["error"]

    bad_invalid = await dispatch_tool(_tc("collect_user_intent", {"slot": "booking_date", "value": "2026-13-99"}), state)
    assert bad_invalid["ok"] is False

    good = await dispatch_tool(_tc("collect_user_intent", {"slot": "booking_date", "value": "2026-05-04"}), state)
    assert good["ok"] is True
    assert state.slots["booking_date"] == "2026-05-04"


@pytest.mark.asyncio
async def test_collect_user_intent_validates_phone_format():
    """'abc' must NOT pass; valid phone numbers include short hotlines."""
    sch = SlotDef(name="merchant_phone", description_zh="电话", description_en="phone", criticality="H", expected_type="phone")
    state = _make_state(slots_schema=[sch])

    bad_alpha = await dispatch_tool(_tc("collect_user_intent", {"slot": "merchant_phone", "value": "abc-1234567"}), state)
    assert bad_alpha["ok"] is False
    assert "phone" in bad_alpha["error"]

    bad_no_digits = await dispatch_tool(_tc("collect_user_intent", {"slot": "merchant_phone", "value": "()-—"}), state)
    assert bad_no_digits["ok"] is False

    good_us_full = await dispatch_tool(_tc("collect_user_intent", {"slot": "merchant_phone", "value": "(555) 123-4567"}), state)
    assert good_us_full["ok"] is True

    good_cn_full = await dispatch_tool(_tc("collect_user_intent", {"slot": "merchant_phone", "value": "13800001111"}), state)
    assert good_cn_full["ok"] is True

    # Short hotlines must pass — task_planner few-shots use 10086 / 911.
    good_carrier = await dispatch_tool(_tc("collect_user_intent", {"slot": "merchant_phone", "value": "10086"}), state)
    assert good_carrier["ok"] is True
    good_emergency = await dispatch_tool(_tc("collect_user_intent", {"slot": "merchant_phone", "value": "911"}), state)
    assert good_emergency["ok"] is True


@pytest.mark.asyncio
async def test_request_user_clarification_preserves_llm_filler() -> None:
    state = _make_state()
    state.merchant_lang = "zh"
    preceding_message = "麻烦稍等，我跟客户确认一下"
    result = await dispatch_tool(
        _tc("request_user_clarification", {"field_name": "allergy", "question_text": "您有过敏吗？", "target_lang": "zh", "urgency": "normal"}),
        state,
        preceding_message=preceding_message,
    )
    assert result["ok"] is True
    assert result["filler_was_default"] is False
    assert result["filler_used"] == preceding_message


@pytest.mark.asyncio
async def test_request_user_clarification_injects_default_filler_zh() -> None:
    state = _make_state()
    state.merchant_lang = "zh"
    result = await dispatch_tool(
        _tc("request_user_clarification", {"field_name": "allergy", "question_text": "您有过敏吗？", "target_lang": "zh", "urgency": "normal"}),
        state,
        preceding_message="",
    )
    assert result["ok"] is True
    assert result["filler_was_default"] is True
    assert result["filler_used"] == "好的，请您稍等一下，我确认一下"


@pytest.mark.asyncio
async def test_request_user_clarification_injects_default_filler_en() -> None:
    state = _make_state()
    state.merchant_lang = "en"
    result = await dispatch_tool(
        _tc("request_user_clarification", {"field_name": "allergy", "question_text": "Any allergies?", "target_lang": "en", "urgency": "normal"}),
        state,
        preceding_message="",
    )
    assert result["ok"] is True
    assert result["filler_was_default"] is True
    assert result["filler_used"] == "One moment please, let me check on that."


@pytest.mark.asyncio
async def test_finalize_task_completed():
    state = _make_state()
    state.phase = TaskPhase.EXECUTION_ACTIVE
    result = await dispatch_tool(_tc("finalize_task", {"success": True, "summary": "Booking confirmed", "outcomes": {"confirmation_id": "ABC123"}}), state)
    assert result["ok"] is True
    assert state.phase == TaskPhase.COMPLETED
    assert result["outcomes"]["confirmation_id"] == "ABC123"


@pytest.mark.asyncio
async def test_finalize_task_failed():
    state = _make_state()
    state.phase = TaskPhase.EXECUTION_ACTIVE
    await dispatch_tool(_tc("finalize_task", {"success": False, "summary": "Merchant declined", "outcomes": {}}), state)
    assert state.phase == TaskPhase.FAILED


@pytest.mark.asyncio
async def test_transition_to_calling_blocked_until_readiness():
    state = _make_state()
    result = await dispatch_tool(_tc("transition_to_calling", {}), state)
    assert result["ok"] is False
    state.readiness = ReadinessVerdict(missing_critical=[], confidence=0.9)
    result = await dispatch_tool(_tc("transition_to_calling", {}), state)
    assert result["ok"] is True
    assert state.phase == TaskPhase.READY_TO_DIAL


# ---------------------------------------------------------------------------
# assess_readiness_to_dial — deterministic-vs-LLM merge guard
# (post-merge audit gap)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_assess_readiness_merges_deterministic_missing_slots():
    """If the LLM's self-assessment claims no slots are missing, but the
    schema says an H slot is empty, the deterministic check must still
    flag it — otherwise the LLM could vote itself ready and dial with
    required data missing."""
    h_slot = SlotDef(
        name="restaurant_phone",
        description_zh="phone", description_en="phone",
        criticality="H", expected_type="phone",
    )
    state = _make_state(slots_schema=[h_slot])
    # No slots filled — deterministic check finds restaurant_phone missing.

    # LLM claims missing_critical is empty (over-confident).
    result = await dispatch_tool(
        _tc("assess_readiness_to_dial", {
            "missing_critical": [],
            "confidence": 0.95,
            "rationale": "looks good",
        }),
        state,
    )
    assert result["ok"] is True
    # Merged list must include the slot the LLM forgot.
    assert "restaurant_phone" in state.readiness.missing_critical
    # And readiness must NOT pass.
    assert state.readiness.passed is False


@pytest.mark.asyncio
async def test_assess_readiness_keeps_llm_missing_when_deterministic_clean():
    """If the LLM reports a missing slot the schema doesn't know about
    (e.g. business-rule miss), it should still be preserved in the merged
    list rather than being dropped by the deterministic check."""
    h_slot = SlotDef(
        name="merchant_lang",
        description_zh="lang", description_en="lang",
        criticality="H", expected_type="enum",
        enum_values=("zh", "en"),
    )
    state = _make_state(slots_schema=[h_slot])
    state.slots["merchant_lang"] = "en"
    # Deterministic check sees no missing H slots.

    result = await dispatch_tool(
        _tc("assess_readiness_to_dial", {
            "missing_critical": ["unspecified_business_constraint"],
            "confidence": 0.6,
            "rationale": "still need user buy-in",
        }),
        state,
    )
    assert result["ok"] is True
    assert "unspecified_business_constraint" in state.readiness.missing_critical
    assert state.readiness.passed is False  # has missing OR low confidence


@pytest.mark.asyncio
async def test_assess_readiness_passes_when_truly_ready():
    """Both LLM and deterministic agree on no missing → passed=True
    when confidence >= 0.7."""
    h_slot = SlotDef(
        name="merchant_lang",
        description_zh="lang", description_en="lang",
        criticality="H", expected_type="enum",
        enum_values=("zh", "en"),
    )
    state = _make_state(slots_schema=[h_slot])
    state.slots["merchant_lang"] = "en"

    result = await dispatch_tool(
        _tc("assess_readiness_to_dial", {
            "missing_critical": [],
            "confidence": 0.9,
            "rationale": "all set",
        }),
        state,
    )
    assert result["ok"] is True
    assert state.readiness.missing_critical == []
    assert state.readiness.passed is True
