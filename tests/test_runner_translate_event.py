from __future__ import annotations

import asyncio

import pytest

from vocalize.server.runner import _ReadinessChangeDebouncer, _translate_event


def test_transition_event_becomes_phase_change_event() -> None:
    out = _translate_event({
        "event": "transition",
        "from": "READY_TO_DIAL",
        "to": "EXECUTION_ACTIVE",
    })

    assert out == {
        "event": "phase_change",
        "previous": "ready_to_dial",
        "current": "execution_active",
    }


def test_unknown_transition_shape_falls_back_to_state_update() -> None:
    event = {"event": "transition", "to": "EXECUTION_ACTIVE"}

    out = _translate_event(event)

    assert out == {"event": "state_update", "diff": event}


def test_browser_visible_review_events_passthrough() -> None:
    for event in (
        {
            "event": "phase_change",
            "previous": "execution_active",
            "current": "post_call_review",
        },
        {
            "event": "uncertain_assumption_added",
            "assumption": {"id": "a-1"},
        },
        {
            "event": "pending_callback_added",
            "callback": {"id": "cb-1"},
        },
        {
            "event": "escalation_warning",
            "reason": "merchant_impatience",
            "holds_used": 3,
            "message_zh": "商家催了三次",
            "message_en": "Merchant interrupted 3 times",
        },
    ):
        assert _translate_event(event) == event


@pytest.mark.asyncio
async def test_readiness_change_debouncer_emits_latest_only() -> None:
    sent: list[dict] = []

    async def push_event(frame: dict) -> None:
        sent.append(frame)

    debouncer = _ReadinessChangeDebouncer(push_event, delay_s=0.1)
    first = {
        "event": "readiness_change",
        "passed": False,
        "missing_critical": ["date"],
        "confidence": 0.4,
    }
    second = {
        "event": "readiness_change",
        "passed": True,
        "missing_critical": [],
        "confidence": 0.9,
    }

    await debouncer.submit(first)
    await asyncio.sleep(0.05)
    await debouncer.submit(second)
    await asyncio.sleep(0.07)
    assert sent == []

    await asyncio.sleep(0.05)
    assert sent == [second]


@pytest.mark.asyncio
async def test_readiness_change_flush_preserves_inflight_frame() -> None:
    sent: list[dict] = []
    entered_push = asyncio.Event()
    push_calls = 0
    frame = {
        "event": "readiness_change",
        "passed": True,
        "missing_critical": [],
        "confidence": 0.9,
    }

    async def push_event(pushed: dict) -> None:
        nonlocal push_calls
        push_calls += 1
        if push_calls == 1:
            entered_push.set()
            await asyncio.Event().wait()
        sent.append(pushed)

    debouncer = _ReadinessChangeDebouncer(push_event, delay_s=0)

    await debouncer.submit(frame)
    await asyncio.wait_for(entered_push.wait(), timeout=1.0)

    await asyncio.wait_for(debouncer.flush(), timeout=1.0)

    assert sent == [frame]
    assert push_calls == 2
