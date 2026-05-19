"""dialogue.tools — 6 LLM tool definitions + async dispatch + dual-channel allowlists.

Purpose: Phase 4 dialogue layer LLM-facing contract. Pure data mutation —
``dispatch_tool`` does no I/O, only mutates ``TaskState``; side effects are
returned as a dict so the orchestrator decides next steps (e.g., transition).

Module contract:
- ``TOOLS: dict[str, ToolDef]`` — 6 generic task tools (replaces booking-specific
  tools from Phase 4). Keys are tool names, values are ``ToolDef``.
- ``USER_CHANNEL_TOOLS`` — 5 tools: collect_user_intent /
  assess_readiness_to_dial / transition_to_calling / relay_to_user /
  finalize_task.
- ``MERCHANT_CHANNEL_TOOLS`` — 4 tools: request_user_clarification /
  relay_to_user / finalize_task / collect_user_intent.
- ``async def dispatch_tool(tc, state, *, preceding_message="") -> dict`` —
  branches on ``tc.name``; illegal transitions are caught and returned as
  ``{"ok": False, "error": ...}`` so LLM-driven loops can recover; unknown
  tool names raise ``DialogueOrchestratorError`` (programmer error, not
  LLM behavior).

Design rationale:
- Flat ``if tc.name == "x":`` dispatch — 6 tools is too few to amortize a
  decorator/registry abstraction; flat branches are debuggable in pdb.
- Slot validation via ``SlotDef.expected_type`` instead of hardcoded field
  names — supports runtime-generated task schemas (Layer 1 output).
- ``assess_readiness_to_dial`` drops the old ``_schema_check`` call (no
  fixed schema in the generic model); records LLM-reported missing_critical
  and confidence verbatim.
- ``request_user_clarification`` injects a default filler message in the
  merchant's language if the LLM failed to emit one alongside the tool call.
"""
from __future__ import annotations

import json
import time
from dataclasses import asdict
from datetime import date as _date
from typing import Any

from vocalize.dialogue.state import (
    DialogueOrchestratorError,
    ReadinessVerdict,
    TaskPhase,
    TaskState,
)
from vocalize.llm.base import ToolCall, ToolDef


# ---------------------------------------------------------------------------
# Tool definitions — task-generic replacements for the 6 booking-specific
# tools. JSON Schema draft-7; OpenAI/DeepSeek tools interface standard.
# ---------------------------------------------------------------------------

TOOLS: dict[str, ToolDef] = {
    "collect_user_intent": ToolDef(
        name="collect_user_intent",
        description=(
            "Fill or update a single slot of the TaskState with information "
            "the user just provided. Call this once per slot. Do NOT guess — "
            "only call with values the user explicitly stated. The slot must "
            "be one of those listed in TaskState.slots_schema or "
            "optional_slots_schema."
        ),
        parameters={
            "type": "object",
            "properties": {
                "slot": {
                    "type": "string",
                    "description": "The slot name (must match schema).",
                },
                "value": {
                    "description": "The new value. Type must match expected_type.",
                },
            },
            "required": ["slot", "value"],
        },
    ),
    "assess_readiness_to_dial": ToolDef(
        name="assess_readiness_to_dial",
        description=(
            "Self-assess whether the TaskState has enough information to start "
            "the call. Use the readiness_criteria_text from the task schema as "
            "your judging rubric. Return missing critical slots and a "
            "confidence score."
        ),
        parameters={
            "type": "object",
            "properties": {
                "missing_critical": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Names of H-level slots still missing.",
                },
                "confidence": {
                    "type": "number",
                    "minimum": 0.0,
                    "maximum": 1.0,
                },
                "rationale": {"type": "string"},
            },
            "required": ["missing_critical", "confidence", "rationale"],
        },
    ),
    "transition_to_calling": ToolDef(
        name="transition_to_calling",
        description=(
            "Transition from preflight to actually executing the call. Call "
            "only after readiness has passed AND user has confirmed."
        ),
        parameters={"type": "object", "properties": {}, "required": []},
    ),
    "request_user_clarification": ToolDef(
        name="request_user_clarification",
        description=(
            "The merchant asked something not in TaskState.slots. Suspend the "
            "call (you'll be put on hold) and ask the user. IMPORTANT: before "
            "calling this tool, you MUST emit a non-empty filler message to "
            "the merchant in their language asking them to hold on briefly. "
            "The orchestrator will inject a default filler if you forget, but "
            "your filler is more contextual."
        ),
        parameters={
            "type": "object",
            "properties": {
                "field_name": {
                    "type": "string",
                    "description": "Slot the merchant asked about.",
                },
                "question_text": {
                    "type": "string",
                    "description": "Short question for user, in user_lang. <= 20 words.",
                },
                "target_lang": {"type": "string", "enum": ["zh", "en"]},
                "urgency": {
                    "type": "string",
                    "enum": ["low", "normal", "high"],
                },
            },
            "required": ["field_name", "question_text", "target_lang", "urgency"],
        },
    ),
    "relay_to_user": ToolDef(
        name="relay_to_user",
        description=(
            "Translate merchant text into user's language and push to user "
            "transcript. Translate only — do not add or omit information. "
            "Per task relay_strategy: numbers/dates/names verbatim; "
            "tone may summarize."
        ),
        parameters={
            "type": "object",
            "properties": {
                "text": {"type": "string"},
                "target_lang": {"type": "string", "enum": ["zh", "en"]},
            },
            "required": ["text", "target_lang"],
        },
    ),
    "finalize_task": ToolDef(
        name="finalize_task",
        description=(
            "Mark the task as complete (success=True) or failed (success=False). "
            "Provide a one-sentence summary AND a structured outcomes dict "
            "containing any data the user should know (confirmation numbers, "
            "balances queried, ticket IDs, etc.)."
        ),
        parameters={
            "type": "object",
            "properties": {
                "success": {"type": "boolean"},
                "summary": {"type": "string"},
                "outcomes": {
                    "type": "object",
                    "description": "Structured task results, free-form keys.",
                },
            },
            "required": ["success", "summary", "outcomes"],
        },
    ),
}


# Per-channel allowlists. ``relay_to_user`` lives on BOTH channels (D-15
# requirement): direction is determined by the prompt file selected at the
# call site, not by the tool itself.
USER_CHANNEL_TOOLS: list[ToolDef] = [
    TOOLS["collect_user_intent"],
    TOOLS["assess_readiness_to_dial"],
    TOOLS["transition_to_calling"],
    TOOLS["relay_to_user"],
    TOOLS["finalize_task"],
]

MERCHANT_CHANNEL_TOOLS: list[ToolDef] = [
    TOOLS["request_user_clarification"],
    TOOLS["relay_to_user"],
    TOOLS["finalize_task"],
    TOOLS["collect_user_intent"],
]


# ---------------------------------------------------------------------------
# Dispatcher — name-keyed flat dispatch.
# ---------------------------------------------------------------------------


def _require_args(
    tool_name: str, args: dict, required: tuple[str, ...],
) -> dict | None:
    """Validate that ``required`` keys are all present in ``args``.

    Returns a recoverable ``{ok: False, error: ...}`` dict if a required
    key is missing, or ``None`` if all keys are present. Both
    ``dispatch_tool`` and the orchestrator's ``_dispatch_one_tool``
    special-case branches use this so a single forgetful LLM tool call
    yields a recoverable tool error instead of a ``KeyError`` that
    aborts the session.
    """
    for key in required:
        if key not in args:
            return {
                "ok": False,
                "error": (
                    f"tool {tool_name!r} missing required argument {key!r}"
                ),
            }
    return None


async def dispatch_tool(
    tc: ToolCall,
    state: TaskState,
    *,
    preceding_message: str = "",
) -> dict[str, Any]:
    """Execute one ToolCall against ``state``; return a dict for the LLM.

    ``preceding_message`` is the assistant text emitted alongside the tool
    call (used for filler verification on request_user_clarification).

    Contract:
    - Pure data mutation — no I/O, no logging.
    - Return ``{"ok": True, ...}`` on success, ``{"ok": False, "error": str}``
      for recoverable failures.
    - Raise ``DialogueOrchestratorError`` for unknown tool names (programmer
      error, not LLM behavior).

    Per-tool semantics:
    - ``collect_user_intent``: validates slot name against schema, value type
      against ``SlotDef.expected_type``, writes to ``state.slots[slot]``.
    - ``assess_readiness_to_dial``: records LLM-reported missing_critical and
      confidence as a ``ReadinessVerdict``. No fixed-schema check.
    - ``transition_to_calling``: gated by ``state.readiness.passed``; illegal
      transitions caught and returned as ok=False.
    - ``request_user_clarification``: if no preceding_message from LLM, injects
      a default filler ("好的，请您稍等一下，我确认一下" for zh /
      "One moment please, let me check on that." for en).
    - ``relay_to_user``: echo args verbatim; orchestrator drives cross-channel
      work.
    - ``finalize_task``: transition to COMPLETED (success=True) or FAILED
      (success=False); stores ``outcomes`` dict in evidence.
    """
    # Surface malformed/truncated tool-call JSON as a recoverable error
    # instead of letting ``JSONDecodeError`` bubble up — otherwise the
    # whole session aborts on a single bad tool call, where retrying
    # would let the LLM recover on the next turn.
    try:
        args = json.loads(tc.arguments)
    except json.JSONDecodeError as exc:
        return {
            "ok": False,
            "error": f"tool {tc.name!r} arguments are not valid JSON: {exc}",
        }
    # Valid JSON can still be a non-object (``[]`` / ``null`` / number /
    # string); per-branch ``args.get(...)`` / ``args[...]`` access would
    # raise AttributeError or TypeError. Reject up front.
    if not isinstance(args, dict):
        return {
            "ok": False,
            "error": (
                f"tool {tc.name!r} arguments must be a JSON object; "
                f"got {type(args).__name__}"
            ),
        }

    if tc.name == "collect_user_intent":
        slot = args.get("slot")
        if not isinstance(slot, str):
            return {"ok": False, "error": "missing or invalid 'slot'"}
        if "value" not in args:
            return {"ok": False, "error": f"missing 'value' for slot {slot!r}"}
        value = args["value"]
        all_slots = {s.name: s for s in state.slots_schema + state.optional_slots_schema}
        if slot not in all_slots:
            return {"ok": False, "error": f"slot {slot!r} not in TaskState schema"}
        sdef = all_slots[slot]
        # Reject null / malformed values for every slot type — readiness
        # reconciliation gates on key presence, so unvalidated writes would
        # let H-slots pass with unusable data (P1 guard).
        if value is None:
            return {"ok": False, "error": f"slot {slot!r} value cannot be null"}
        # bool is a subclass of int in Python; explicitly reject so a
        # forgetful LLM cannot pass ``true`` for an H-slot expecting a
        # numeric headcount / amount and pass readiness with garbage.
        if sdef.expected_type == "number" and (
            isinstance(value, bool) or not isinstance(value, (int, float))
        ):
            return {"ok": False, "error": f"slot {slot!r} expects number"}
        if sdef.expected_type == "enum":
            if not sdef.enum_values:
                # Malformed schema: enum slot without enum_values has no
                # contract to enforce, so any write would silently bypass
                # validation. Refuse the write rather than treating the
                # slot as filled.
                return {
                    "ok": False,
                    "error": (
                        f"slot {slot!r} declared enum but schema has no "
                        f"enum_values; cannot validate"
                    ),
                }
            if value not in sdef.enum_values:
                return {
                    "ok": False,
                    "error": f"slot {slot!r} value not in enum {sdef.enum_values}",
                }
        if sdef.expected_type in ("string", "date", "phone"):
            if not isinstance(value, str) or not value.strip():
                return {
                    "ok": False,
                    "error": f"slot {slot!r} expects non-empty {sdef.expected_type}",
                }
        # Format-level validation for date / phone slots. Without this, free
        # text like "next Friday" or "abc" passes readiness because the gate
        # only checks slot presence, leading to dialing with unusable data.
        if sdef.expected_type == "date":
            try:
                _date.fromisoformat(value.strip())
            except ValueError:
                return {
                    "ok": False,
                    "error": (
                        f"slot {slot!r} expects ISO date YYYY-MM-DD; "
                        f"got {value!r}"
                    ),
                }
        if sdef.expected_type == "phone":
            # Phones cover everything from 3-digit emergency lines (911) and
            # 5-digit carrier hotlines (10086) up to international E.164.
            # Bar: must contain at least one digit and no alphabetic
            # characters. This rejects free-text like "abc" while still
            # accepting short hotlines that the task planner explicitly
            # surfaces in its few-shot examples.
            has_digit = any(ch.isdigit() for ch in value)
            has_alpha = any(ch.isalpha() for ch in value)
            if not has_digit or has_alpha:
                return {
                    "ok": False,
                    "error": (
                        f"slot {slot!r} expects a phone number "
                        f"(digits, no letters); got {value!r}"
                    ),
                }
        state.slots[slot] = value
        if slot == "merchant_lang":
            state.merchant_lang = value
        return {"ok": True, "slot": slot, "value": value}

    if tc.name == "assess_readiness_to_dial":
        llm_missing = list(args.get("missing_critical") or [])
        try:
            confidence = float(args.get("confidence", 0.0))
        except (TypeError, ValueError):
            confidence = 0.0
        # Cross-check LLM-reported missing_critical with deterministic schema.
        # If the model missed any H-slot, merge them so transition_to_calling
        # never proceeds with required data missing (P1 guard).
        deterministic_missing = state.critical_slots_missing()
        merged_missing = list(dict.fromkeys(llm_missing + deterministic_missing))
        state.readiness = ReadinessVerdict(
            missing_critical=merged_missing,
            confidence=confidence,
            decided_at=time.monotonic(),
        )
        return {"ok": True, "readiness": asdict(state.readiness)}

    if tc.name == "transition_to_calling":
        if state.readiness is None or not state.readiness.passed:
            return {
                "ok": False,
                "error": (
                    "readiness not passed; collect remaining critical slots "
                    "and run assess_readiness_to_dial first"
                ),
            }
        if state.phase == TaskPhase.READY_TO_DIAL:
            return {"ok": True, "phase": state.phase.value, "noop": True}
        try:
            state.transition(
                TaskPhase.READY_TO_DIAL,
                reason="LLM tool transition_to_calling",
                evidence={"readiness": asdict(state.readiness)},
            )
        except DialogueOrchestratorError as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "phase": state.phase.value}

    if tc.name == "request_user_clarification":
        # Note: in production, the merchant-side ``request_user_clarification``
        # path is intercepted by ``DialogueOrchestrator._dispatch_one_tool``
        # *before* reaching dispatch_tool, so the merchant filler-speak is
        # actually emitted there. This branch still runs in unit tests that
        # invoke ``dispatch_tool`` directly — keep the default-filler logic
        # in sync with the orchestrator's branch.
        missing = _require_args(
            tc.name, args, ("field_name", "question_text", "target_lang", "urgency"),
        )
        if missing is not None:
            return missing
        filler_emitted = bool(preceding_message and preceding_message.strip())
        if not filler_emitted:
            default_filler_zh = "好的，请您稍等一下，我确认一下"
            default_filler_en = "One moment please, let me check on that."
            tgt = args.get("target_lang", "zh")
            mlang = state.merchant_lang or tgt
            fallback_filler = default_filler_zh if mlang == "zh" else default_filler_en
            return {
                "ok": True,
                "field_name": args["field_name"],
                "question_text": args["question_text"],
                "target_lang": args["target_lang"],
                "urgency": args["urgency"],
                "filler_used": fallback_filler,
                "filler_was_default": True,
            }
        return {
            "ok": True,
            "field_name": args["field_name"],
            "question_text": args["question_text"],
            "target_lang": args["target_lang"],
            "urgency": args["urgency"],
            "filler_used": preceding_message.strip(),
            "filler_was_default": False,
        }

    if tc.name == "relay_to_user":
        missing = _require_args(tc.name, args, ("text", "target_lang"))
        if missing is not None:
            return missing
        return {
            "ok": True,
            "text": args["text"],
            "target_lang": args["target_lang"],
        }

    if tc.name == "finalize_task":
        success = bool(args["success"])
        target = TaskPhase.COMPLETED if success else TaskPhase.FAILED
        outcomes = args.get("outcomes", {})
        try:
            state.transition(
                target,
                reason="LLM tool finalize_task",
                evidence={
                    "summary": args.get("summary", ""),
                    "outcomes": outcomes,
                },
            )
        except DialogueOrchestratorError as exc:
            return {"ok": False, "error": str(exc)}
        return {
            "ok": True,
            "phase": state.phase.value,
            "summary": args.get("summary", ""),
            "outcomes": outcomes,
        }

    raise DialogueOrchestratorError(f"unknown tool: {tc.name}")


__all__ = [
    "MERCHANT_CHANNEL_TOOLS",
    "TOOLS",
    "USER_CHANNEL_TOOLS",
    "dispatch_tool",
]
