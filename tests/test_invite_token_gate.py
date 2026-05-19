"""Tests for the X-Invite-Token gate on POST /api/sessions (D-08).

Covers:
  - Localhost-dev mode (no token configured): gate disabled, POST succeeds.
  - Production mode (token configured): gate enforces header presence + value.
  - SetTaskRequest.task length bound (max_length=2000).
  - Source assertion: _check_invite_token uses secrets.compare_digest.
"""
from __future__ import annotations

import inspect

import pytest
from fastapi.testclient import TestClient

from vocalize.config import reset_config
from vocalize.server import create_app


def _client(monkeypatch) -> TestClient:
    """Build a TestClient for the full production app.

    VOCALIZE_HOST is forced to 127.0.0.1 so create_app() does not raise the
    startup RuntimeError for missing VOCALIZE_WS_BASE_URL (Task 2).
    VOCALIZE_CORS_ORIGINS and VOCALIZE_WS_BASE_URL are cleared to prevent a
    CI environment with VOCALIZE_CORS_ORIGINS='*' from raising RuntimeError
    inside create_app() and masking the invite-token test failures.
    """
    monkeypatch.setenv("VOCALIZE_HOST", "127.0.0.1")
    monkeypatch.delenv("VOCALIZE_CORS_ORIGINS", raising=False)
    monkeypatch.delenv("VOCALIZE_WS_BASE_URL", raising=False)
    reset_config()
    return TestClient(create_app())


# ---------------------------------------------------------------------------
# Test 1: No token configured → localhost-dev gate disabled, POST succeeds
# ---------------------------------------------------------------------------

def test_no_token_configured_allows_session_creation(monkeypatch):
    monkeypatch.delenv("VOCALIZE_INVITE_TOKEN", raising=False)
    client = _client(monkeypatch)
    resp = client.post("/api/sessions")
    assert resp.status_code == 200
    assert "session_id" in resp.json()


# ---------------------------------------------------------------------------
# Test 2: Token configured, header missing → 401
# ---------------------------------------------------------------------------

def test_missing_header_returns_401_when_token_configured(monkeypatch):
    monkeypatch.setenv("VOCALIZE_INVITE_TOKEN", "abc123")
    client = _client(monkeypatch)
    resp = client.post("/api/sessions")
    assert resp.status_code == 401
    assert resp.json()["detail"] == "invalid or missing X-Invite-Token"


# ---------------------------------------------------------------------------
# Test 3: Token configured, wrong header value → 401
# ---------------------------------------------------------------------------

def test_wrong_header_value_returns_401(monkeypatch):
    monkeypatch.setenv("VOCALIZE_INVITE_TOKEN", "abc123")
    client = _client(monkeypatch)
    resp = client.post("/api/sessions", headers={"X-Invite-Token": "wrong-value"})
    assert resp.status_code == 401
    assert resp.json()["detail"] == "invalid or missing X-Invite-Token"


# ---------------------------------------------------------------------------
# Test 4: Token configured, correct header value → 200 + session_id
# ---------------------------------------------------------------------------

def test_correct_header_value_allows_session_creation(monkeypatch):
    monkeypatch.setenv("VOCALIZE_INVITE_TOKEN", "abc123")
    client = _client(monkeypatch)
    resp = client.post("/api/sessions", headers={"X-Invite-Token": "abc123"})
    assert resp.status_code == 200
    data = resp.json()
    assert "session_id" in data
    assert data["session_id"]


# ---------------------------------------------------------------------------
# Test 5: SetTaskRequest.task > 2000 chars → 422
# ---------------------------------------------------------------------------

def test_task_length_over_2000_returns_422(monkeypatch):
    monkeypatch.delenv("VOCALIZE_INVITE_TOKEN", raising=False)
    client = _client(monkeypatch)
    # Create a session first
    sess_resp = client.post("/api/sessions")
    assert sess_resp.status_code == 200
    session_id = sess_resp.json()["session_id"]

    oversized_task = "x" * 2001
    resp = client.post(
        f"/api/sessions/{session_id}/task",
        json={"task": oversized_task},
    )
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Test 6: SetTaskRequest.task == 2000 chars → 200
# ---------------------------------------------------------------------------

def test_task_length_exactly_2000_is_accepted(monkeypatch):
    monkeypatch.delenv("VOCALIZE_INVITE_TOKEN", raising=False)
    client = _client(monkeypatch)
    sess_resp = client.post("/api/sessions")
    assert sess_resp.status_code == 200
    session_id = sess_resp.json()["session_id"]

    max_task = "x" * 2000
    resp = client.post(
        f"/api/sessions/{session_id}/task",
        json={"task": max_task},
    )
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Test 7: Source assertion — _check_invite_token uses secrets.compare_digest
# ---------------------------------------------------------------------------

def test_check_invite_token_uses_timing_safe_comparison():
    from vocalize.server.sessions import _check_invite_token  # noqa: PLC0415

    source = inspect.getsource(_check_invite_token)
    assert "secrets.compare_digest" in source, (
        "_check_invite_token must use secrets.compare_digest for timing-safe "
        "comparison (T-04c-02)"
    )


# ---------------------------------------------------------------------------
# Test 8: Non-ASCII invite token → 401 (not 500) — regression for WR-04
# ---------------------------------------------------------------------------

def test_non_ascii_invite_token_returns_401_not_500(monkeypatch):
    """secrets.compare_digest raises TypeError when VOCALIZE_INVITE_TOKEN contains
    non-ASCII characters (e.g. a Unicode passphrase set by the operator).

    HTTP headers are ASCII-only, so the client always sends an ASCII (or empty)
    token value; the non-ASCII value lives in the expected side only.
    The gate must catch TypeError and return 401, not propagate a 500 that would
    increment vocalize_error_log_total toward the D-05 error budget (T-04c-02).
    """
    monkeypatch.setenv("VOCALIZE_INVITE_TOKEN", "パスワード123")
    client = _client(monkeypatch)
    # Send an ASCII token value; the server side holds the non-ASCII expected value.
    # compare_digest raises TypeError when either argument is non-ASCII.
    resp = client.post("/api/sessions", headers={"X-Invite-Token": "ascii-token"})
    assert resp.status_code == 401, (
        f"Expected 401 for non-ASCII configured token, got {resp.status_code}"
    )
    assert resp.json()["detail"] == "invalid or missing X-Invite-Token"
