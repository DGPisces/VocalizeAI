"""Reactive holding and merchant-impatience escalation."""
from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any, Literal

from vocalize.dialogue.prompts import load_prompt
from vocalize.dialogue.state import TaskState

log = logging.getLogger(__name__)


class ReactiveHolding:
    """Track merchant interruption count for one clarification cycle."""

    def __init__(
        self,
        *,
        state: TaskState,
        merchant_speak: Callable[[str], Awaitable[None]],
        lang: Literal["zh", "en"],
        current_slot: str,
        current_question: str,
        default_value: Any,
        on_keepalive_reset: Callable[[], None] | None = None,
        emit_filler: Callable[[str], Awaitable[None]] | None = None,
    ) -> None:
        self._state = state
        self._merchant_speak = merchant_speak
        self._lang = lang
        self._current_slot = current_slot
        self._current_question = current_question
        self._default_value = default_value
        self._on_keepalive_reset = on_keepalive_reset
        self._emit_filler = emit_filler
        self.escalated = False

    def start_cycle(self) -> None:
        self._state.reset_clarification_holds()

    async def on_interruption(self) -> None:
        if self.escalated:
            return

        self._state.clarification_holds_used += 1
        if self._state.clarification_holds_used >= 3:
            self.escalated = True
            self._state.record_uncertain_assumption(
                slot=self._current_slot,
                question=self._current_question,
                assumed_value=self._default_value,
                source="merchant_impatience",
            )
            line = load_prompt(f"impatience_end_{self._lang}").strip()
            await self._safe_speak(line)
            return

        line = load_prompt(f"hold_filler_{self._lang}").strip()
        if self._emit_filler is not None:
            await self._emit_filler(line)
        await self._safe_speak(line)
        if self._on_keepalive_reset is not None:
            self._on_keepalive_reset()

    async def _safe_speak(self, line: str) -> None:
        try:
            await self._merchant_speak(line)
        except Exception:
            log.debug("reactive holding TTS failed", exc_info=True)


__all__ = ["ReactiveHolding"]
