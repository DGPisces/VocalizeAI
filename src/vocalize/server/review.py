"""Trimmed post-call review REST surface."""
from __future__ import annotations

import time
from datetime import datetime
from typing import Any, Literal

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from vocalize.dialogue.state import (
    CallSegment,
    CallbackEntry,
    SlotAssumption,
    TaskAuditEntry,
    TaskPhase,
    TaskState,
)
from vocalize.server.state import SessionRegistry


ReviewStatus = Literal["completed", "interrupted", "escalated"]


class CallSegmentDTO(BaseModel):
    id: str
    index: int
    started_at: datetime
    ended_at: datetime | None
    interrupted: bool
    interrupt_reason: Literal["ws_close", "user_hangup", "merchant_impatience"] | None
    transcript: list[dict]


class ReviewResponse(BaseModel):
    session_id: str
    status: ReviewStatus
    slots: dict[str, Any]
    uncertain_assumptions: list[dict]
    pending_callbacks: list[dict]
    completion_summary: str | None
    call_segments: list[CallSegmentDTO]


class ConfirmAssumptionRequest(BaseModel):
    assumption_id: str
    confirmed_value: Any | None = None


def _not_found(detail: str) -> HTTPException:
    return HTTPException(
        status_code=404,
        detail={"message_zh": detail, "message_en": detail},
    )


def _illegal_state(detail: str) -> HTTPException:
    return HTTPException(
        status_code=409,
        detail={"message_zh": detail, "message_en": detail},
    )


def _find_assumption(state: TaskState, assumption_id: str) -> SlotAssumption:
    assumption = state.find_assumption_by_id(assumption_id)
    if assumption is None:
        raise _not_found(f"Assumption {assumption_id!r} not found")
    return assumption


def _find_callback(state: TaskState, callback_id: str) -> CallbackEntry:
    callback = next(
        (item for item in state.pending_callbacks if item.id == callback_id),
        None,
    )
    if callback is None:
        raise _not_found(f"Callback {callback_id!r} not found")
    return callback


def _derive_status(state: TaskState) -> ReviewStatus:
    if state.call_segments:
        reason = state.call_segments[-1].interrupt_reason
        if reason == "merchant_impatience":
            return "escalated"
        if reason in {"ws_close", "user_hangup"}:
            return "interrupted"
        # Last segment is the primary signal. If it ended normally, the audit
        # trail is only a tiebreaker for review paths that did not close a
        # segment explicitly.

    for entry in reversed(state.audit_log):
        to_phase = getattr(entry.to_phase, "value", entry.to_phase)
        if to_phase != TaskPhase.POST_CALL_REVIEW.value:
            continue
        reason_text = entry.reason.lower()
        if "impatience" in reason_text:
            return "escalated"
        if "ws disconnect" in reason_text:
            return "interrupted"
        return "completed"
    return "completed"


def _assumption_to_review_dict(assumption: SlotAssumption) -> dict[str, Any]:
    body = assumption.model_dump(mode="json")
    body["confirmed_value"] = (
        assumption.correction if assumption.status == "corrected" else None
    )
    return body


def _build_segment(segment: CallSegment, state: TaskState) -> CallSegmentDTO:
    transcript = [
        message.model_dump(mode="json")
        for message in getattr(state, "transcripts", [])
        if message.segment_id == segment.id
    ]
    return CallSegmentDTO(
        id=segment.id,
        index=segment.index,
        started_at=segment.started_at,
        ended_at=segment.ended_at,
        interrupted=segment.interrupted,
        interrupt_reason=segment.interrupt_reason,
        transcript=transcript,
    )


def _build_review_response(state: TaskState) -> ReviewResponse:
    return ReviewResponse(
        session_id=state.session_id,
        status=_derive_status(state),
        slots=dict(state.slots),
        uncertain_assumptions=[
            _assumption_to_review_dict(item)
            for item in state.uncertain_assumptions
        ],
        pending_callbacks=[
            item.model_dump(mode="json")
            for item in state.pending_callbacks
        ],
        completion_summary=getattr(state, "completion_summary", None),
        call_segments=[
            _build_segment(segment, state)
            for segment in state.call_segments
        ],
    )


def _state_for_review(registry: SessionRegistry, session_id: str) -> TaskState:
    session = registry.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")
    if session.task_state is None:
        return TaskState(
            session_id=session.session_id,
            user_task_description=session.task_description or "",
            phase=TaskPhase.COMPLETED,
        )
    return session.task_state


def register_review_routes(app: FastAPI, *, registry: SessionRegistry) -> None:
    @app.get("/api/sessions/{session_id}/review", response_model=ReviewResponse)
    async def get_review(session_id: str) -> ReviewResponse:
        return _build_review_response(_state_for_review(registry, session_id))

    @app.post(
        "/api/sessions/{session_id}/confirm_assumption",
        response_model=ReviewResponse,
    )
    async def confirm_assumption(
        session_id: str,
        payload: ConfirmAssumptionRequest,
    ) -> ReviewResponse:
        state = _state_for_review(registry, session_id)
        assumption = _find_assumption(state, payload.assumption_id)
        if payload.confirmed_value is None:
            assumption.status = "confirmed"
            assumption.correction = None
        else:
            assumption.status = "corrected"
            assumption.correction = str(payload.confirmed_value)
        return _build_review_response(state)

    @app.post(
        "/api/sessions/{session_id}/callbacks/{cb_id}/cancel",
        response_model=ReviewResponse,
    )
    async def cancel_callback(session_id: str, cb_id: str) -> ReviewResponse:
        state = _state_for_review(registry, session_id)
        callback = _find_callback(state, cb_id)
        callback.status = "cancelled"
        return _build_review_response(state)

    @app.post(
        "/api/sessions/{session_id}/callbacks/{cb_id}/restore",
        response_model=ReviewResponse,
    )
    async def restore_callback(session_id: str, cb_id: str) -> ReviewResponse:
        state = _state_for_review(registry, session_id)
        callback = _find_callback(state, cb_id)
        if callback.status != "cancelled":
            raise _illegal_state(f"Callback {cb_id!r} is not in cancelled state")
        callback.status = "queued"
        return _build_review_response(state)

    @app.post(
        "/api/sessions/{session_id}/callbacks/{cb_id}/trigger",
        response_model=ReviewResponse,
    )
    async def trigger_callback(session_id: str, cb_id: str) -> ReviewResponse:
        state = _state_for_review(registry, session_id)
        callback = _find_callback(state, cb_id)
        if callback.status in {"cancelled", "triggered", "completed"}:
            raise _illegal_state(f"Callback {cb_id!r} cannot be triggered")
        callback.status = "triggered"
        state.audit_log.append(
            TaskAuditEntry(
                timestamp=time.monotonic(),
                from_phase=state.phase,
                to_phase=state.phase,
                reason="standalone review callback trigger",
                evidence={
                    "callback_id": cb_id,
                    "source": "standalone_review_rest",
                },
            )
        )
        return _build_review_response(state)


__all__ = [
    "CallSegmentDTO",
    "ConfirmAssumptionRequest",
    "ReviewResponse",
    "register_review_routes",
]
