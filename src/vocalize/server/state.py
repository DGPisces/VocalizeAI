"""In-process session state for B1 server.

A session is created when the frontend hits ``POST /api/sessions`` and lives
until explicit dismissal or stale-session sweep. ``SessionRegistry`` is a thin
dict-of-dataclasses with a threading lock so concurrent REST and WS handlers
don't race each other.

Persistence (SQLite) is v1.x; that is why the registry isn't pluggable yet.
"""
from __future__ import annotations

import threading
import time as _time
import uuid
from dataclasses import dataclass, field
from typing import Literal

from vocalize.dialogue.state import TaskPhase, TaskState


@dataclass
class DeviceSelection:
    """Browser-owned device selection metadata recorded for observability."""

    input_id: str = ""
    output_id: str = ""
    aec: bool = True


@dataclass
class Session:
    """Per-WS-session record.

    ``default_lang`` is a placeholder hint for the frontend; the actual user
    language is detected from the first utterance / text input by the
    orchestrator. ``task_description`` is set by ``POST /sessions/{id}/task``
    before the WS connects.
    """

    session_id: str
    default_lang: Literal["zh", "en"] = "zh"
    task_description: str | None = None
    task_state: TaskState | None = None
    preferred_voice_id: str | None = None
    auto_translate_merchant: bool = True
    device_selection: DeviceSelection = field(default_factory=DeviceSelection)
    merchant_transcript_cache: dict[str, tuple[str, str]] = field(
        default_factory=dict
    )
    created_at: float = field(default_factory=_time.monotonic)
    last_active_at: float = field(default_factory=_time.monotonic)


class SessionRegistry:
    """Thread-safe in-memory session store.

    ``claim`` / ``release`` prevent concurrent WebSocket connections from
    opening the same session twice (e.g. two browser tabs, fast reconnect).
    """

    def __init__(self) -> None:
        self._sessions: dict[str, Session] = {}
        self._active: set[str] = set()
        self._lock = threading.Lock()

    def create(
        self,
        default_lang: Literal["zh", "en"] = "zh",
        *,
        preferred_voice_id: str | None = None,
        auto_translate_merchant: bool = True,
    ) -> Session:
        if preferred_voice_id == "default":
            preferred_voice_id = None
        session_id = uuid.uuid4().hex
        session = Session(
            session_id=session_id,
            default_lang=default_lang,
            preferred_voice_id=preferred_voice_id,
            auto_translate_merchant=auto_translate_merchant,
        )
        with self._lock:
            self._sessions[session_id] = session
        return session

    def get(self, session_id: str) -> Session | None:
        with self._lock:
            return self._sessions.get(session_id)

    def set_task(self, session_id: str, task: str) -> None:
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                raise KeyError(session_id)
            session.task_description = task
            session.last_active_at = _time.monotonic()

    def claim(self, session_id: str) -> bool:
        """Atomically mark a session as active. Returns False if already claimed."""
        with self._lock:
            if session_id not in self._sessions:
                return False
            if session_id in self._active:
                return False
            self._active.add(session_id)
            return True

    def release(self, session_id: str) -> None:
        """Release the active claim on a session. Idempotent."""
        with self._lock:
            self._active.discard(session_id)

    def is_active(self, session_id: str) -> bool:
        with self._lock:
            return session_id in self._active

    def remove(self, session_id: str) -> None:
        with self._lock:
            self._sessions.pop(session_id, None)
            self._active.discard(session_id)

    def touch(self, session_id: str) -> None:
        with self._lock:
            session = self._sessions.get(session_id)
            if session is not None:
                session.last_active_at = _time.monotonic()

    def sweep_stale(self, *, max_age_s: float) -> int:
        now = _time.monotonic()
        with self._lock:
            stale = [
                session_id
                for session_id, session in self._sessions.items()
                if (
                    session_id not in self._active
                    and (
                        session.task_state is None
                        or session.task_state.phase != TaskPhase.POST_CALL_REVIEW
                    )
                    and now - session.last_active_at > max_age_s
                )
            ]
            for session_id in stale:
                self._sessions.pop(session_id, None)
                self._active.discard(session_id)
        return len(stale)


__all__ = ["DeviceSelection", "Session", "SessionRegistry"]
