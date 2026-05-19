"""Tests for create_app() startup guards and env-conditional CORS (D-10, D-11).

Covers:
  1. Localhost host + no VOCALIZE_WS_BASE_URL → create_app() succeeds.
  2. Non-localhost host + no VOCALIZE_WS_BASE_URL → RuntimeError raised.
  3. Non-localhost host + VOCALIZE_WS_BASE_URL set → create_app() succeeds.
  4. Localhost mode: CORS preflight from http://localhost:3000 allowed.
  5. Localhost mode: CORS preflight from attacker origin rejected.
  6. CORS preflight response has explicit allow_methods and allow_headers.
  7. VOCALIZE_CORS_ORIGINS=* rejected at startup (WR-01 guard).
  8. VOCALIZE_CORS_ORIGINS with * mixed with real origins also rejected.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from vocalize.config import reset_config
from vocalize.server import create_app


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_client(monkeypatch, *, host: str, ws_base_url: str | None = None) -> TestClient:
    monkeypatch.setenv("VOCALIZE_HOST", host)
    if ws_base_url is not None:
        monkeypatch.setenv("VOCALIZE_WS_BASE_URL", ws_base_url)
    else:
        monkeypatch.delenv("VOCALIZE_WS_BASE_URL", raising=False)
    monkeypatch.delenv("VOCALIZE_INVITE_TOKEN", raising=False)
    monkeypatch.delenv("VOCALIZE_CORS_ORIGINS", raising=False)
    reset_config()
    return TestClient(create_app())


# ---------------------------------------------------------------------------
# Test 1: localhost + no WS_BASE_URL → succeeds
# ---------------------------------------------------------------------------

def test_localhost_without_ws_base_url_succeeds(monkeypatch):
    # The key assertion is that create_app() does NOT raise. We verify this by
    # constructing the client (which calls create_app()) without an exception.
    client = _make_client(monkeypatch, host="127.0.0.1")
    # GET /api/sessions/{id} with a non-existent id should 404; that's fine —
    # the route exists and is served (app started successfully).
    resp = client.get("/api/sessions/nonexistent-id")
    assert resp.status_code in {404}  # route exists; session doesn't — app started OK


# ---------------------------------------------------------------------------
# Test 2: Non-localhost + no WS_BASE_URL → RuntimeError
# ---------------------------------------------------------------------------

def test_non_localhost_without_ws_base_url_raises(monkeypatch):
    monkeypatch.setenv("VOCALIZE_HOST", "0.0.0.0")
    monkeypatch.delenv("VOCALIZE_WS_BASE_URL", raising=False)
    monkeypatch.delenv("VOCALIZE_INVITE_TOKEN", raising=False)
    monkeypatch.delenv("VOCALIZE_CORS_ORIGINS", raising=False)
    reset_config()
    with pytest.raises(RuntimeError, match="VOCALIZE_WS_BASE_URL is required"):
        create_app()


# ---------------------------------------------------------------------------
# Test 3: Non-localhost + WS_BASE_URL set → succeeds
# ---------------------------------------------------------------------------

def test_non_localhost_with_ws_base_url_succeeds(monkeypatch):
    client = _make_client(
        monkeypatch,
        host="0.0.0.0",
        ws_base_url="wss://vocalize-api.dgpisces.com",
    )
    # App started without RuntimeError; 404 here just means session not found.
    resp = client.get("/api/sessions/nonexistent-id")
    assert resp.status_code in {404}


# ---------------------------------------------------------------------------
# Test 4: localhost CORS — allowed origin passes
# ---------------------------------------------------------------------------

def test_cors_localhost_origin_allowed(monkeypatch):
    client = _make_client(monkeypatch, host="127.0.0.1")
    resp = client.options(
        "/api/sessions",
        headers={
            "Origin": "http://localhost:3000",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "X-Invite-Token",
        },
    )
    assert resp.status_code == 200
    assert resp.headers.get("access-control-allow-origin") == "http://localhost:3000"


# ---------------------------------------------------------------------------
# Test 5: localhost CORS — attacker origin is rejected (header absent)
# ---------------------------------------------------------------------------

def test_cors_attacker_origin_rejected(monkeypatch):
    client = _make_client(monkeypatch, host="127.0.0.1")
    resp = client.options(
        "/api/sessions",
        headers={
            "Origin": "https://attacker.example",
            "Access-Control-Request-Method": "POST",
        },
    )
    # CORSMiddleware silently omits the header for disallowed origins.
    assert "access-control-allow-origin" not in resp.headers


# ---------------------------------------------------------------------------
# Test 6: CORS preflight — allow_methods and allow_headers are explicit
# ---------------------------------------------------------------------------

def test_cors_preflight_returns_explicit_methods_and_headers(monkeypatch):
    client = _make_client(monkeypatch, host="127.0.0.1")
    resp = client.options(
        "/api/sessions",
        headers={
            "Origin": "http://localhost:3000",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "Content-Type, X-Invite-Token",
        },
    )
    assert resp.status_code == 200
    allow_methods = resp.headers.get("access-control-allow-methods", "")
    allow_headers = resp.headers.get("access-control-allow-headers", "")
    # Methods must contain GET, POST, DELETE but NOT *
    assert "GET" in allow_methods
    assert "POST" in allow_methods
    assert "DELETE" in allow_methods
    assert "*" not in allow_methods
    # Headers must include X-Invite-Token
    assert "X-Invite-Token" in allow_headers


# ---------------------------------------------------------------------------
# Test 7: VOCALIZE_CORS_ORIGINS=* → RuntimeError at startup (WR-01)
# ---------------------------------------------------------------------------

def test_cors_wildcard_origins_raises_at_startup(monkeypatch):
    monkeypatch.setenv("VOCALIZE_HOST", "127.0.0.1")
    monkeypatch.setenv("VOCALIZE_CORS_ORIGINS", "*")
    monkeypatch.delenv("VOCALIZE_WS_BASE_URL", raising=False)
    monkeypatch.delenv("VOCALIZE_INVITE_TOKEN", raising=False)
    reset_config()
    with pytest.raises(RuntimeError, match="VOCALIZE_CORS_ORIGINS must not contain"):
        create_app()


# ---------------------------------------------------------------------------
# Test 8: VOCALIZE_CORS_ORIGINS with * mixed in also rejected (WR-01)
# ---------------------------------------------------------------------------

def test_cors_wildcard_mixed_with_real_origins_raises(monkeypatch):
    monkeypatch.setenv("VOCALIZE_HOST", "0.0.0.0")
    monkeypatch.setenv("VOCALIZE_WS_BASE_URL", "wss://vocalize-api.dgpisces.com")
    monkeypatch.setenv("VOCALIZE_CORS_ORIGINS", "https://example.com,*")
    monkeypatch.delenv("VOCALIZE_INVITE_TOKEN", raising=False)
    reset_config()
    with pytest.raises(RuntimeError, match="VOCALIZE_CORS_ORIGINS must not contain"):
        create_app()
