"""dialogue.task_planner — Layer 1 of v1 5-layer prompt architecture.

Takes the user's natural-language task description and returns a structured
``TaskSchema`` describing what slots to collect, what conversation goals to
hit, and what relay strategy to use. Single LLM call wrapped in OpenAI
tool-call structured-output mode.

Contract:
- Pure async function. No I/O beyond the LLM call.
- Returns ``TaskSchema`` on success.
- Raises ``TaskPlannerError`` on (a) LLM tool-call returns non-JSON,
  (b) JSON fails schema validation, (c) task_category == 'refused'
  (caller decides UX).
- Caller is expected to retry once on transient parse errors before
  surfacing to user.

Design: see spec §6.1 (Layer 1 design) and §6.2 (Layer 1 constraints).
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from typing import Literal

from vocalize.dialogue.state import SlotDef
from vocalize.llm.base import LLMService

log = logging.getLogger(__name__)


class TaskPlannerError(RuntimeError):
    """Raised when the task planner cannot produce a valid schema."""


@dataclass
class TaskSchema:
    """Layer 1 output: complete instructions for the dialogue layers.

    All fields populated by the LLM via the structured tool-call. ``refused``
    is set when the LLM judges the task as off-limits (harassment, illegal,
    impersonation); in that case ``slots_schema`` is empty and ``reasoning``
    explains.
    """

    task_category: str
    slots_schema: list[SlotDef]
    optional_slots_schema: list[SlotDef] = field(default_factory=list)
    conversation_goals: list[str] = field(default_factory=list)
    merchant_etiquette_notes: str = ""
    readiness_criteria_text: str = ""
    relay_strategy: str = ""
    refused: bool = False
    reasoning: str = ""


# Tool spec consumed by openai_compat client. Forces structured output.
TASK_PLANNER_TOOL_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "task_category": {
            "type": "string",
            "description": (
                "Free-form short identifier for this task type, e.g. "
                "'restaurant-booking', 'customer-service-billing-inquiry', "
                "'appointment-medical-checkup'. Use 'refused' to reject the task."
            ),
        },
        "slots_schema": {
            "type": "array",
            "description": "Critical (H-level) information that must be collected before dialing.",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "description_zh": {"type": "string"},
                    "description_en": {"type": "string"},
                    "criticality": {"type": "string", "enum": ["H"]},
                    "expected_type": {
                        "type": "string",
                        "enum": ["string", "number", "date", "phone", "enum"],
                    },
                    "enum_values": {"type": "array", "items": {"type": "string"}},
                    "validation_hint": {"type": "string"},
                },
                "required": [
                    "name",
                    "description_zh",
                    "description_en",
                    "criticality",
                    "expected_type",
                ],
            },
        },
        "optional_slots_schema": {
            "type": "array",
            "description": "Optional information (M or L criticality).",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "description_zh": {"type": "string"},
                    "description_en": {"type": "string"},
                    "criticality": {"type": "string", "enum": ["M", "L"]},
                    "expected_type": {
                        "type": "string",
                        "enum": ["string", "number", "date", "phone", "enum"],
                    },
                    "enum_values": {"type": "array", "items": {"type": "string"}},
                    "validation_hint": {"type": "string"},
                },
                "required": [
                    "name",
                    "description_zh",
                    "description_en",
                    "criticality",
                    "expected_type",
                ],
            },
        },
        "conversation_goals": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Concrete goals the merchant call must achieve.",
        },
        "merchant_etiquette_notes": {
            "type": "string",
            "description": "Phone etiquette specific to this task category.",
        },
        "readiness_criteria_text": {
            "type": "string",
            "description": "Plain-language description of when preflight is done.",
        },
        "relay_strategy": {
            "type": "string",
            "description": "How aggressively to translate verbatim vs summarize.",
        },
        "reasoning": {
            "type": "string",
            "description": "One sentence explaining the schema choices.",
        },
    },
    "required": [
        "task_category",
        "slots_schema",
        "conversation_goals",
        "readiness_criteria_text",
        "relay_strategy",
        "reasoning",
    ],
}


async def generate_task_schema(
    user_task: str,
    *,
    user_lang: Literal["zh", "en"],
    llm: LLMService,
) -> TaskSchema:
    """Run Layer 1: NL task → TaskSchema. Single LLM call.

    Args:
        user_task: User's free-form task description (any language).
        user_lang: Used to select prompt file (zh or en variant).
        llm: An ``LLMService`` instance configured with a model that supports
             structured tool output (DeepSeek-V3 default; OpenAI works).

    Returns:
        TaskSchema ready for downstream Layer 2/3/4 templates.

    Raises:
        TaskPlannerError on parse failure or refusal.
    """
    # Test-bypass: when VOCALIZE_TEST_BYPASS_TASK_PLANNER=1, return a zero-slot
    # schema so the live-Pi stability driver can reach READY_TO_DIAL without
    # depending on LLM judgment. This is a DIFFERENT gate from
    # VOCALIZE_ENABLE_TEST_FRAMES (which only authorizes merchant_text_inject)
    # — the Phase 3 harness sets ENABLE_TEST_FRAMES but expects the scripted
    # LLM to drive task_planner, so we must NOT short-circuit there.
    # The Pi 24h driver sets BOTH env vars; Phase 3 CI sets only the first.
    if os.getenv("VOCALIZE_TEST_BYPASS_TASK_PLANNER") == "1":
        log.warning(
            "task_planner: returning test-bypass zero-slot schema "
            "(VOCALIZE_TEST_BYPASS_TASK_PLANNER=1); user_task=%r",
            user_task[:80],
        )
        return TaskSchema(
            task_category="test_bypass",
            slots_schema=[],
            optional_slots_schema=[],
            conversation_goals=[
                "complete the merchant dialogue using test-injected frames",
            ],
            merchant_etiquette_notes="",
            readiness_criteria_text="Always ready (test-bypass mode)",
            relay_strategy="",
            refused=False,
            reasoning=(
                "Test bypass — schema synthesized for "
                "VOCALIZE_TEST_BYPASS_TASK_PLANNER=1; LLM not called."
            ),
        )

    from pathlib import Path

    from vocalize.llm.base import ChatMessage, FinishChunk, ToolCallDelta, ToolDef

    # Load Layer 1 prompt
    prompt_dir = Path(__file__).parent / "prompts"
    prompt_file = prompt_dir / f"task_planner_{user_lang}.md"
    system_prompt = prompt_file.read_text(encoding="utf-8")

    tool = ToolDef(
        name="emit_task_schema",
        description="Emit the task schema. ALWAYS call this once with full schema.",
        parameters=TASK_PLANNER_TOOL_SCHEMA,
    )

    messages = [
        ChatMessage(role="system", content=system_prompt),
        ChatMessage(role="user", content=user_task),
    ]

    # Accumulate tool call deltas by index
    deltas_by_index: dict[int, dict] = {}
    final_tool_call = None

    async for chunk in llm.stream_chat(messages, tools=[tool]):
        if isinstance(chunk, ToolCallDelta):
            idx = chunk.tool_call_index
            if idx not in deltas_by_index:
                deltas_by_index[idx] = {
                    "id": chunk.tool_call_id,
                    "name": chunk.name or "",
                    "arguments_parts": [],
                }
            entry = deltas_by_index[idx]
            if chunk.tool_call_id is not None:
                entry["id"] = chunk.tool_call_id
            if chunk.name is not None:
                entry["name"] = chunk.name
            entry["arguments_parts"].append(chunk.arguments_delta)
        elif isinstance(chunk, FinishChunk):
            if chunk.reason == "tool_calls" and deltas_by_index:
                # Assemble by lowest tool_call_index so streaming-order
                # interleaving cannot select a non-zero tool call when
                # the model is supposed to emit exactly one.
                if len(deltas_by_index) > 1:
                    log.warning(
                        "task_planner LLM emitted %d tool calls; using "
                        "index=%d (lowest) — extras dropped silently",
                        len(deltas_by_index), min(deltas_by_index.keys()),
                    )
                first = deltas_by_index[min(deltas_by_index.keys())]
                full_args = "".join(first["arguments_parts"])
                final_tool_call = (first["id"], first["name"], full_args)
                break

    if final_tool_call is None:
        raise TaskPlannerError("LLM did not call emit_task_schema tool")

    _, _, raw_args = final_tool_call

    try:
        payload = json.loads(raw_args)
    except json.JSONDecodeError as exc:
        raise TaskPlannerError(f"tool_call arguments not JSON: {exc}") from exc

    # JSON top-level must be an object — a list / string / number is valid
    # JSON but cannot satisfy the emit_task_schema contract. Without this
    # check, ``payload.get(...)`` raises AttributeError and callers cannot
    # classify the failure as a planner parse error.
    if not isinstance(payload, dict):
        raise TaskPlannerError(
            f"tool_call arguments must be a JSON object; got {type(payload).__name__}"
        )

    if payload.get("task_category") == "refused":
        return TaskSchema(
            task_category="refused",
            slots_schema=[],
            conversation_goals=[],
            readiness_criteria_text="",
            relay_strategy="",
            refused=True,
            reasoning=payload.get("reasoning", ""),
        )

    # Validate merchant_lang slot contract: first H-slot must be merchant_lang
    # with expected_type="enum" and enum_values=["zh", "en"] (P2 guard).
    slots = payload.get("slots_schema", [])
    if not isinstance(slots, list) or not all(
        isinstance(s, dict) for s in slots
    ):
        raise TaskPlannerError(
            f"slots_schema must be a list of objects; got {slots!r}"
        )
    optional = payload.get("optional_slots_schema", [])
    if not isinstance(optional, list) or not all(
        isinstance(s, dict) for s in optional
    ):
        raise TaskPlannerError(
            f"optional_slots_schema must be a list of objects; got {optional!r}"
        )
    if not slots or slots[0].get("name") != "merchant_lang":
        raise TaskPlannerError(
            "slots_schema must start with 'merchant_lang' slot; "
            f"got {slots[0].get('name', '<missing>') if slots else '<empty>'}"
        )
    mlang = slots[0]
    if mlang.get("expected_type") != "enum" or set(mlang.get("enum_values", [])) != {"zh", "en"}:
        raise TaskPlannerError(
            "merchant_lang must be expected_type='enum' with enum_values=['zh','en']; "
            f"got type={mlang.get('expected_type')!r} values={mlang.get('enum_values')!r}"
        )

    valid_expected_type = {"string", "number", "date", "phone", "enum"}

    def _slot(d: dict, *, allowed_criticality: set[str]) -> SlotDef:
        try:
            crit = d["criticality"]
            etype = d["expected_type"]
            ev = d.get("enum_values")
            # Validate criticality against the *source list*, not the
            # global {H, M, L}. ``slots_schema`` must contain only H
            # (readiness-blocking); ``optional_slots_schema`` must
            # contain only M / L. Otherwise an H slot misfiled into
            # ``optional_slots_schema`` (or M/L into ``slots_schema``)
            # makes ``state.critical_slots_missing()`` — which only
            # walks ``slots_schema`` looking for H — report wrong data,
            # and dialing can proceed with required slots empty.
            if crit not in allowed_criticality:
                raise TaskPlannerError(
                    f"slot {d.get('name')!r} criticality {crit!r} not "
                    f"allowed in this list "
                    f"(expected one of {sorted(allowed_criticality)})"
                )
            if etype not in valid_expected_type:
                raise TaskPlannerError(
                    f"slot {d.get('name')!r} expected_type {etype!r} not in "
                    f"{sorted(valid_expected_type)}"
                )
            return SlotDef(
                name=d["name"],
                description_zh=d["description_zh"],
                description_en=d["description_en"],
                criticality=crit,
                expected_type=etype,
                enum_values=tuple(ev) if ev else None,
                validation_hint=d.get("validation_hint", ""),
            )
        except KeyError as exc:
            raise TaskPlannerError(
                f"slot definition missing required key {exc!s}: {d!r}"
            ) from exc

    # Required top-level keys: surface as TaskPlannerError instead of raw
    # KeyError so callers can classify shape failures and retry.
    for required in ("task_category", "readiness_criteria_text", "relay_strategy"):
        if required not in payload:
            raise TaskPlannerError(
                f"task-planner payload missing required key {required!r}"
            )

    # conversation_goals must be a list of strings — silently coercing a
    # string via ``list(...)`` would explode it character-by-character and
    # corrupt downstream prompts.
    goals_raw = payload.get("conversation_goals", [])
    if not isinstance(goals_raw, list) or not all(
        isinstance(g, str) for g in goals_raw
    ):
        raise TaskPlannerError(
            f"conversation_goals must be a list of strings; got {goals_raw!r}"
        )

    return TaskSchema(
        task_category=payload["task_category"],
        slots_schema=[
            _slot(d, allowed_criticality={"H"})
            for d in payload.get("slots_schema", [])
        ],
        optional_slots_schema=[
            _slot(d, allowed_criticality={"M", "L"}) for d in optional
        ],
        conversation_goals=list(goals_raw),
        merchant_etiquette_notes=payload.get("merchant_etiquette_notes", ""),
        readiness_criteria_text=payload["readiness_criteria_text"],
        relay_strategy=payload["relay_strategy"],
        refused=False,
        reasoning=payload.get("reasoning", ""),
    )


__all__ = [
    "TaskPlannerError",
    "TaskSchema",
    "TASK_PLANNER_TOOL_SCHEMA",
    "generate_task_schema",
]
