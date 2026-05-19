"""dialogue.preflight — preflight outer loop + dial-now phrase short-circuit.

``run_preflight`` is the v1 user-channel single-channel driver. It is
called once by ``DialogueOrchestrator`` before merchant execution, and
drives user-side I/O until one of these conditions is met:

1. ``state.phase != COLLECTING`` — the LLM pushed phase out of ``COLLECTING``
   via a tool call. Two production paths:
   - ``transition_to_calling`` → ``READY_TO_DIAL`` (all H-level slots filled,
     readiness passed);
   - ``finalize_task`` → ``COMPLETED`` / ``FAILED`` (user abandons
     mid-preflight — rare off-dial-path branch).
   ``assess_readiness_to_dial`` only writes ``state.readiness`` and does
   NOT change phase, so readiness passing alone is NOT enough to exit —
   the LLM can keep asking M/L slots before transitioning.
2. ``detect_dial_now`` matches the latest user text — D-11 voice override:
   sets ``readiness.override=True`` and pushes phase to ``READY_TO_DIAL``
   *without* invoking ``drive_turn``.
3. ``turns >= max_turns`` — raises ``DialogueOrchestratorError``.
4. ``user_channel.receive_text`` raises ``EOFError`` (stdin closed /
   STT stream exhausted) — raises ``DialogueOrchestratorError``
   "user channel exhausted".

I/O is decoupled from the user's ``VoicePipeline``: input comes via
``user_channel.receive_text()`` (TextUserChannel reads stdin,
LocalMicUserChannel reads mic+STT) and the LLM round-trip is delegated
to an injected ``drive_turn`` callback supplied by the orchestrator.

Schema is dynamic: ``run_preflight`` reads ``state.slots_schema`` and
``state.critical_slots_missing()`` rather than booking-specific fields.
Prompt rendering for the preflight system prompt lives in
``orchestrator._render_prompt`` (single source of truth).

Dial-now matcher
================

- Globally lower-case + collapse all whitespace on the transcript;
- Take the last ``recent_window_chars`` (default 80) characters for
  substring matching — avoids cross-turn false positives from phrases
  said turns ago;
- 6 D-11 phrases pre-normalized at import time; runtime is pure
  substring comparison.
"""
from __future__ import annotations

import logging
import re
import time
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from vocalize.dialogue.state import (
    DialogueOrchestratorError,
    ReadinessVerdict,
    TaskPhase,
    TaskState,
)

if TYPE_CHECKING:
    from vocalize.dialogue.user_channel import UserChannel

log = logging.getLogger(__name__)


# D-11 voice-override phrase set — both Chinese and English variants.
# When editing this list, ``test_dial_now_phrases`` parametrize 6-row
# contract in tests/test_dialogue_preflight.py must be updated in sync
# (the test file holds a same-named constant as a drift guard).
_DIAL_NOW_PHRASES: list[str] = [
    "立刻拨号",
    "现在打吧",
    "马上打",
    "dial now",
    "call now",
    "skip ahead",
]

# Pre-compute normalized forms: lower-case + strip all whitespace. One-time
# cost at import; runtime is 6 string substring checks — constant overhead.
_DIAL_NOW_NORMALIZED: list[str] = [
    re.sub(r"\s+", "", p.lower()) for p in _DIAL_NOW_PHRASES
]


def detect_dial_now(
    transcript_text: str,
    *,
    recent_window_chars: int = 80,
) -> bool:
    """Match D-11 dial-now phrase; True if hit, False otherwise.

    Matching rules:
    1. Overall ``lower()`` + ``re.sub(r"\\s+", "", ...)`` collapses all whitespace;
    2. Take last ``recent_window_chars`` characters (default 80 ~ one utterance);
    3. Any normalized phrase as substring → True.

    Window anchoring rationale: prevents LLM system summaries from
    re-triggering "dial now" across multiple turns. 80 chars covers one
    typical Chinese response or one short English sentence.
    """
    haystack = re.sub(r"\s+", "", transcript_text.lower())[-recent_window_chars:]
    return any(p in haystack for p in _DIAL_NOW_NORMALIZED)


# ---------------------------------------------------------------------------
# Main preflight loop
# ---------------------------------------------------------------------------


async def run_preflight(
    user_channel: "UserChannel",
    state: TaskState,
    *,
    drive_turn: Callable[[str, str], Awaitable[None]],
    initial_turn: tuple[str, str] | None = None,
    max_turns: int = 20,
) -> ReadinessVerdict:
    """Drive user-side preflight loop until readiness / dial-now / max_turns.

    Args:
        user_channel: ``UserChannel`` — text or mic-backed user I/O.
            ``receive_text()`` provides the next user utterance + lang;
            ``speak_text()`` is invoked indirectly by the ``drive_turn``
            callback (the orchestrator's wiring).
        state: shared ``TaskState`` mutated by the LLM tool dispatch
            inside ``drive_turn``.
        drive_turn: async callable ``(user_text, lang) -> None`` that runs
            one LLM round-trip on the user channel. Provided by the
            orchestrator (``_run_llm_turn`` + ``user_channel.speak_text``).
            Tests inject a recorder.
        initial_turn: optional first ``(text, lang)`` turn that has already
            been received outside the preflight loop. Used by the live WS path
            where the first user text becomes the task description.
        max_turns: outer loop budget; raises ``DialogueOrchestratorError``
            on the (max_turns+1)-th turn.

    Returns:
        ``ReadinessVerdict`` (also written to ``state.readiness``).

    Raises:
        DialogueOrchestratorError: ``max_turns`` exceeded, OR
            ``user_channel`` exhausted (EOFError) before readiness passed.
    """
    turns = 0
    queued_initial_turn = initial_turn
    while True:
        if queued_initial_turn is None:
            try:
                user_text, lang = await user_channel.receive_text()
            except EOFError as exc:
                raise DialogueOrchestratorError(
                    f"preflight user channel exhausted before readiness: {exc}"
                ) from exc
        else:
            user_text, lang = queued_initial_turn
            queued_initial_turn = None

        user_text = user_text.strip()
        if not user_text:
            # receive_text impls already strip + raise on empty, but
            # belt-and-braces: never fire LLM on empty input.
            continue

        # D-11 short-circuit — must precede drive_turn so we don't burn
        # a wasted LLM round-trip on dial-now phrases.
        if detect_dial_now(user_text):
            state.readiness = ReadinessVerdict(
                missing_critical=[],
                confidence=1.0,
                override=True,
                decided_at=time.monotonic(),
            )
            state.transition(
                TaskPhase.READY_TO_DIAL,
                reason="dial-now phrase",
                evidence={"phrase": user_text, "degradation": True},
            )
            log.info(
                "[preflight] dial-now override fired (phrase=%r); "
                "skipping LLM turn",
                user_text,
            )
            return state.readiness

        log.info("[preflight] turn=%d user=%r lang=%s", turns, user_text, lang)
        await drive_turn(user_text, lang)

        # Exit when LLM has driven phase out of COLLECTING.
        # - READY_TO_DIAL → transition_to_calling fired; return the
        #   readiness verdict so orchestrator post-preflight hand-off
        #   can proceed into the merchant loop.
        # - COMPLETED / FAILED → finalize_task fired (the rare "user
        #   abandoned off the dial path" branch per preflight prompt spec).
        #   Surface as DialogueOrchestratorError so the orchestrator's
        #   failure-handling path runs, instead of conflating terminal
        #   exits with readiness-passed and continuing into the merchant
        #   loop. (Codex P1 2026-05-04.)
        if state.phase in (TaskPhase.COMPLETED, TaskPhase.FAILED):
            raise DialogueOrchestratorError(
                f"preflight terminated via finalize_task: "
                f"phase={state.phase.value}"
            )
        if state.phase != TaskPhase.COLLECTING:
            if state.readiness is None:
                # Defensive: LLM transitioned to READY_TO_DIAL without
                # a prior assess_readiness call. Synthesize a passing
                # verdict so downstream code never gets None back.
                state.readiness = ReadinessVerdict(
                    missing_critical=[],
                    confidence=1.0,
                    override=False,
                    decided_at=time.monotonic(),
                )
            log.info(
                "[preflight] phase=%s after turn=%d (readiness conf=%.2f "
                "override=%s) — exiting preflight loop",
                state.phase.value, turns,
                state.readiness.confidence,
                state.readiness.override,
            )
            return state.readiness

        turns += 1
        if turns >= max_turns:
            raise DialogueOrchestratorError("preflight max_turns exceeded")


__all__ = ["detect_dial_now", "run_preflight"]
