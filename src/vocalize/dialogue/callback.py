"""Callback correction helpers for follow-up merchant calls."""
from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Literal

from vocalize.dialogue.prompts import load_prompt
from vocalize.dialogue.state import (
    CallbackEntry,
    DialogueOrchestratorError,
    TaskPhase,
    TaskState,
)
from vocalize.llm.base import ChatMessage, FinishChunk, TextDelta, ToolCallDelta

__all__ = ["drive_callback_turn", "render_callback_prompt", "run_callback"]

EmitTranscript = Callable[[str, str, str], Awaitable[None]]
AwaitMerchantReply = Callable[[], Awaitable[str]]


def render_callback_prompt(
    *,
    state: TaskState,
    callback: CallbackEntry,
    lang: Literal["zh", "en"],
) -> str:
    """Render callback correction prompt with assumption context."""
    assumption = next(
        (
            item for item in state.uncertain_assumptions
            if item.id == callback.assumption_id
        ),
        None,
    )
    return (
        load_prompt(f"callback_correction_{lang}")
        .replace("{{slot}}", assumption.slot if assumption else "")
        .replace(
            "{{assumed_value}}",
            str(assumption.assumed_value) if assumption else "",
        )
        .replace("{{correction}}", callback.correction)
        .replace("{{note}}", callback.note or "")
    )


async def drive_callback_turn(
    *,
    system_prompt: str,
    merchant_message: str | None,
    llm,
    finalize_event: asyncio.Event,
) -> str:
    """Stream one callback merchant LLM turn and notice finalize_task."""
    messages = [ChatMessage(role="system", content=system_prompt)]
    if merchant_message is not None:
        messages.append(ChatMessage(role="user", content=merchant_message))

    pieces: list[str] = []
    tool_names: dict[int, str] = {}
    finish_reason: str | None = None

    async for chunk in llm.stream_chat(messages=messages):
        if isinstance(chunk, TextDelta):
            pieces.append(chunk.text)
        elif isinstance(chunk, ToolCallDelta):
            if chunk.name:
                tool_names[chunk.tool_call_index] = chunk.name
        elif isinstance(chunk, FinishChunk):
            finish_reason = chunk.reason
            break

    if finish_reason == "tool_calls" and "finalize_task" in tool_names.values():
        finalize_event.set()
    return "".join(pieces).strip()


async def run_callback(
    *,
    state: TaskState,
    callback: CallbackEntry,
    llm,
    emit_transcript: EmitTranscript,
    await_merchant_reply: AwaitMerchantReply,
    lang: Literal["zh", "en"],
    max_turns: int = 6,
    transition_to_active: bool = True,
) -> None:
    """Run a callback correction conversation lifecycle."""
    if transition_to_active and state.phase != TaskPhase.POST_CALL_REVIEW:
        raise DialogueOrchestratorError(
            "run_callback called outside post_call_review"
        )
    if not transition_to_active and state.phase != TaskPhase.CALLBACK_ACTIVE:
        raise DialogueOrchestratorError(
            "run_callback called before callback_active"
        )

    if transition_to_active:
        state.transition(TaskPhase.CALLBACK_ACTIVE, reason="user-triggered callback")
    callback.status = "in_progress"
    callback.started_at = datetime.now(timezone.utc)
    segment_id = uuid.uuid4().hex
    callback.transcript_segment_id = segment_id

    system_prompt = render_callback_prompt(
        state=state,
        callback=callback,
        lang=lang,
    )
    finalize_event = asyncio.Event()
    merchant_message: str | None = None

    try:
        for _ in range(max_turns):
            ai_text = await drive_callback_turn(
                system_prompt=system_prompt,
                merchant_message=merchant_message,
                llm=llm,
                finalize_event=finalize_event,
            )
            if ai_text:
                await emit_transcript("ai_to_merchant", ai_text, segment_id)
            if finalize_event.is_set():
                break
            merchant_message = await await_merchant_reply()
            if merchant_message:
                await emit_transcript(
                    "merchant_to_ai",
                    merchant_message,
                    segment_id,
                )
        if finalize_event.is_set():
            callback.status = "completed"
        else:
            raise DialogueOrchestratorError(
                "callback did not finalize before max_turns"
            )
    except Exception:
        callback.status = "failed"
        raise
    finally:
        callback.completed_at = datetime.now(timezone.utc)
        try:
            state.transition(
                TaskPhase.POST_CALL_REVIEW,
                reason="callback finished",
                evidence={"segment_id": segment_id},
            )
        except DialogueOrchestratorError:
            pass
