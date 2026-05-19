"""dialogue.task_planner — Layer 1 unit tests.

Real LLM tests live in tests/test_task_planner_quality.py (Task 18).
This file is for module-surface and parsing-logic tests with mocked LLM.
"""
from __future__ import annotations

import json

import pytest

from vocalize.dialogue.task_planner import (
    TASK_PLANNER_TOOL_SCHEMA,
    TaskPlannerError,
    TaskSchema,
    generate_task_schema,
)
from vocalize.llm.base import FinishChunk, ToolCallDelta


def test_task_schema_dataclass_fields():
    """TaskSchema has the public fields downstream layers depend on."""
    s = TaskSchema(
        task_category="x",
        slots_schema=[],
        conversation_goals=[],
        readiness_criteria_text="",
        relay_strategy="",
    )
    assert s.optional_slots_schema == []
    assert s.refused is False
    assert s.reasoning == ""


def test_tool_schema_required_fields():
    """The tool schema requires all fields downstream layers consume."""
    required = set(TASK_PLANNER_TOOL_SCHEMA["required"])
    assert "task_category" in required
    assert "slots_schema" in required
    assert "conversation_goals" in required
    assert "readiness_criteria_text" in required
    assert "relay_strategy" in required


@pytest.mark.asyncio
async def test_generate_task_schema_parses_booking_response():
    """Mock LLM returns valid booking JSON via ToolCallDelta + FinishChunk."""
    booking_json = json.dumps({
        "task_category": "restaurant-booking",
        "slots_schema": [
            {"name": "merchant_lang", "description_zh": "商家语言", "description_en": "merchant lang",
             "criticality": "H", "expected_type": "enum", "enum_values": ["zh", "en"]},
            {"name": "restaurant_branch", "description_zh": "分店", "description_en": "branch",
             "criticality": "H", "expected_type": "string"},
        ],
        "conversation_goals": ["确认空位"],
        "readiness_criteria_text": "all H slots filled",
        "relay_strategy": "verbatim numbers",
        "reasoning": "ok",
    })

    class FakeLLM:
        async def stream_chat(self, messages, tools=None):
            yield ToolCallDelta(
                tool_call_index=0,
                tool_call_id="tc1",
                name="emit_task_schema",
                arguments_delta=booking_json,
            )
            yield FinishChunk(reason="tool_calls")

    schema = await generate_task_schema("帮我订海底捞", user_lang="zh", llm=FakeLLM())
    assert schema.task_category == "restaurant-booking"
    assert len(schema.slots_schema) == 2
    assert schema.slots_schema[0].name == "merchant_lang"
    assert schema.slots_schema[0].enum_values == ("zh", "en")
    assert schema.refused is False


@pytest.mark.asyncio
async def test_generate_task_schema_handles_refused():
    """Mock LLM returns refused; assert refused=True."""
    class RefusingLLM:
        async def stream_chat(self, messages, tools=None):
            yield ToolCallDelta(
                tool_call_index=0,
                tool_call_id="tc1",
                name="emit_task_schema",
                arguments_delta=json.dumps({
                    "task_category": "refused",
                    "slots_schema": [],
                    "conversation_goals": [],
                    "readiness_criteria_text": "",
                    "relay_strategy": "",
                    "reasoning": "harassment task",
                }),
            )
            yield FinishChunk(reason="tool_calls")

    schema = await generate_task_schema("Help me harass my ex", user_lang="en", llm=RefusingLLM())
    assert schema.refused is True
    assert schema.task_category == "refused"
    assert "harassment" in schema.reasoning


@pytest.mark.asyncio
async def test_generate_task_schema_raises_on_no_tool_call():
    """LLM stops without calling tool; assert TaskPlannerError."""
    class NoToolLLM:
        async def stream_chat(self, messages, tools=None):
            yield FinishChunk(reason="stop")

    with pytest.raises(TaskPlannerError, match="did not call"):
        await generate_task_schema("test", user_lang="zh", llm=NoToolLLM())


@pytest.mark.asyncio
async def test_generate_task_schema_raises_on_non_object_payload():
    """LLM returns valid JSON whose top level is a list / string / number;
    must surface as TaskPlannerError, not AttributeError."""
    class ListPayloadLLM:
        async def stream_chat(self, messages, tools=None):
            yield ToolCallDelta(
                tool_call_index=0,
                tool_call_id="tc1",
                name="emit_task_schema",
                arguments_delta="[]",
            )
            yield FinishChunk(reason="tool_calls")

    with pytest.raises(TaskPlannerError, match="JSON object"):
        await generate_task_schema("test", user_lang="zh", llm=ListPayloadLLM())


@pytest.mark.asyncio
async def test_generate_task_schema_raises_on_invalid_json():
    """LLM returns invalid JSON; assert TaskPlannerError."""
    class BadJSONLLM:
        async def stream_chat(self, messages, tools=None):
            yield ToolCallDelta(
                tool_call_index=0,
                tool_call_id="tc1",
                name="emit_task_schema",
                arguments_delta="{not json",
            )
            yield FinishChunk(reason="tool_calls")

    with pytest.raises(TaskPlannerError, match="not JSON"):
        await generate_task_schema("test", user_lang="zh", llm=BadJSONLLM())


# ---------------------------------------------------------------------------
# merchant_lang slot contract validation (P2 guard)
# ---------------------------------------------------------------------------


def _emit_schema(payload: dict) -> "type":
    """Build a one-shot fake LLM that emits ``payload`` via emit_task_schema."""
    body = json.dumps(payload)

    class FakeLLM:
        async def stream_chat(self, messages, tools=None):
            yield ToolCallDelta(
                tool_call_index=0,
                tool_call_id="tc1",
                name="emit_task_schema",
                arguments_delta=body,
            )
            yield FinishChunk(reason="tool_calls")

    return FakeLLM


@pytest.mark.asyncio
async def test_generate_task_schema_rejects_first_slot_not_merchant_lang():
    """First H-slot must be ``merchant_lang``; otherwise TaskPlannerError."""
    payload = {
        "task_category": "x",
        "slots_schema": [
            {"name": "headcount", "description_zh": "人数",
             "description_en": "party size", "criticality": "H",
             "expected_type": "number"},
        ],
        "conversation_goals": [],
        "readiness_criteria_text": "x",
        "relay_strategy": "x",
    }
    with pytest.raises(TaskPlannerError, match="merchant_lang"):
        await generate_task_schema(
            "test", user_lang="en", llm=_emit_schema(payload)(),
        )


@pytest.mark.asyncio
async def test_generate_task_schema_rejects_merchant_lang_wrong_type():
    """``merchant_lang`` must have expected_type='enum'; reject string etc."""
    payload = {
        "task_category": "x",
        "slots_schema": [
            {"name": "merchant_lang", "description_zh": "语言",
             "description_en": "lang", "criticality": "H",
             "expected_type": "string"},
        ],
        "conversation_goals": [],
        "readiness_criteria_text": "x",
        "relay_strategy": "x",
    }
    with pytest.raises(TaskPlannerError, match="enum"):
        await generate_task_schema(
            "test", user_lang="en", llm=_emit_schema(payload)(),
        )


@pytest.mark.asyncio
async def test_generate_task_schema_raises_on_missing_required_key():
    """Missing top-level required key surfaces as TaskPlannerError, not KeyError."""
    payload = {
        # Missing task_category
        "slots_schema": [
            {"name": "merchant_lang", "description_zh": "lang", "description_en": "lang",
             "criticality": "H", "expected_type": "enum", "enum_values": ["zh", "en"]},
        ],
        "conversation_goals": [],
        "readiness_criteria_text": "x",
        "relay_strategy": "x",
    }
    with pytest.raises(TaskPlannerError, match="missing required key"):
        await generate_task_schema(
            "test", user_lang="en", llm=_emit_schema(payload)(),
        )


@pytest.mark.asyncio
async def test_generate_task_schema_raises_on_string_conversation_goals():
    """conversation_goals as a bare string must surface as TaskPlannerError,
    not silently char-explode via ``list(...)``."""
    payload = {
        "task_category": "x",
        "slots_schema": [
            {"name": "merchant_lang", "description_zh": "lang", "description_en": "lang",
             "criticality": "H", "expected_type": "enum", "enum_values": ["zh", "en"]},
        ],
        "conversation_goals": "this should be a list",  # WRONG type
        "readiness_criteria_text": "x",
        "relay_strategy": "x",
    }
    with pytest.raises(TaskPlannerError, match="conversation_goals"):
        await generate_task_schema(
            "test", user_lang="en", llm=_emit_schema(payload)(),
        )


@pytest.mark.asyncio
async def test_generate_task_schema_rejects_non_dict_slot_elements():
    """If slots_schema element isn't a dict (e.g. list of strings),
    raise TaskPlannerError instead of AttributeError."""
    payload = {
        "task_category": "x",
        "slots_schema": ["merchant_lang", "phone"],  # WRONG: strings not dicts
        "conversation_goals": [],
        "readiness_criteria_text": "x",
        "relay_strategy": "x",
    }
    with pytest.raises(TaskPlannerError, match="list of objects"):
        await generate_task_schema(
            "test", user_lang="en", llm=_emit_schema(payload)(),
        )


@pytest.mark.asyncio
async def test_generate_task_schema_rejects_invalid_criticality():
    """A typo'd lowercase 'h' must surface as TaskPlannerError, not pass
    silently as a non-blocking slot (which would let readiness skip it)."""
    payload = {
        "task_category": "x",
        "slots_schema": [
            {"name": "merchant_lang", "description_zh": "lang", "description_en": "lang",
             "criticality": "h",  # WRONG — must be uppercase H
             "expected_type": "enum", "enum_values": ["zh", "en"]},
        ],
        "conversation_goals": [],
        "readiness_criteria_text": "x",
        "relay_strategy": "x",
    }
    with pytest.raises(TaskPlannerError, match="criticality"):
        await generate_task_schema(
            "test", user_lang="en", llm=_emit_schema(payload)(),
        )


@pytest.mark.asyncio
async def test_generate_task_schema_rejects_M_in_slots_schema():
    """slots_schema must contain only H slots — an M slot misfiled there
    would be silently treated as optional by critical_slots_missing()."""
    payload = {
        "task_category": "x",
        "slots_schema": [
            {"name": "merchant_lang", "description_zh": "lang", "description_en": "lang",
             "criticality": "H", "expected_type": "enum", "enum_values": ["zh", "en"]},
            {"name": "headcount", "description_zh": "size", "description_en": "size",
             "criticality": "M",  # WRONG — must be H in slots_schema
             "expected_type": "number"},
        ],
        "conversation_goals": [],
        "readiness_criteria_text": "x",
        "relay_strategy": "x",
    }
    with pytest.raises(TaskPlannerError, match="not allowed in this list"):
        await generate_task_schema(
            "test", user_lang="en", llm=_emit_schema(payload)(),
        )


@pytest.mark.asyncio
async def test_generate_task_schema_rejects_H_in_optional_slots_schema():
    """optional_slots_schema must contain only M/L slots — an H slot
    misfiled there is genuinely required data hidden behind 'optional',
    a real readiness-bypass risk."""
    payload = {
        "task_category": "x",
        "slots_schema": [
            {"name": "merchant_lang", "description_zh": "lang", "description_en": "lang",
             "criticality": "H", "expected_type": "enum", "enum_values": ["zh", "en"]},
        ],
        "optional_slots_schema": [
            {"name": "phone", "description_zh": "phone", "description_en": "phone",
             "criticality": "H",  # WRONG — must be M or L
             "expected_type": "phone"},
        ],
        "conversation_goals": [],
        "readiness_criteria_text": "x",
        "relay_strategy": "x",
    }
    with pytest.raises(TaskPlannerError, match="not allowed in this list"):
        await generate_task_schema(
            "test", user_lang="en", llm=_emit_schema(payload)(),
        )


@pytest.mark.asyncio
async def test_generate_task_schema_rejects_unknown_expected_type():
    """Unknown expected_type ('url' etc.) must surface as TaskPlannerError,
    not slip past dispatch_tool's per-type validation."""
    payload = {
        "task_category": "x",
        "slots_schema": [
            {"name": "merchant_lang", "description_zh": "lang", "description_en": "lang",
             "criticality": "H", "expected_type": "enum", "enum_values": ["zh", "en"]},
            {"name": "homepage", "description_zh": "url", "description_en": "url",
             "criticality": "H",
             "expected_type": "url"},  # WRONG — not in the Literal set
        ],
        "conversation_goals": [],
        "readiness_criteria_text": "x",
        "relay_strategy": "x",
    }
    with pytest.raises(TaskPlannerError, match="expected_type"):
        await generate_task_schema(
            "test", user_lang="en", llm=_emit_schema(payload)(),
        )


@pytest.mark.asyncio
async def test_generate_task_schema_rejects_non_dict_optional_slot_elements():
    """optional_slots_schema must satisfy the same shape contract as
    slots_schema; structural drift surfaces as TaskPlannerError."""
    payload = {
        "task_category": "x",
        "slots_schema": [
            {"name": "merchant_lang", "description_zh": "lang", "description_en": "lang",
             "criticality": "H", "expected_type": "enum", "enum_values": ["zh", "en"]},
        ],
        "optional_slots_schema": "not-a-list",  # WRONG type
        "conversation_goals": [],
        "readiness_criteria_text": "x",
        "relay_strategy": "x",
    }
    with pytest.raises(TaskPlannerError, match="optional_slots_schema"):
        await generate_task_schema(
            "test", user_lang="en", llm=_emit_schema(payload)(),
        )


@pytest.mark.asyncio
async def test_generate_task_schema_picks_lowest_tool_call_index():
    """Streaming tool-call deltas can interleave; the planner must select
    the *lowest* tool_call_index, not whichever index appears first."""
    booking_payload = {
        "task_category": "restaurant-booking",
        "slots_schema": [
            {"name": "merchant_lang", "description_zh": "lang", "description_en": "lang",
             "criticality": "H", "expected_type": "enum", "enum_values": ["zh", "en"]},
        ],
        "conversation_goals": [],
        "readiness_criteria_text": "x",
        "relay_strategy": "x",
    }
    decoy_payload = {"task_category": "decoy", "slots_schema": [], "conversation_goals": [], "readiness_criteria_text": "", "relay_strategy": ""}

    class InterleavedLLM:
        async def stream_chat(self, messages, tools=None):
            # Index 1 chunk arrives before index 0 chunk (interleaved order).
            yield ToolCallDelta(
                tool_call_index=1, tool_call_id="tc1",
                name="emit_task_schema", arguments_delta=json.dumps(decoy_payload),
            )
            yield ToolCallDelta(
                tool_call_index=0, tool_call_id="tc0",
                name="emit_task_schema", arguments_delta=json.dumps(booking_payload),
            )
            yield FinishChunk(reason="tool_calls")

    schema = await generate_task_schema("test", user_lang="en", llm=InterleavedLLM())
    # Lowest index (0) wins — task_category should be the booking one,
    # not the decoy that arrived first in the stream.
    assert schema.task_category == "restaurant-booking"


@pytest.mark.asyncio
async def test_generate_task_schema_rejects_merchant_lang_wrong_enum_values():
    """``merchant_lang`` enum_values must be exactly {'zh','en'}; reject others."""
    payload = {
        "task_category": "x",
        "slots_schema": [
            {"name": "merchant_lang", "description_zh": "语言",
             "description_en": "lang", "criticality": "H",
             "expected_type": "enum", "enum_values": ["zh", "en", "es"]},
        ],
        "conversation_goals": [],
        "readiness_criteria_text": "x",
        "relay_strategy": "x",
    }
    with pytest.raises(TaskPlannerError, match="enum_values"):
        await generate_task_schema(
            "test", user_lang="en", llm=_emit_schema(payload)(),
        )


# ---------------------------------------------------------------------------
# Test-bypass branch: VOCALIZE_TEST_BYPASS_TASK_PLANNER=1
# ---------------------------------------------------------------------------


class _ExplodingLLM:
    """LLM stub that fails the test if invoked. Confirms test-bypass short-circuits."""

    async def stream_chat(self, messages, tools=None):  # type: ignore[no-untyped-def]
        raise AssertionError(
            "LLM must not be invoked when VOCALIZE_TEST_BYPASS_TASK_PLANNER=1"
        )
        yield  # pragma: no cover


@pytest.mark.asyncio
async def test_test_frames_bypass_returns_zero_slot_schema_without_calling_llm(
    monkeypatch,
):
    """VOCALIZE_TEST_BYPASS_TASK_PLANNER=1 returns synthesized zero-slot schema; no LLM call."""
    monkeypatch.setenv("VOCALIZE_TEST_BYPASS_TASK_PLANNER", "1")
    # ENABLE_TEST_FRAMES alone must NOT trigger the bypass; harness sets that.
    monkeypatch.setenv("VOCALIZE_ENABLE_TEST_FRAMES", "1")
    schema = await generate_task_schema(
        "Call the bank and ask the current balance",
        user_lang="en",
        llm=_ExplodingLLM(),  # raises if invoked
    )
    assert schema.task_category == "test_bypass"
    assert schema.slots_schema == []
    assert schema.optional_slots_schema == []
    assert schema.refused is False
    assert "VOCALIZE_TEST_BYPASS_TASK_PLANNER" in schema.reasoning


@pytest.mark.asyncio
async def test_test_frames_bypass_disabled_by_default(monkeypatch):
    """Without VOCALIZE_TEST_BYPASS_TASK_PLANNER, the LLM IS invoked (no bypass)."""
    monkeypatch.delenv("VOCALIZE_TEST_BYPASS_TASK_PLANNER", raising=False)
    # Critical: ENABLE_TEST_FRAMES alone (as set by Phase 3 harness) must NOT bypass.
    monkeypatch.setenv("VOCALIZE_ENABLE_TEST_FRAMES", "1")
    payload = {
        "task_category": "from_llm_path",
        "slots_schema": [
            {"name": "merchant_lang", "description_zh": "语言",
             "description_en": "lang", "criticality": "H",
             "expected_type": "enum", "enum_values": ["zh", "en"]},
        ],
        "conversation_goals": ["g"],
        "readiness_criteria_text": "r",
        "relay_strategy": "s",
    }
    schema = await generate_task_schema(
        "test", user_lang="en", llm=_emit_schema(payload)(),
    )
    # If bypass leaked through we'd see "test_bypass"; we should see the LLM's value.
    assert schema.task_category == "from_llm_path"


@pytest.mark.asyncio
async def test_test_frames_bypass_explicit_zero_does_not_trigger(monkeypatch):
    """VOCALIZE_TEST_BYPASS_TASK_PLANNER=0 (not "1") does not trigger the bypass."""
    monkeypatch.setenv("VOCALIZE_TEST_BYPASS_TASK_PLANNER", "0")
    payload = {
        "task_category": "from_llm_path",
        "slots_schema": [
            {"name": "merchant_lang", "description_zh": "语言",
             "description_en": "lang", "criticality": "H",
             "expected_type": "enum", "enum_values": ["zh", "en"]},
        ],
        "conversation_goals": ["g"],
        "readiness_criteria_text": "r",
        "relay_strategy": "s",
    }
    schema = await generate_task_schema(
        "test", user_lang="en", llm=_emit_schema(payload)(),
    )
    assert schema.task_category == "from_llm_path"
