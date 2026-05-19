from __future__ import annotations

import asyncio

import pytest

from vocalize.dialogue.keepalive import KeepaliveTimer
from vocalize.server.frames import TranscriptSubtype
from typing import get_args


class _FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


@pytest.mark.asyncio
async def test_keepalive_fires_after_interval() -> None:
    sent: list[str] = []

    async def speak(text: str) -> None:
        sent.append(text)

    timer = KeepaliveTimer(
        merchant_speak=speak,
        lang="zh",
        interval_s=0.05,
        suppression_window_s=0.025,
    )
    task = asyncio.create_task(timer.run())
    await asyncio.sleep(0.18)
    timer.stop()
    await task

    assert len(sent) >= 2
    assert all("正在确认" in text for text in sent)


def test_transcript_subtype_includes_filler_and_keepalive() -> None:
    assert {"filler", "keepalive"}.issubset(set(get_args(TranscriptSubtype)))


@pytest.mark.asyncio
async def test_keepalive_timer_emit_transcript_called_each_cycle_with_keepalive_subtype() -> None:
    order: list[tuple[str, str]] = []

    async def emit_transcript(text: str) -> None:
        order.append(("emit", text))

    async def speak(text: str) -> None:
        order.append(("speak", text))

    timer = KeepaliveTimer(
        merchant_speak=speak,
        lang="zh",
        interval_s=0.01,
        suppression_window_s=0.0,
        emit_transcript=emit_transcript,
    )
    task = asyncio.create_task(timer.run())
    await asyncio.sleep(0.035)
    timer.stop()
    await task

    assert len(order) >= 2
    assert order[0][0] == "emit"
    assert order[1][0] == "speak"
    assert order[0][1] == order[1][1]


@pytest.mark.asyncio
async def test_keepalive_default_emit_transcript_none_is_backward_compatible() -> None:
    sent: list[str] = []

    async def speak(text: str) -> None:
        sent.append(text)

    timer = KeepaliveTimer(
        merchant_speak=speak,
        lang="zh",
        interval_s=0.01,
        suppression_window_s=0.0,
    )
    task = asyncio.create_task(timer.run())
    await asyncio.sleep(0.025)
    timer.stop()
    await task

    assert sent


@pytest.mark.asyncio
async def test_keepalive_reset_suppresses_immediately_following_tick() -> None:
    sent: list[str] = []
    clock = _FakeClock()

    async def speak(text: str) -> None:
        sent.append(text)

    timer = KeepaliveTimer(
        merchant_speak=speak,
        lang="zh",
        interval_s=0.05,
        suppression_window_s=0.08,
        monotonic=clock,
    )
    task = asyncio.create_task(timer.run())
    clock.now = 0.0
    timer.note_reactive_filler()
    clock.now = 0.06
    await asyncio.sleep(0.06)
    timer.stop()
    await task

    assert sent == []


@pytest.mark.asyncio
async def test_stop_cancels_in_flight_keepalive_speech() -> None:
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def blocking_speak(_text: str) -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    timer = KeepaliveTimer(
        merchant_speak=blocking_speak,
        lang="zh",
        interval_s=0.01,
        suppression_window_s=0.0,
    )
    task = asyncio.create_task(timer.run())
    await asyncio.wait_for(started.wait(), timeout=0.2)

    timer.stop()
    await asyncio.wait_for(task, timeout=0.2)

    assert cancelled.is_set()
