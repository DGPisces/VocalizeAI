"""REST routes for session lifecycle.

These three endpoints are the only HTTP surface the frontend hits before the
WebSocket connects; everything after WS open is over the WS frame protocol.
"""
from __future__ import annotations

import logging
import os
import secrets
from typing import Literal

log = logging.getLogger(__name__)

from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
from pydantic import BaseModel, Field

from vocalize.server.review import register_review_routes
from vocalize.server.state import SessionRegistry


class CreateSessionRequest(BaseModel):
    preferred_voice_id: str | None = None
    auto_translate_merchant: bool = True
    default_lang: Literal["zh", "en"] | None = None


class CreateSessionResponse(BaseModel):
    session_id: str
    ws_url: str
    default_lang: str
    preferred_voice_id: str | None
    auto_translate_merchant: bool


class GetSessionResponse(BaseModel):
    session_id: str
    default_lang: str
    task_description: str | None
    preferred_voice_id: str | None
    auto_translate_merchant: bool
    phase: str
    uncertain_assumptions: list[dict]
    pending_callbacks: list[dict]


class SetTaskRequest(BaseModel):
    task: str = Field(min_length=1, max_length=2000)


class SetTaskResponse(BaseModel):
    ok: bool = True


def _check_invite_token(
    x_invite_token: str | None = Header(default=None, alias="X-Invite-Token"),
) -> None:
    """Verify the shared invite secret on session creation (D-08).

    Gate behaviour:
    - If VOCALIZE_INVITE_TOKEN is not configured (localhost-dev mode), the
      check is skipped so local development keeps working without any env setup.
    - In production (non-localhost host), the token is required and must
      match via constant-time comparison to avoid timing oracles (T-04c-02).

    # TODO(v1.x AUTH-01): no rotation; this is a long-lived shared secret.
    #   Per-user auth replaces this gate in v1.x.
    """
    from vocalize.config import get_config

    expected = get_config().invite_token
    if expected is None:
        return  # localhost-dev mode: gate disabled
    try:
        match = secrets.compare_digest(x_invite_token or "", expected)
    except (TypeError, ValueError):
        # secrets.compare_digest raises TypeError when either string contains
        # non-ASCII characters. This is operator misconfiguration (e.g. a
        # Unicode passphrase). Log once and return 401 — do not propagate as
        # a 500 which would increment vocalize_error_log_total toward the
        # D-05 budget (T-04c-02).
        log.warning(
            "VOCALIZE_INVITE_TOKEN contains non-ASCII characters; "
            "compare_digest raised TypeError — returning 401"
        )
        match = False
    if not match:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid or missing X-Invite-Token",
        )


def register_session_routes(
    app: FastAPI,
    *,
    registry: SessionRegistry,
) -> None:
    """Attach the three REST routes to ``app``.

    The ``POST /api/sessions`` endpoint derives the WebSocket URL from the
    incoming request's base URL when ``VOCALIZE_WS_BASE_URL`` is not set,
    so the returned ``ws_url`` always matches the actual listener address.
    """
    register_review_routes(app, registry=registry)

    @app.post("/api/sessions", response_model=CreateSessionResponse)
    async def create_session(
        request: Request,
        payload: CreateSessionRequest | None = None,
        _gate: None = Depends(_check_invite_token),
    ) -> CreateSessionResponse:
        registry.sweep_stale(max_age_s=1800)
        payload = payload or CreateSessionRequest()
        s = registry.create(
            default_lang=payload.default_lang or "zh",
            preferred_voice_id=payload.preferred_voice_id,
            auto_translate_merchant=payload.auto_translate_merchant,
        )
        # When the operator has explicitly set VOCALIZE_WS_BASE_URL, use it
        # as the source of truth (it is the only way to work behind proxies
        # or custom domains). Otherwise derive the WS URL from the request's
        # base URL so it stays in sync with the actual bind address even when
        # the server is launched with a CLI --port that differs from the
        # VOCALIZE_PORT env default.
        explicit = os.getenv("VOCALIZE_WS_BASE_URL")
        if explicit:
            base = explicit
        else:
            base = str(request.base_url).rstrip("/").replace(
                "http://", "ws://", 1
            ).replace("https://", "wss://", 1)
        return CreateSessionResponse(
            session_id=s.session_id,
            ws_url=f"{base}/ws/sessions/{s.session_id}",
            default_lang=s.default_lang,
            preferred_voice_id=s.preferred_voice_id,
            auto_translate_merchant=s.auto_translate_merchant,
        )

    @app.get("/api/sessions/{session_id}", response_model=GetSessionResponse)
    async def get_session(session_id: str) -> GetSessionResponse:
        s = registry.get(session_id)
        if s is None:
            raise HTTPException(status_code=404, detail="session not found")
        phase = s.task_state.phase.value if s.task_state is not None else "draft"
        uncertain_assumptions = (
            [
                assumption.model_dump(mode="json")
                for assumption in s.task_state.uncertain_assumptions
            ]
            if s.task_state is not None
            else []
        )
        pending_callbacks = (
            [
                callback.model_dump(mode="json")
                for callback in s.task_state.pending_callbacks
            ]
            if s.task_state is not None
            else []
        )
        return GetSessionResponse(
            session_id=s.session_id,
            default_lang=s.default_lang,
            task_description=s.task_description,
            preferred_voice_id=s.preferred_voice_id,
            auto_translate_merchant=s.auto_translate_merchant,
            phase=phase,
            uncertain_assumptions=uncertain_assumptions,
            pending_callbacks=pending_callbacks,
        )

    @app.delete("/api/sessions/{session_id}")
    async def delete_session(session_id: str) -> dict[str, bool]:
        s = registry.get(session_id)
        if s is None:
            raise HTTPException(status_code=404, detail="session not found")
        if registry.is_active(session_id):
            raise HTTPException(status_code=409, detail="session is active")
        registry.remove(session_id)
        return {"ok": True}

    @app.post("/api/sessions/{session_id}/task", response_model=SetTaskResponse)
    async def set_task(session_id: str, payload: SetTaskRequest) -> SetTaskResponse:
        task = payload.task.strip()
        if not task:
            raise HTTPException(status_code=422, detail="task must be non-empty")
        try:
            registry.set_task(session_id, task)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="session not found") from exc
        return SetTaskResponse()


__all__ = ["register_session_routes"]
