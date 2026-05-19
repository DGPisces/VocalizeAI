from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Literal

import pytest

from vocalize.dialogue.prompts import load_prompt
from vocalize.dialogue.reactive_holding import ReactiveHolding
from vocalize.dialogue.state import TaskPhase, TaskState


@pytest.fixture
def state_in_clarification() -> TaskState:
    return TaskState(
        session_id="s",
        user_task_description="t",
        phase=TaskPhase.AWAIT_USER_CLARIFICATION,
    )


async def _noop_speak(_text: str) -> None:
    return None


def _make_handler(
    *,
    state: TaskState,
    merchant_speak: Callable[[str], Awaitable[None]] = _noop_speak,
    lang: Literal["zh", "en"] = "zh",
    current_slot: str = "x",
    current_question: str = "?",
    default_value: Any = 4,
    on_keepalive_reset: Callable[[], None] | None = None,
) -> ReactiveHolding:
    return ReactiveHolding(
        state=state,
        merchant_speak=merchant_speak,
        lang=lang,
        current_slot=current_slot,
        current_question=current_question,
        default_value=default_value,
        on_keepalive_reset=on_keepalive_reset,
    )


@pytest.mark.asyncio
async def test_start_cycle_resets_holds_used(state_in_clarification: TaskState) -> None:
    state = state_in_clarification
    state.clarification_holds_used = 7
    rh = _make_handler(state=state)

    rh.start_cycle()

    assert state.clarification_holds_used == 0


@pytest.mark.asyncio
async def test_first_two_interruptions_inject_filler_and_increment(
    state_in_clarification: TaskState,
) -> None:
    state = state_in_clarification
    sent: list[str] = []

    async def speak(text: str) -> None:
        sent.append(text)

    keepalive_resets: list[bool] = []
    rh = _make_handler(
        state=state,
        merchant_speak=speak,
        on_keepalive_reset=lambda: keepalive_resets.append(True),
    )

    rh.start_cycle()
    await rh.on_interruption()
    await rh.on_interruption()

    assert state.clarification_holds_used == 2
    assert sent == [load_prompt("hold_filler_zh").strip()] * 2
    assert rh.escalated is False
    assert state.uncertain_assumptions == []
    assert keepalive_resets == [True, True]


@pytest.mark.asyncio
async def test_third_interruption_records_assumption_and_escalates(
    state_in_clarification: TaskState,
) -> None:
    state = state_in_clarification
    sent: list[str] = []

    async def speak(text: str) -> None:
        sent.append(text)

    rh = _make_handler(
        state=state,
        merchant_speak=speak,
        current_slot="party_size",
        current_question="how many?",
        default_value=4,
    )

    rh.start_cycle()
    await rh.on_interruption()
    await rh.on_interruption()
    await rh.on_interruption()

    assert rh.escalated is True
    assert state.clarification_holds_used == 3
    assert len(state.uncertain_assumptions) == 1
    assumption = state.uncertain_assumptions[0]
    assert assumption.slot == "party_size"
    assert assumption.question == "how many?"
    assert assumption.assumed_value == 4
    assert assumption.source == "merchant_impatience"
    assert sent[-1] == load_prompt("impatience_end_zh").strip()


@pytest.mark.asyncio
async def test_interruptions_after_escalation_do_not_duplicate_side_effects(
    state_in_clarification: TaskState,
) -> None:
    state = state_in_clarification
    sent: list[str] = []

    async def speak(text: str) -> None:
        sent.append(text)

    rh = _make_handler(
        state=state,
        merchant_speak=speak,
        current_slot="party_size",
        current_question="how many?",
        default_value=4,
    )

    rh.start_cycle()
    await rh.on_interruption()
    await rh.on_interruption()
    await rh.on_interruption()
    await rh.on_interruption()

    assert state.clarification_holds_used == 3
    assert len(state.uncertain_assumptions) == 1
    assert sent == [
        load_prompt("hold_filler_zh").strip(),
        load_prompt("hold_filler_zh").strip(),
        load_prompt("impatience_end_zh").strip(),
    ]


@pytest.mark.asyncio
async def test_merchant_speak_failure_does_not_block_state_updates(
    state_in_clarification: TaskState,
) -> None:
    state = state_in_clarification

    async def fail_speak(_text: str) -> None:
        raise RuntimeError("tts failed")

    rh = _make_handler(
        state=state,
        merchant_speak=fail_speak,
        current_slot="party_size",
        current_question="how many?",
        default_value=4,
    )

    rh.start_cycle()
    await rh.on_interruption()
    await rh.on_interruption()
    await rh.on_interruption()

    assert rh.escalated is True
    assert state.clarification_holds_used == 3
    assert len(state.uncertain_assumptions) == 1
