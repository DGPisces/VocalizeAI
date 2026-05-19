from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from typing import Any

import pytest

from vocalize.dialogue.callback import (
    drive_callback_turn,
    render_callback_prompt,
    run_callback,
)
from vocalize.dialogue.state import (
    CallbackEntry,
    DialogueOrchestratorError,
    SlotAssumption,
    TaskPhase,
    TaskState,
)
from vocalize.llm.base import ChatMessage, FinishChunk, TextDelta, ToolCallDelta


class _ScriptedLLM:
    def __init__(self, script: list[Any]) -> None:
        self._script = list(script)
        self.calls: list[list[ChatMessage]] = []

    async def stream_chat(
        self,
        *,
        messages: list[ChatMessage],
    ) -> AsyncIterator[Any]:
        self.calls.append(messages)
        for chunk in self._script:
            yield chunk


class _TurnScriptedLLM:
    def __init__(self, scripts: list[list[Any]]) -> None:
        self._scripts = list(scripts)
        self.calls: list[list[ChatMessage]] = []

    async def stream_chat(
        self,
        *,
        messages: list[ChatMessage],
    ) -> AsyncIterator[Any]:
        self.calls.append(messages)
        script = self._scripts.pop(0)
        for chunk in script:
            yield chunk


def test_render_callback_prompt_zh_substitutes_placeholders() -> None:
    state = TaskState(session_id="s", user_task_description="t")
    assumption = SlotAssumption(
        id="a-1",
        slot="party_size",
        question="how many?",
        assumed_value=4,
        source="user_timeout",
        created_at=datetime.now(timezone.utc),
    )
    state.uncertain_assumptions.append(assumption)
    callback = CallbackEntry(
        id="cb-1",
        assumption_id="a-1",
        correction="6",
        note="6 adults",
        created_at=datetime.now(timezone.utc),
    )

    rendered = render_callback_prompt(
        state=state,
        callback=callback,
        lang="zh",
    )

    assert "party_size" in rendered
    assert "4" in rendered
    assert "6" in rendered
    assert "6 adults" in rendered
    assert "{{" not in rendered


def test_render_callback_prompt_en_handles_missing_assumption() -> None:
    state = TaskState(session_id="s", user_task_description="t")
    callback = CallbackEntry(
        id="cb-1",
        assumption_id="missing",
        correction="x",
        created_at=datetime.now(timezone.utc),
    )

    rendered = render_callback_prompt(
        state=state,
        callback=callback,
        lang="en",
    )

    assert "{{slot}}" not in rendered
    assert "{{assumed_value}}" not in rendered
    assert "x" in rendered


@pytest.mark.asyncio
async def test_drive_callback_turn_streams_text_without_finalize() -> None:
    finalize = asyncio.Event()
    llm = _ScriptedLLM([
        TextDelta(text="刚才说错了一点，"),
        TextDelta(text="其实是 6 位。"),
        FinishChunk(reason="stop"),
    ])

    text = await drive_callback_turn(
        system_prompt="callback system",
        merchant_message=None,
        llm=llm,
        finalize_event=finalize,
    )

    assert "其实是 6 位" in text
    assert finalize.is_set() is False
    assert llm.calls[0] == [ChatMessage(role="system", content="callback system")]


@pytest.mark.asyncio
async def test_drive_callback_turn_finalize_event_set_on_finalize_task() -> None:
    finalize = asyncio.Event()
    llm = _ScriptedLLM([
        ToolCallDelta(
            tool_call_index=0,
            tool_call_id="x",
            name="finalize_task",
            arguments_delta=json.dumps({"success": True}),
        ),
        FinishChunk(reason="tool_calls"),
    ])

    await drive_callback_turn(
        system_prompt="callback system",
        merchant_message="ok",
        llm=llm,
        finalize_event=finalize,
    )

    assert finalize.is_set() is True
    assert llm.calls[0] == [
        ChatMessage(role="system", content="callback system"),
        ChatMessage(role="user", content="ok"),
    ]


@pytest.mark.asyncio
async def test_run_callback_transitions_phases_and_records_segment_id() -> None:
    state = TaskState(
        session_id="s",
        user_task_description="t",
        phase=TaskPhase.POST_CALL_REVIEW,
    )
    assumption = SlotAssumption(
        id="a-1",
        slot="party_size",
        question="how many?",
        assumed_value=4,
        source="user_timeout",
        created_at=datetime.now(timezone.utc),
    )
    state.uncertain_assumptions.append(assumption)
    callback = CallbackEntry(
        id="cb-1",
        assumption_id="a-1",
        correction="6",
        created_at=datetime.now(timezone.utc),
    )
    llm = _TurnScriptedLLM([
        [
            TextDelta(text="刚才说错了一点，其实是 6 位。"),
            FinishChunk(reason="stop"),
        ],
        [
            ToolCallDelta(
                tool_call_index=0,
                tool_call_id="x",
                name="finalize_task",
                arguments_delta=json.dumps({"success": True}),
            ),
            FinishChunk(reason="tool_calls"),
        ],
    ])
    transcript_recorded: list[tuple[str, str, str]] = []

    async def emit_transcript(role: str, text: str, segment_id: str) -> None:
        transcript_recorded.append((role, text, segment_id))

    async def await_merchant_reply() -> str:
        return "好的，记下了"

    await run_callback(
        state=state,
        callback=callback,
        llm=llm,
        emit_transcript=emit_transcript,
        await_merchant_reply=await_merchant_reply,
        lang="zh",
    )

    assert state.phase == TaskPhase.POST_CALL_REVIEW
    assert callback.status == "completed"
    assert callback.started_at is not None
    assert callback.completed_at is not None
    assert callback.transcript_segment_id is not None
    assert (
        "ai_to_merchant",
        "刚才说错了一点，其实是 6 位。",
        callback.transcript_segment_id,
    ) in transcript_recorded
    assert (
        "merchant_to_ai",
        "好的，记下了",
        callback.transcript_segment_id,
    ) in transcript_recorded


@pytest.mark.asyncio
async def test_run_callback_rejects_non_review_phase() -> None:
    state = TaskState(
        session_id="s",
        user_task_description="t",
        phase=TaskPhase.EXECUTION_ACTIVE,
    )
    callback = CallbackEntry(
        id="cb-1",
        assumption_id="missing",
        correction="x",
        created_at=datetime.now(timezone.utc),
    )

    async def emit_transcript(role: str, text: str, segment_id: str) -> None:
        raise AssertionError("should not emit")

    async def await_merchant_reply() -> str:
        raise AssertionError("should not await")

    with pytest.raises(DialogueOrchestratorError):
        await run_callback(
            state=state,
            callback=callback,
            llm=_TurnScriptedLLM([]),
            emit_transcript=emit_transcript,
            await_merchant_reply=await_merchant_reply,
            lang="en",
        )

    assert callback.status == "queued"


@pytest.mark.asyncio
async def test_run_callback_marks_failed_when_merchant_reply_fails() -> None:
    state = TaskState(
        session_id="s",
        user_task_description="t",
        phase=TaskPhase.POST_CALL_REVIEW,
    )
    callback = CallbackEntry(
        id="cb-1",
        assumption_id="missing",
        correction="x",
        created_at=datetime.now(timezone.utc),
    )
    llm = _TurnScriptedLLM([
        [TextDelta(text="Correction."), FinishChunk(reason="stop")],
    ])

    async def emit_transcript(role: str, text: str, segment_id: str) -> None:
        pass

    async def await_merchant_reply() -> str:
        raise RuntimeError("stt failed")

    with pytest.raises(RuntimeError):
        await run_callback(
            state=state,
            callback=callback,
            llm=llm,
            emit_transcript=emit_transcript,
            await_merchant_reply=await_merchant_reply,
            lang="en",
        )

    assert state.phase == TaskPhase.POST_CALL_REVIEW
    assert callback.status == "failed"
    assert callback.completed_at is not None


@pytest.mark.asyncio
async def test_run_callback_marks_failed_when_max_turns_exhausted() -> None:
    state = TaskState(
        session_id="s",
        user_task_description="t",
        phase=TaskPhase.POST_CALL_REVIEW,
    )
    callback = CallbackEntry(
        id="cb-1",
        assumption_id="missing",
        correction="x",
        created_at=datetime.now(timezone.utc),
    )
    llm = _TurnScriptedLLM([
        [TextDelta(text="Still checking."), FinishChunk(reason="stop")],
    ])

    async def emit_transcript(role: str, text: str, segment_id: str) -> None:
        pass

    async def await_merchant_reply() -> str:
        return "not finalized"

    with pytest.raises(DialogueOrchestratorError):
        await run_callback(
            state=state,
            callback=callback,
            llm=llm,
            emit_transcript=emit_transcript,
            await_merchant_reply=await_merchant_reply,
            lang="en",
            max_turns=1,
        )

    assert state.phase == TaskPhase.POST_CALL_REVIEW
    assert callback.status == "failed"
    assert callback.completed_at is not None
