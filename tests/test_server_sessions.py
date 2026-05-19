"""REST routes for session creation + task setup.

Uses ``httpx.AsyncClient`` against a small FastAPI app assembled inline so
each test sees a fresh ``SessionRegistry``.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from vocalize.dialogue.state import (
    CallSegment,
    CallbackEntry,
    SlotAssumption,
    TaskAuditEntry,
    TaskPhase,
    TaskState,
    TranscriptMessage,
)
from vocalize.server import create_app
from vocalize.server.sessions import register_session_routes
from vocalize.server.state import SessionRegistry


def _app() -> tuple[FastAPI, SessionRegistry]:
    app = FastAPI()
    registry = SessionRegistry()
    register_session_routes(app, registry=registry)
    return app, registry


def _review_state(session_id: str = "s") -> TaskState:
    now = datetime(2026, 5, 7, 12, tzinfo=timezone.utc)
    seg_a = CallSegment(id="seg-a", index=1, started_at=now)
    seg_b = CallSegment(
        id="seg-b",
        index=2,
        started_at=now,
        ended_at=now,
        interrupted=True,
        interrupt_reason="ws_close",
    )
    return TaskState(
        session_id=session_id,
        user_task_description="demo",
        phase=TaskPhase.POST_CALL_REVIEW,
        slots={"party_size": 4},
        uncertain_assumptions=[
            SlotAssumption(
                id="a-1",
                slot="party_size",
                question="How many?",
                assumed_value=4,
                source="user_timeout",
                created_at=now,
            )
        ],
        pending_callbacks=[
            CallbackEntry(
                id="cb-1",
                assumption_id="a-1",
                correction="6",
                created_at=now,
            )
        ],
        call_segments=[seg_a, seg_b],
        transcripts=[
            TranscriptMessage(
                id="t-1",
                role="merchant_to_ai",
                text="hello",
                lang="en",
                is_final=True,
                segment_id="seg-a",
                created_at=now,
            ),
            TranscriptMessage(
                id="t-2",
                role="ai_to_merchant",
                text="sure",
                lang="en",
                is_final=True,
                segment_id="seg-b",
                created_at=now,
            ),
        ],
        completion_summary="done",
    )


@pytest.fixture
async def client():
    app, registry = _app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac, registry


async def test_create_session_returns_id_and_ws_url(client) -> None:
    ac, _ = client
    resp = await ac.post("/api/sessions")
    assert resp.status_code == 200
    body = resp.json()
    assert "session_id" in body
    # ws_url is derived from the incoming Request's base_url when
    # VOCALIZE_WS_BASE_URL is not set. The test client uses
    # base_url="http://test", so the WS URL becomes ws://test/...
    assert body["ws_url"] == f"ws://test/ws/sessions/{body['session_id']}"
    assert body["default_lang"] == "zh"


async def test_post_sessions_accepts_preferred_voice_id_and_auto_translate(
    client,
) -> None:
    ac, _ = client
    resp = await ac.post(
        "/api/sessions",
        json={
            "preferred_voice_id": "voice-42",
            "auto_translate_merchant": False,
        },
    )
    assert resp.status_code == 200
    sid = resp.json()["session_id"]

    get_resp = await ac.get(f"/api/sessions/{sid}")
    assert get_resp.status_code == 200
    body = get_resp.json()
    assert body["preferred_voice_id"] == "voice-42"
    assert body["auto_translate_merchant"] is False


async def test_post_sessions_normalises_legacy_default_voice_id_to_none(
    client,
) -> None:
    ac, _ = client
    resp = await ac.post("/api/sessions", json={"preferred_voice_id": "default"})
    assert resp.status_code == 200
    sid = resp.json()["session_id"]

    get_resp = await ac.get(f"/api/sessions/{sid}")
    assert get_resp.status_code == 200
    assert get_resp.json()["preferred_voice_id"] is None


async def test_post_sessions_defaults_auto_translate_to_true(client) -> None:
    ac, _ = client
    resp = await ac.post("/api/sessions", json={})
    assert resp.status_code == 200
    sid = resp.json()["session_id"]

    get_resp = await ac.get(f"/api/sessions/{sid}")
    assert get_resp.status_code == 200
    body = get_resp.json()
    assert body["auto_translate_merchant"] is True
    assert body["preferred_voice_id"] is None


async def test_get_session_returns_snapshot(client) -> None:
    ac, registry = client
    s = registry.create()
    resp = await ac.get(f"/api/sessions/{s.session_id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["session_id"] == s.session_id
    assert body["task_description"] is None


async def test_get_session_includes_task_state_review_fields(client) -> None:
    ac, registry = client
    now = datetime(2026, 5, 7, 12, tzinfo=timezone.utc)
    session = registry.create()
    session.task_description = "demo"
    session.task_state = TaskState(
        session_id=session.session_id,
        user_task_description="demo",
        phase=TaskPhase.POST_CALL_REVIEW,
        uncertain_assumptions=[
            SlotAssumption(
                id="a-1",
                slot="party_size",
                question="How many?",
                assumed_value=4,
                source="user_timeout",
                created_at=now,
            )
        ],
        pending_callbacks=[
            CallbackEntry(
                id="cb-1",
                assumption_id="a-1",
                correction="6",
                created_at=now,
            )
        ],
    )

    resp = await ac.get(f"/api/sessions/{session.session_id}")

    assert resp.status_code == 200
    body = resp.json()
    assert body["phase"] == "post_call_review"
    assert body["uncertain_assumptions"] == [
        {
            "id": "a-1",
            "slot": "party_size",
            "question": "How many?",
            "assumed_value": 4,
            "source": "user_timeout",
            "created_at": "2026-05-07T12:00:00Z",
            "status": "pending_review",
            "correction": None,
            "note": None,
            "callback_id": None,
        }
    ]
    assert body["pending_callbacks"][0]["id"] == "cb-1"
    assert body["pending_callbacks"][0]["correction"] == "6"


async def test_create_session_sweep_keeps_post_call_review_sessions(client) -> None:
    ac, registry = client
    session = registry.create()
    session.task_description = "demo"
    session.task_state = TaskState(
        session_id=session.session_id,
        user_task_description="demo",
        phase=TaskPhase.POST_CALL_REVIEW,
    )
    session.last_active_at = time.monotonic() - 3600

    resp = await ac.post("/api/sessions")

    assert resp.status_code == 200
    assert registry.get(session.session_id) is session


def test_create_app_stashes_registry_on_app_state(monkeypatch) -> None:
    # D-11: startup raises when non-localhost host + no WS_BASE_URL. Set localhost
    # mode so this test can verify app.state.registry without triggering the guard.
    monkeypatch.setenv("VOCALIZE_HOST", "127.0.0.1")
    monkeypatch.delenv("VOCALIZE_WS_BASE_URL", raising=False)

    app = create_app()

    assert isinstance(app.state.registry, SessionRegistry)


async def test_get_unknown_session_returns_404(client) -> None:
    ac, _ = client
    resp = await ac.get("/api/sessions/does-not-exist")
    assert resp.status_code == 404


async def test_get_review_returns_trimmed_dto(client) -> None:
    ac, registry = client
    session = registry.create()
    session.task_state = _review_state(session.session_id)

    resp = await ac.get(f"/api/sessions/{session.session_id}/review")

    assert resp.status_code == 200
    body = resp.json()
    assert body["session_id"] == session.session_id
    assert body["slots"] == {"party_size": 4}
    assert body["completion_summary"] == "done"
    assert body["status"] == "interrupted"
    assert body["call_segments"][0]["transcript"][0]["id"] == "t-1"
    assert body["call_segments"][1]["transcript"][0]["id"] == "t-2"
    assert "audit_log" not in body
    assert "merchant_channel" not in body
    assert "raw_audio" not in body


async def test_get_review_404_on_unknown_session(client) -> None:
    ac, _ = client
    resp = await ac.get("/api/sessions/nope/review")
    assert resp.status_code == 404


@pytest.mark.parametrize(
    ("interrupt_reason", "audit_reason", "expected"),
    [
        ("ws_close", "merchant impatience escalation", "interrupted"),
        ("merchant_impatience", "ws disconnect", "escalated"),
        (None, "clarification timeout", "completed"),
        (None, "merchant impatience escalation", "escalated"),
    ],
)
async def test_get_review_status_derivation_rule_locked(
    client,
    interrupt_reason,
    audit_reason,
    expected,
) -> None:
    ac, registry = client
    session = registry.create()
    state = _review_state(session.session_id)
    state.call_segments[-1].interrupt_reason = interrupt_reason
    state.call_segments[-1].interrupted = interrupt_reason is not None
    state.audit_log.append(
        TaskAuditEntry(
            timestamp=time.monotonic(),
            from_phase=TaskPhase.EXECUTION_ACTIVE,
            to_phase=TaskPhase.POST_CALL_REVIEW,
            reason=audit_reason,
            evidence={},
        )
    )
    session.task_state = state

    resp = await ac.get(f"/api/sessions/{session.session_id}/review")

    assert resp.status_code == 200
    assert resp.json()["status"] == expected


async def test_get_review_status_defaults_completed_without_signals(client) -> None:
    ac, registry = client
    session = registry.create()
    session.task_state = TaskState(
        session_id=session.session_id,
        user_task_description="demo",
        phase=TaskPhase.POST_CALL_REVIEW,
    )

    resp = await ac.get(f"/api/sessions/{session.session_id}/review")

    assert resp.status_code == 200
    assert resp.json()["status"] == "completed"


async def test_post_confirm_assumption_mutates_and_returns_review_dto(client) -> None:
    ac, registry = client
    session = registry.create()
    session.task_state = _review_state(session.session_id)

    resp = await ac.post(
        f"/api/sessions/{session.session_id}/confirm_assumption",
        json={"assumption_id": "a-1", "confirmed_value": "6"},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["uncertain_assumptions"][0]["status"] == "corrected"
    assert body["uncertain_assumptions"][0]["confirmed_value"] == "6"
    again = await ac.get(f"/api/sessions/{session.session_id}/review")
    assert again.json()["uncertain_assumptions"][0]["confirmed_value"] == "6"


async def test_post_confirm_assumption_404_on_unknown_assumption_id(client) -> None:
    ac, registry = client
    session = registry.create()
    session.task_state = _review_state(session.session_id)

    resp = await ac.post(
        f"/api/sessions/{session.session_id}/confirm_assumption",
        json={"assumption_id": "missing", "confirmed_value": None},
    )

    assert resp.status_code == 404


async def test_post_callback_cancel_restore_and_trigger_mutations(client) -> None:
    ac, registry = client
    session = registry.create()
    session.task_state = _review_state(session.session_id)

    cancelled = await ac.post(
        f"/api/sessions/{session.session_id}/callbacks/cb-1/cancel",
    )
    assert cancelled.status_code == 200
    assert cancelled.json()["pending_callbacks"][0]["status"] == "cancelled"

    restored = await ac.post(
        f"/api/sessions/{session.session_id}/callbacks/cb-1/restore",
    )
    assert restored.status_code == 200
    assert restored.json()["pending_callbacks"][0]["status"] == "queued"

    triggered = await ac.post(
        f"/api/sessions/{session.session_id}/callbacks/cb-1/trigger",
    )
    assert triggered.status_code == 200
    assert triggered.json()["pending_callbacks"][0]["status"] == "triggered"
    assert session.task_state.audit_log[-1].evidence["source"] == (
        "standalone_review_rest"
    )


async def test_post_callback_restore_409_on_non_cancelled(client) -> None:
    ac, registry = client
    session = registry.create()
    session.task_state = _review_state(session.session_id)

    resp = await ac.post(
        f"/api/sessions/{session.session_id}/callbacks/cb-1/restore",
    )

    assert resp.status_code == 409


async def test_delete_session_removes_session(client) -> None:
    ac, registry = client
    session = registry.create()

    resp = await ac.delete(f"/api/sessions/{session.session_id}")

    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    assert registry.get(session.session_id) is None


async def test_delete_active_session_returns_409(client) -> None:
    ac, registry = client
    session = registry.create()
    assert registry.claim(session.session_id) is True

    resp = await ac.delete(f"/api/sessions/{session.session_id}")

    assert resp.status_code == 409
    assert registry.get(session.session_id) is session
    registry.release(session.session_id)


async def test_delete_unknown_session_returns_404(client) -> None:
    ac, _ = client

    resp = await ac.delete("/api/sessions/does-not-exist")

    assert resp.status_code == 404


async def test_post_task_persists_description(client) -> None:
    ac, registry = client
    s = registry.create()
    resp = await ac.post(
        f"/api/sessions/{s.session_id}/task",
        json={"task": "帮我订海底捞"},
    )
    assert resp.status_code == 200
    fetched = registry.get(s.session_id)
    assert fetched is not None
    assert fetched.task_description == "帮我订海底捞"


async def test_post_task_unknown_session_returns_404(client) -> None:
    ac, _ = client
    resp = await ac.post(
        "/api/sessions/nope/task",
        json={"task": "x"},
    )
    assert resp.status_code == 404


async def test_post_task_rejects_empty(client) -> None:
    ac, registry = client
    s = registry.create()
    resp = await ac.post(
        f"/api/sessions/{s.session_id}/task",
        json={"task": "  "},
    )
    assert resp.status_code == 422
