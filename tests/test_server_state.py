"""SessionRegistry tests — in-process per-session state ownership for B1.

Persistence is out of scope (v1.x); the registry is a plain dict-of-dataclasses
with a lock for safe registration / retrieval / removal.
"""
from __future__ import annotations

import asyncio
import time

import pytest

from vocalize.dialogue.state import TaskPhase, TaskState
from vocalize.server.state import SessionRegistry


def test_create_session_returns_unique_ids() -> None:
    registry = SessionRegistry()
    s1 = registry.create()
    s2 = registry.create()
    assert s1.session_id != s2.session_id
    assert s1.default_lang == "zh"


def test_create_session_defaults_device_selection_metadata() -> None:
    registry = SessionRegistry()
    session = registry.create()

    assert session.device_selection.input_id == ""
    assert session.device_selection.output_id == ""
    assert session.device_selection.aec is True


def test_device_selection_contains_no_local_test_artifacts() -> None:
    registry = SessionRegistry()
    session = registry.create()

    assert not hasattr(session.device_selection, "recording")
    assert not hasattr(session.device_selection, "label")
    assert not hasattr(session.device_selection, "permission_status")
    assert not hasattr(session.device_selection, "test_result")


def test_get_session_by_id() -> None:
    registry = SessionRegistry()
    s = registry.create()
    fetched = registry.get(s.session_id)
    assert fetched is s


def test_get_unknown_session_returns_none() -> None:
    registry = SessionRegistry()
    assert registry.get("does-not-exist") is None


def test_set_task_description() -> None:
    registry = SessionRegistry()
    s = registry.create()
    registry.set_task(s.session_id, "帮我订海底捞")
    fetched = registry.get(s.session_id)
    assert fetched is not None
    assert fetched.task_description == "帮我订海底捞"


def test_set_task_unknown_session_raises_key_error() -> None:
    registry = SessionRegistry()
    with pytest.raises(KeyError):
        registry.set_task("nope", "x")


def test_remove_session() -> None:
    registry = SessionRegistry()
    s = registry.create()
    registry.remove(s.session_id)
    assert registry.get(s.session_id) is None


def test_remove_is_idempotent() -> None:
    registry = SessionRegistry()
    s = registry.create()
    registry.remove(s.session_id)
    registry.remove(s.session_id)  # MUST NOT raise


def test_claim_unknown_session_returns_false() -> None:
    registry = SessionRegistry()

    assert registry.claim("does-not-exist") is False


def test_touch_updates_last_active_at() -> None:
    registry = SessionRegistry()
    s = registry.create()
    before = s.last_active_at

    time.sleep(0.001)
    registry.touch(s.session_id)

    assert s.last_active_at > before


def test_sweep_stale_removes_inactive_sessions() -> None:
    registry = SessionRegistry()
    stale = registry.create()
    fresh = registry.create()
    stale.last_active_at = time.monotonic() - 3600

    removed = registry.sweep_stale(max_age_s=300)

    assert removed == 1
    assert registry.get(stale.session_id) is None
    assert registry.get(fresh.session_id) is fresh


def test_sweep_stale_keeps_active_sessions() -> None:
    registry = SessionRegistry()
    active = registry.create()
    inactive = registry.create()
    assert registry.claim(active.session_id) is True
    active.last_active_at = time.monotonic() - 3600
    inactive.last_active_at = time.monotonic() - 3600

    removed = registry.sweep_stale(max_age_s=300)

    assert removed == 1
    assert registry.get(active.session_id) is active
    assert registry.get(inactive.session_id) is None


def test_sweep_stale_keeps_post_call_review_sessions() -> None:
    registry = SessionRegistry()
    review = registry.create()
    stale = registry.create()
    review.task_state = TaskState(
        session_id=review.session_id,
        user_task_description="demo",
        phase=TaskPhase.POST_CALL_REVIEW,
    )
    review.last_active_at = time.monotonic() - 3600
    stale.last_active_at = time.monotonic() - 3600

    removed = registry.sweep_stale(max_age_s=300)

    assert removed == 1
    assert registry.get(review.session_id) is review
    assert registry.get(stale.session_id) is None


async def test_concurrent_creates_do_not_collide() -> None:
    registry = SessionRegistry()
    sessions = await asyncio.gather(*(asyncio.to_thread(registry.create) for _ in range(50)))
    ids = {s.session_id for s in sessions}
    assert len(ids) == 50
