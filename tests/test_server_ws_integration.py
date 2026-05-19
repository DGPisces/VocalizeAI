"""End-to-end WS handler tests.

Task 13 covers the framing layer in isolation (a fake orchestrator runner).
Task 14 swaps the fake out for the real DialogueOrchestrator wiring.
Task 17 adds a real-GPU integration variant gated on env vars.
"""
from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncIterator
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from vocalize.dialogue.state import TaskPhase, TaskState
from vocalize.server.runner import DialogueOrchestratorRunner, _translate_event
from vocalize.server.sessions import register_session_routes
from vocalize.server.state import SessionRegistry
from vocalize.server.ws import OrchestratorRunner, register_ws_routes


class _EchoRunner:
    """Test runner that records what the channel + transport surface up
    from the WS frame layer, and emits one canned ``state_update`` event
    when ``run`` starts.

    Contract pinned here:
    1. Server creates one runner per WS open.
    2. ``run(channel, transport)`` is awaited until the connection closes.
    3. ``text_input`` frames are routed by the server to the channel's
       text-input queue; we observe them via ``channel.receive_text``.
    4. ``ack_clarification`` frames are routed to the channel's ack queue;
       we don't exercise them here (Task 15 covers the round-trip).
    5. Other text frames (mode_change / hangup / set_devices) are buffered
       in ``runner.text_frames`` for the runner's own dispatch loop.
    6. Binary frames go to the transport's inbound audio queue; we observe
       them via ``transport.input_stream``.
    """

    def __init__(self) -> None:
        self.text_frames: list[str] = []  # mode_change / hangup / set_devices
        self.text_inputs: list[tuple[str, str | None]] = []  # via channel
        self.takeover_inputs: list[tuple[str, str]] = []  # via takeover queue
        self.merchant_hints: list[tuple[str, str]] = []  # via hint queue
        self.audio_blocks: list[bytes] = []  # via transport
        self.started = asyncio.Event()
        self.stop = asyncio.Event()
        self._hint_q: asyncio.Queue | None = None
        self._takeover_q: asyncio.Queue | None = None

    def attach_session_queues(
        self,
        *,
        merchant_hint_queue: asyncio.Queue,
        user_takeover_queue: asyncio.Queue,
    ) -> None:
        self._hint_q = merchant_hint_queue
        self._takeover_q = user_takeover_queue

    async def run(self, *, channel: Any, transport: Any) -> None:
        await channel.push_event({
            "event": "state_update",
            "diff": {"phase": "task_planning"},
        })
        self.started.set()

        async def _consume_text() -> None:
            while True:
                try:
                    text, lang = await channel.receive_text()
                except Exception:
                    return
                self.text_inputs.append((text, lang))

        async def _consume_audio() -> None:
            async for block in transport.input_stream():
                self.audio_blocks.append(block)

        async def _consume_hints() -> None:
            if self._hint_q is None:
                return
            while True:
                try:
                    self.merchant_hints.append(await self._hint_q.get())
                except Exception:
                    return

        async def _consume_takeover() -> None:
            if self._takeover_q is None:
                return
            while True:
                try:
                    self.takeover_inputs.append(await self._takeover_q.get())
                except Exception:
                    return

        tasks = [
            asyncio.create_task(_consume_text()),
            asyncio.create_task(_consume_audio()),
            asyncio.create_task(_consume_hints()),
            asyncio.create_task(_consume_takeover()),
        ]
        try:
            await self.stop.wait()
        finally:
            for t in tasks:
                t.cancel()
            for t in tasks:
                try:
                    await t
                except (asyncio.CancelledError, Exception):
                    pass


class _RawTextQueueRunner:
    """Probe runner for ws.py text_input queue payload shape."""

    def __init__(self) -> None:
        self.text_frames: list[str] = []
        self.raw_text_inputs: list[tuple[Any, ...]] = []
        self.started = asyncio.Event()
        self.stop = asyncio.Event()

    def attach_session_queues(
        self,
        *,
        merchant_hint_queue: asyncio.Queue,
        user_takeover_queue: asyncio.Queue,
    ) -> None:
        return None

    async def run(self, *, channel: Any, transport: Any) -> None:
        await channel.push_event({
            "event": "state_update",
            "diff": {"phase": "task_planning"},
        })
        self.started.set()

        async def _consume_raw_text() -> None:
            while True:
                try:
                    raw = await channel._text_q.get()
                except Exception:
                    return
                self.raw_text_inputs.append(raw)

        text_task = asyncio.create_task(_consume_raw_text())
        try:
            await self.stop.wait()
        finally:
            text_task.cancel()
            try:
                await text_task
            except (asyncio.CancelledError, Exception):
                pass


class _AttachedQueueRunner:
    """Runner that proves ws.py/channel route into attached session queues.

    It deliberately does not call ``channel.receive_text()``; that keeps the
    D2 tests from passing only because a fake runner drained ``text_input_q``.
    """

    def __init__(self) -> None:
        self.text_frames: list[str] = []
        self.takeover_inputs: list[tuple[str, str]] = []
        self.merchant_hints: list[tuple[str, str]] = []
        self.started = asyncio.Event()
        self.stop = asyncio.Event()
        self._hint_q: asyncio.Queue | None = None
        self._takeover_q: asyncio.Queue | None = None

    def attach_session_queues(
        self,
        *,
        merchant_hint_queue: asyncio.Queue,
        user_takeover_queue: asyncio.Queue,
    ) -> None:
        self._hint_q = merchant_hint_queue
        self._takeover_q = user_takeover_queue

    async def run(self, *, channel: Any, transport: Any) -> None:
        await channel.push_event({
            "event": "state_update",
            "diff": {"phase": "task_planning"},
        })
        self.started.set()

        async def _consume_hints() -> None:
            assert self._hint_q is not None
            while True:
                self.merchant_hints.append(await self._hint_q.get())

        async def _consume_takeover() -> None:
            assert self._takeover_q is not None
            while True:
                self.takeover_inputs.append(await self._takeover_q.get())

        tasks = [
            asyncio.create_task(_consume_hints()),
            asyncio.create_task(_consume_takeover()),
        ]
        try:
            await self.stop.wait()
        finally:
            for task in tasks:
                task.cancel()
            for task in tasks:
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass


def _build_app(runner: OrchestratorRunner | None = None) -> FastAPI:
    app = FastAPI()
    registry = SessionRegistry()
    register_session_routes(app, registry=registry)
    register_ws_routes(
        app,
        registry=registry,
        runner_factory=lambda session: runner or _EchoRunner(),
    )
    s = registry.create()
    app.state.session_id = s.session_id  # type: ignore[attr-defined]
    app.state.registry = registry  # type: ignore[attr-defined]
    return app


def _wait_until(predicate) -> None:
    import time as _t

    for _ in range(50):
        if predicate():
            return
        _t.sleep(0.01)


def test_ws_unknown_session_closes_with_4404() -> None:
    app = _build_app()
    with TestClient(app) as tc:
        # Starlette TestClient accepts the WS connection; the server then
        # closes immediately with code 4404. receive_json/text/bytes all
        # raise WebSocketDisconnect when the remote close is delivered.
        with tc.websocket_connect("/ws/sessions/does-not-exist") as ws:
            with pytest.raises(Exception):
                ws.receive_json()


def test_ws_accepts_session_and_streams_state_update() -> None:
    app = _build_app()
    sid = app.state.session_id  # type: ignore[attr-defined]
    with TestClient(app) as tc:
        with tc.websocket_connect(f"/ws/sessions/{sid}") as ws:
            msg = ws.receive_json()
            assert msg == {"type": "state_update", "diff": {"phase": "task_planning"}}


def test_ws_routes_text_input_through_channel() -> None:
    """``text_input`` frames must reach the channel's text queue (which
    the runner consumes via ``channel.receive_text``), NOT the runner's
    raw-frame buffer used for mode_change/hangup/set_devices.
    """
    import time as _t

    runner = _EchoRunner()
    app = _build_app(runner=runner)
    sid = app.state.session_id  # type: ignore[attr-defined]
    with TestClient(app) as tc:
        with tc.websocket_connect(f"/ws/sessions/{sid}") as ws:
            ws.receive_json()  # state_update
            ws.send_text(json.dumps({"type": "text_input", "text": "hi", "lang_hint": "en"}))
            for _ in range(50):
                if runner.text_inputs:
                    break
                _t.sleep(0.01)
            assert runner.text_inputs == [("hi", "en")]
            assert runner.text_frames == []  # text_input MUST NOT leak here


def test_ws_routes_binary_through_transport() -> None:
    """Binary frames must reach the transport's inbound queue (which the
    runner consumes via ``transport.input_stream``).
    """
    import time as _t

    runner = _EchoRunner()
    app = _build_app(runner=runner)
    sid = app.state.session_id  # type: ignore[attr-defined]
    with TestClient(app) as tc:
        with tc.websocket_connect(f"/ws/sessions/{sid}") as ws:
            ws.receive_json()
            ws.send_bytes(b"\x10\x20\x30\x40")
            for _ in range(50):
                if runner.audio_blocks:
                    break
                _t.sleep(0.01)
            assert runner.audio_blocks == [b"\x10\x20\x30\x40"]


def test_ws_routes_mode_change_to_runner_text_frames() -> None:
    """``mode_change`` is one of the frame types the WS handler buffers
    on the runner for its own dispatch loop (the WS handler does not
    have semantic context to act on it). This test pins that contract.
    """
    import time as _t

    runner = _EchoRunner()
    app = _build_app(runner=runner)
    sid = app.state.session_id  # type: ignore[attr-defined]
    with TestClient(app) as tc:
        with tc.websocket_connect(f"/ws/sessions/{sid}") as ws:
            ws.receive_json()
            payload = json.dumps({"type": "mode_change", "mode": "preflight"})
            ws.send_text(payload)
            for _ in range(50):
                if runner.text_frames:
                    break
                _t.sleep(0.01)
            assert runner.text_frames == [payload]


def test_ws_routes_trigger_callback_to_runner_text_frames() -> None:
    runner = _EchoRunner()
    app = _build_app(runner=runner)
    sid = app.state.session_id  # type: ignore[attr-defined]
    with TestClient(app) as tc:
        with tc.websocket_connect(f"/ws/sessions/{sid}") as ws:
            ws.receive_json()
            payload = json.dumps({"type": "trigger_callback", "callback_id": "cb-1"})
            ws.send_text(payload)
            _wait_until(lambda: bool(runner.text_frames))
            assert runner.text_frames == [payload]


def test_ws_routes_confirm_assumption_to_runner_text_frames() -> None:
    runner = _EchoRunner()
    app = _build_app(runner=runner)
    sid = app.state.session_id  # type: ignore[attr-defined]
    with TestClient(app) as tc:
        with tc.websocket_connect(f"/ws/sessions/{sid}") as ws:
            ws.receive_json()
            payload = json.dumps({
                "type": "confirm_assumption",
                "assumption_id": "a-1",
                "choice": "correct",
                "correction": None,
            })
            ws.send_text(payload)
            _wait_until(lambda: bool(runner.text_frames))
            assert runner.text_frames == [payload]


def test_ws_routes_set_auto_translate_to_runner_text_frames() -> None:
    runner = _EchoRunner()
    app = _build_app(runner=runner)
    sid = app.state.session_id  # type: ignore[attr-defined]
    with TestClient(app) as tc:
        with tc.websocket_connect(f"/ws/sessions/{sid}") as ws:
            ws.receive_json()
            payload = json.dumps({"type": "set_auto_translate", "value": False})
            ws.send_text(payload)
            _wait_until(lambda: bool(runner.text_frames))
            assert runner.text_frames == [payload]


def test_ws_routes_on_demand_translate_to_runner_text_frames() -> None:
    runner = _EchoRunner()
    app = _build_app(runner=runner)
    sid = app.state.session_id  # type: ignore[attr-defined]
    with TestClient(app) as tc:
        with tc.websocket_connect(f"/ws/sessions/{sid}") as ws:
            ws.receive_json()
            payload = json.dumps({
                "type": "on_demand_translate",
                "transcript_id": "t-42",
            })
            ws.send_text(payload)
            _wait_until(lambda: bool(runner.text_frames))
            assert runner.text_frames == [payload]


def test_ws_routes_merchant_text_inject_to_runner_text_frames() -> None:
    runner = _EchoRunner()
    app = _build_app(runner=runner)
    sid = app.state.session_id  # type: ignore[attr-defined]
    with TestClient(app) as tc:
        with tc.websocket_connect(f"/ws/sessions/{sid}") as ws:
            ws.receive_json()
            payload = json.dumps({
                "type": "merchant_text_inject",
                "text": "Hello, how can I help?",
                "scenario_id": "handover-readiness",
                "seed": "merchant-direct",
                "lang_hint": "en",
            })
            ws.send_text(payload)
            _wait_until(lambda: bool(runner.text_frames))
            assert runner.text_frames == [payload]
            assert runner.text_inputs == []
            assert runner.merchant_hints == []


def test_ws_routes_text_input_with_mode_to_channel_queue() -> None:
    runner = _RawTextQueueRunner()
    app = _build_app(runner=runner)
    sid = app.state.session_id  # type: ignore[attr-defined]
    with TestClient(app) as tc:
        with tc.websocket_connect(f"/ws/sessions/{sid}") as ws:
            ws.receive_json()
            ws.send_text(json.dumps({
                "type": "text_input",
                "text": "yes",
                "mode": "user_takeover",
            }))
            _wait_until(lambda: bool(runner.raw_text_inputs))
            assert runner.raw_text_inputs == [("yes", None, "user_takeover")]
            assert runner.text_frames == []


def test_user_takeover_text_reaches_runner_takeover_queue() -> None:
    runner = _AttachedQueueRunner()
    app = _build_app(runner=runner)
    sid: str = app.state.session_id  # type: ignore[attr-defined]
    registry: SessionRegistry = app.state.registry  # type: ignore[attr-defined]
    session = registry.get(sid)
    assert session is not None
    session.task_state = TaskState(
        session_id=sid,
        user_task_description="t",
        phase=TaskPhase.EXECUTION_ACTIVE,
    )

    with TestClient(app) as tc:
        with tc.websocket_connect(f"/ws/sessions/{sid}") as ws:
            ws.receive_json()
            ws.send_text(json.dumps({
                "type": "text_input",
                "text": "yes",
                "mode": "user_takeover",
            }))
            _wait_until(lambda: bool(runner.takeover_inputs))
            assert runner.takeover_inputs == [("yes", "en")]
            assert runner.merchant_hints == []


def test_default_in_call_text_reaches_runner_merchant_hint_queue() -> None:
    runner = _AttachedQueueRunner()
    app = _build_app(runner=runner)
    sid: str = app.state.session_id  # type: ignore[attr-defined]
    registry: SessionRegistry = app.state.registry  # type: ignore[attr-defined]
    session = registry.get(sid)
    assert session is not None
    session.task_state = TaskState(
        session_id=sid,
        user_task_description="t",
        phase=TaskPhase.EXECUTION_ACTIVE,
    )

    with TestClient(app) as tc:
        with tc.websocket_connect(f"/ws/sessions/{sid}") as ws:
            ws.receive_json()
            ws.send_text(json.dumps({
                "type": "text_input",
                "text": "they have a private room",
                "lang_hint": "en",
            }))
            _wait_until(lambda: bool(runner.merchant_hints))
            assert runner.merchant_hints == [("they have a private room", "en")]
            assert runner.takeover_inputs == []


def test_default_ready_to_dial_text_reaches_runner_merchant_hint_queue() -> None:
    runner = _AttachedQueueRunner()
    app = _build_app(runner=runner)
    sid: str = app.state.session_id  # type: ignore[attr-defined]
    registry: SessionRegistry = app.state.registry  # type: ignore[attr-defined]
    session = registry.get(sid)
    assert session is not None
    session.task_state = TaskState(
        session_id=sid,
        user_task_description="t",
        phase=TaskPhase.READY_TO_DIAL,
    )

    with TestClient(app) as tc:
        with tc.websocket_connect(f"/ws/sessions/{sid}") as ws:
            ws.receive_json()
            ws.send_text(json.dumps({
                "type": "text_input",
                "text": "actually make it tomorrow",
                "lang_hint": "en",
            }))
            _wait_until(lambda: bool(runner.merchant_hints))
            assert runner.merchant_hints == [("actually make it tomorrow", "en")]
            assert runner.takeover_inputs == []


def test_default_clarification_text_reaches_runner_merchant_hint_queue() -> None:
    runner = _AttachedQueueRunner()
    app = _build_app(runner=runner)
    sid: str = app.state.session_id  # type: ignore[attr-defined]
    registry: SessionRegistry = app.state.registry  # type: ignore[attr-defined]
    session = registry.get(sid)
    assert session is not None
    session.task_state = TaskState(
        session_id=sid,
        user_task_description="t",
        phase=TaskPhase.NEEDS_CLARIFICATION,
    )

    with TestClient(app) as tc:
        with tc.websocket_connect(f"/ws/sessions/{sid}") as ws:
            ws.receive_json()
            ws.send_text(json.dumps({
                "type": "text_input",
                "text": "tell them I can wait 10 minutes",
                "lang_hint": "en",
            }))
            _wait_until(lambda: bool(runner.merchant_hints))
            assert runner.merchant_hints == [
                ("tell them I can wait 10 minutes", "en")
            ]
            assert runner.takeover_inputs == []


def test_ws_close_retains_session_for_post_call_review() -> None:
    """WS close releases the active claim but retains the Session record.

    Starlette's TestClient runs the async app in the same event loop.
    We set ``runner.stop`` and then call ``ws.receive()`` to pump the
    loop so the handler processes the stop and exits. Afterward the
    PostCallReview REST view must still be able to fetch the live state.
    """
    runner = _EchoRunner()
    app = _build_app(runner=runner)
    sid = app.state.session_id  # type: ignore[attr-defined]
    registry: SessionRegistry = app.state.registry  # type: ignore[attr-defined]
    session = registry.get(sid)
    assert session is not None
    session.task_state = TaskState(
        session_id=sid,
        user_task_description="demo",
        phase=TaskPhase.POST_CALL_REVIEW,
    )
    assert registry.get(sid) is not None

    with TestClient(app) as tc:
        with tc.websocket_connect(f"/ws/sessions/{sid}") as ws:
            ws.receive_json()
            assert registry.get(sid) is not None
            runner.stop.set()
            # Pump the event loop so the handler processes the stop event
            # and runs its cleanup finally block.
            try:
                ws.receive()
            except Exception:
                pass  # WS closed during cleanup — expected

    retained = registry.get(sid)
    assert retained is not None
    assert retained.task_state is not None
    assert retained.task_state.phase is TaskPhase.POST_CALL_REVIEW
    with TestClient(app) as tc:
        resp = tc.get(f"/api/sessions/{sid}")
        assert resp.status_code == 200
        assert resp.json()["phase"] == "post_call_review"

    with TestClient(app) as tc:
        with tc.websocket_connect(f"/ws/sessions/{sid}") as ws:
            ws.receive_json()
            runner.stop.set()
            try:
                ws.receive()
            except Exception:
                pass


def test_ws_runner_factory_error_releases_claim_but_retains_session() -> None:
    """Robustness: if ``runner_factory(session)`` raises before the
    runner starts, the WS handler still releases the active claim. Session
    deletion remains explicit via DELETE / sweep.
    """
    import time as _t

    from fastapi import FastAPI
    from vocalize.server.state import SessionRegistry
    from vocalize.server.ws import register_ws_routes

    registry = SessionRegistry()
    s = registry.create()
    sid = s.session_id

    def boom(session):
        raise RuntimeError("simulated runner_factory failure")

    app = FastAPI()
    register_ws_routes(app, registry=registry, runner_factory=boom)

    with TestClient(app) as tc:
        try:
            with tc.websocket_connect(f"/ws/sessions/{sid}"):
                pass
        except Exception:
            pass

    _t.sleep(0.05)
    assert registry.get(sid) is not None

    ok_runner = _EchoRunner()
    app = FastAPI()
    register_ws_routes(app, registry=registry, runner_factory=lambda _s: ok_runner)
    with TestClient(app) as tc:
        with tc.websocket_connect(f"/ws/sessions/{sid}") as ws:
            assert ws.receive_json() == {
                "type": "state_update",
                "diff": {"phase": "task_planning"},
            }
            ok_runner.stop.set()
            try:
                ws.receive()
            except Exception:
                pass


def test_ws_cleanup_close_attribute_error_releases_claim(monkeypatch) -> None:
    """Regression: uvicorn/websockets can raise AttributeError while closing
    an already-ended browser WS. Cleanup must still release the active claim.
    """
    from starlette.websockets import WebSocket

    runner = _EchoRunner()
    app = _build_app(runner=runner)
    sid: str = app.state.session_id  # type: ignore[attr-defined]
    registry: SessionRegistry = app.state.registry  # type: ignore[attr-defined]

    async def close_raises_attribute_error(self, *args, **kwargs) -> None:
        raise AttributeError(
            "'WebSocketProtocol' object has no attribute 'transfer_data_task'"
        )

    monkeypatch.setattr(WebSocket, "close", close_raises_attribute_error)

    with TestClient(app) as tc:
        try:
            with tc.websocket_connect(f"/ws/sessions/{sid}") as ws:
                ws.receive_json()
                assert registry.is_active(sid) is True
        except Exception:
            pass

    _wait_until(lambda: not registry.is_active(sid))
    assert registry.get(sid) is not None
    assert registry.is_active(sid) is False


# -- Task 14: Orchestrator wiring tests ---------------------------------------


def test_translate_event_readiness_passed_reads_verdict_nested_fields() -> None:
    """Orchestrator emits readiness_passed with verdict nested under
    "verdict": {missing_critical, confidence, override}. The translator
    must descend into verdict to surface the real values; reading
    top-level keys silently misreports preflight to the frontend.
    """
    out = _translate_event({
        "event": "readiness_passed",
        "verdict": {
            "missing_critical": ["party_size"],
            "confidence": 0.62,
            "override": False,
        },
    })
    assert out == {
        "event": "readiness_change",
        "passed": True,
        "missing_critical": ["party_size"],
        "confidence": 0.62,
    }


def test_translate_event_failed_maps_to_error() -> None:
    out = _translate_event({
        "event": "failed",
        "stage": "preflight",
        "error": "no LLM",
    })
    assert out["event"] == "error"
    assert out["code"] == 2000
    assert "preflight" in out["message_zh"]
    assert "preflight" in out["message_en"]


def test_translate_event_unknown_falls_back_to_state_update() -> None:
    raw = {"event": "task_planning_started"}
    out = _translate_event(raw)
    assert out == {"event": "state_update", "diff": raw}


def test_translate_event_lifecycle_events_become_state_update() -> None:
    """Spot-check the orchestrator events the runner forwards. A future
    contributor adding a new orchestrator event sees this test fail and
    decides whether it deserves dedicated mapping.
    """
    for kind in (
        "task_planning_started",
        "preflight_started",
        "turn_complete",
        "tool_dispatched",
        "clarification_started",
        "clarification_failed",
        "clarification_resolved",
        "transition",
        "completed",
    ):
        raw = {"event": kind, "extra": "x"}
        out = _translate_event(raw)
        assert out == {"event": "state_update", "diff": raw}, f"failed for {kind}"


def _make_bounded_receiver(ws, *, mode: str = "text"):
    """Build a getter that returns the next JSON frame within a timeout.

    starlette's ``WebSocketTestSession.receive_json`` blocks indefinitely
    if the server emits nothing — a ``while deadline`` loop only checks
    time *between* receives, so a single stalled receive can hang the
    test forever.

    Returns a callable ``get(timeout: float) -> dict | None`` — ``None``
    on timeout or once the receive thread has signalled end-of-stream
    (an exception during ``ws.receive_json``, e.g. WS closed).
    """
    import queue
    import threading

    q: queue.Queue = queue.Queue()
    _STOP = object()

    def _pump() -> None:
        while True:
            try:
                msg = ws.receive_json(mode=mode)
            except Exception:
                q.put(_STOP)
                return
            q.put(msg)

    t = threading.Thread(target=_pump, daemon=True)
    t.start()

    def get(timeout: float):
        try:
            item = q.get(timeout=timeout)
        except queue.Empty:
            return None
        if item is _STOP:
            return None
        return item

    return get


def _make_bounded_message_receiver(ws):
    """Receive raw WS messages from a background thread with a timeout.

    ``receive_json`` cannot observe binary audio frames, and ``receive_bytes``
    would skip control frames. Integration tests that assert mixed protocol
    traffic use this helper to keep each wait bounded.
    """
    import queue
    import threading

    q: queue.Queue = queue.Queue()
    _STOP = object()

    def _pump() -> None:
        while True:
            try:
                msg = ws.receive()
            except Exception:
                q.put(_STOP)
                return
            q.put(msg)

    t = threading.Thread(target=_pump, daemon=True)
    t.start()

    def get(timeout: float):
        try:
            item = q.get(timeout=timeout)
        except queue.Empty:
            return None
        if item is _STOP:
            return None
        return item

    return get


def _ws_message_payload(msg: dict[str, Any]) -> dict[str, Any] | None:
    if msg.get("text") is not None:
        return {"kind": "json", "frame": json.loads(msg["text"])}
    if msg.get("bytes") is not None:
        raw = msg["bytes"]
        role = "ai_to_merchant" if raw[:1] == b"M" else "ai_to_user"
        return {"kind": "audio", "role": role, "pcm": raw[1:]}
    return None


def _drain_mixed_until(get_message, *, target, timeout_s: float) -> list[dict[str, Any]]:
    import time as _t

    seen: list[dict[str, Any]] = []
    deadline = _t.monotonic() + timeout_s
    while _t.monotonic() < deadline:
        msg = get_message(timeout=0.05)
        if msg is None:
            continue
        payload = _ws_message_payload(msg)
        if payload is None:
            continue
        seen.append(payload)
        if target(payload):
            return seen
    kinds = [
        item.get("frame", {}).get("type", item.get("kind"))
        for item in seen
    ]
    raise AssertionError(f"target frame not seen within {timeout_s}s; got: {kinds}")


class _AudioBlockSTT:
    async def stream_transcribe(
        self,
        audio_chunks: AsyncIterator[bytes],
        **_kwargs: Any,
    ):
        from vocalize.stt.base import Transcript

        utterance_id = 0
        async for block in audio_chunks:
            utterance_id += 1
            yield Transcript(
                text=block.decode("utf-8"),
                is_final=True,
                confidence=1.0,
                start_time=0.0,
                end_time=0.0,
                utterance_id=utterance_id,
                language="en",
            )


class _IdleSTT:
    async def stream_transcribe(
        self,
        audio_chunks: AsyncIterator[bytes],
        **_kwargs: Any,
    ):
        if False:
            from vocalize.stt.base import Transcript

            yield Transcript(
                text="",
                is_final=True,
                confidence=0.0,
                start_time=0.0,
                end_time=0.0,
                utterance_id=0,
                language="en",
            )


class _TextEchoTTS:
    output_sample_rate = 24000
    output_encoding = "pcm_s16le"

    def __init__(self) -> None:
        self.synthesized: list[str] = []

    async def stream_synthesize(self, chunks):
        text = ""
        async for chunk in chunks:
            text += chunk.text
        self.synthesized.append(text)
        if text:
            yield f"tts:{text}".encode("utf-8")


def _build_real_runner_app(scripted_llm) -> FastAPI:
    from vocalize.pipeline import VoicePipeline
    from vocalize.server.state import SessionRegistry
    from vocalize.server.ws import register_ws_routes

    app = FastAPI()
    app.state.registry = registry = SessionRegistry()
    app.state.user_tts = _TextEchoTTS()
    app.state.merchant_tts = _TextEchoTTS()
    app.state.merchant_stt = _AudioBlockSTT()

    def user_pipeline_factory(transport):
        return VoicePipeline(
            transport=transport,
            stt=_IdleSTT(),
            llm=scripted_llm,
            tts=app.state.user_tts,
            system_prompt="",
            default_language="en",
        )

    def merchant_pipeline_factory(transport):
        return VoicePipeline(
            transport=transport,
            stt=app.state.merchant_stt,
            llm=scripted_llm,
            tts=app.state.merchant_tts,
            system_prompt="",
            default_language="en",
        )

    def runner_factory(session):
        runner = DialogueOrchestratorRunner(
            session=session,
            user_pipeline_factory=user_pipeline_factory,
            merchant_pipeline_factory=merchant_pipeline_factory,
        )
        app.state.last_runner = runner
        return runner

    register_ws_routes(app, registry=registry, runner_factory=runner_factory)
    return app


def _create_task_session(app: FastAPI, task_text: str):
    session = app.state.registry.create()
    app.state.registry.set_task(session.session_id, task_text)
    return session


def _planner_tool_names(tools: Any) -> set[str]:
    return {getattr(tool, "name", "") for tool in (tools or [])}


@pytest.mark.skipif(
    "REAL_GPU" in os.environ,
    reason="REAL_GPU mode runs the variant in test_real_gpu_smoke",
)
def test_runner_drives_orchestrator_through_one_text_turn(
    fake_voice_pipeline_factory,
    monkeypatch,
) -> None:
    """End-to-end smoke: open WS, post task, send one text_input, observe
    state_update / transcript_update / readiness_change come back within a
    bounded per-receive deadline.

    Real LLM / STT / TTS are stubbed via the fake pipeline; this proves the
    framing + orchestration plumbing is correct without hitting the network.
    """
    import time as _t

    from fastapi import FastAPI

    from vocalize.server.state import SessionRegistry
    from vocalize.server.ws import register_ws_routes

    DEADLINE_S = 5.0

    registry = SessionRegistry()
    s = registry.create()
    registry.set_task(s.session_id, "帮我订海底捞")

    def runner_factory(session):
        return DialogueOrchestratorRunner(
            session=session,
            user_pipeline_factory=fake_voice_pipeline_factory,
            merchant_pipeline_factory=fake_voice_pipeline_factory,
        )

    app = FastAPI()
    register_ws_routes(app, registry=registry, runner_factory=runner_factory)

    with TestClient(app) as tc:
        with tc.websocket_connect(f"/ws/sessions/{s.session_id}") as ws:
            ws.send_text(json.dumps({
                "type": "text_input",
                "text": "晚上7点4个人",
                "lang_hint": "zh",
            }))

            recv = _make_bounded_receiver(ws)
            seen_types: list[str] = []
            deadline = _t.monotonic() + DEADLINE_S
            while _t.monotonic() < deadline:
                remaining = max(0.05, deadline - _t.monotonic())
                msg = recv(timeout=remaining)
                if msg is None:
                    break
                seen_types.append(msg["type"])
                if msg["type"] == "readiness_change":
                    break
            assert "state_update" in seen_types, (
                f"no state_update within {DEADLINE_S}s; saw {seen_types}"
            )


def test_runner_waits_for_call_listening_before_merchant_execution() -> None:
    import time as _t

    from fastapi import FastAPI
    from tests.conftest import make_scripted_llm
    from tests.test_dialogue_orchestrator import _task_planner_script
    from tests.test_pipeline import FakeSTT, FakeTTS
    from vocalize.pipeline import VoicePipeline
    from vocalize.server.state import SessionRegistry
    from vocalize.server.ws import register_ws_routes

    registry = SessionRegistry()
    s = registry.create()
    registry.set_task(s.session_id, "帮我订海底捞")

    llm = make_scripted_llm(_task_planner_script())

    def user_pipeline_factory(transport):
        return VoicePipeline(
            transport=transport,
            stt=FakeSTT([]),
            llm=llm,
            tts=FakeTTS([]),
            system_prompt="",
        )

    def merchant_pipeline_factory(transport):
        return VoicePipeline(
            transport=transport,
            stt=FakeSTT([]),
            llm=llm,
            tts=FakeTTS([]),
            system_prompt="",
        )

    def runner_factory(session):
        return DialogueOrchestratorRunner(
            session=session,
            user_pipeline_factory=user_pipeline_factory,
            merchant_pipeline_factory=merchant_pipeline_factory,
        )

    app = FastAPI()
    register_ws_routes(app, registry=registry, runner_factory=runner_factory)

    with TestClient(app) as tc:
        with tc.websocket_connect(f"/ws/sessions/{s.session_id}") as ws:
            ws.send_text(json.dumps({
                "type": "text_input",
                "text": "现在打吧",
                "lang_hint": "zh",
            }))
            recv = _make_bounded_receiver(ws)
            seen: list[dict[str, object]] = []
            deadline = _t.monotonic() + 5.0
            while _t.monotonic() < deadline:
                msg = recv(timeout=0.25)
                if msg is None:
                    continue
                seen.append(msg)
                if msg["type"] == "readiness_change":
                    break

            assert any(m["type"] == "readiness_change" for m in seen)

            linger_deadline = _t.monotonic() + 0.5
            while _t.monotonic() < linger_deadline:
                msg = recv(timeout=0.05)
                if msg is None:
                    continue
                seen.append(msg)
                assert not (
                    msg["type"] == "state_update"
                    and msg.get("diff", {}).get("to") == "EXECUTION_ACTIVE"
                )

            ws.send_text(json.dumps({"type": "mode_change", "mode": "call_listening"}))
            mode_ack = recv(timeout=2.0)
            assert mode_ack == {"type": "mode_ack", "mode": "call_listening"}


def test_runner_drops_pre_handover_audio_before_merchant_stt() -> None:
    import threading
    import time as _t

    from fastapi import FastAPI
    from tests.conftest import make_scripted_llm
    from tests.test_dialogue_orchestrator import _task_planner_script
    from tests.test_pipeline import FakeSTT, FakeTTS
    from vocalize.pipeline import VoicePipeline
    from vocalize.server.state import SessionRegistry
    from vocalize.server.ws import register_ws_routes

    class FirstBlockMerchantSTT:
        def __init__(self) -> None:
            self.blocks: list[bytes] = []
            self.ready = threading.Event()

        async def stream_transcribe(self, audio_chunks, **_kwargs):
            async for block in audio_chunks:
                self.blocks.append(block)
                self.ready.set()
                return
            if False:
                yield None

    registry = SessionRegistry()
    s = registry.create()
    registry.set_task(s.session_id, "帮我订海底捞")

    llm = make_scripted_llm(_task_planner_script())
    merchant_stt = FirstBlockMerchantSTT()

    def user_pipeline_factory(transport):
        return VoicePipeline(
            transport=transport,
            stt=FakeSTT([]),
            llm=llm,
            tts=FakeTTS([]),
            system_prompt="",
        )

    def merchant_pipeline_factory(transport):
        return VoicePipeline(
            transport=transport,
            stt=merchant_stt,
            llm=llm,
            tts=FakeTTS([]),
            system_prompt="",
        )

    def runner_factory(session):
        return DialogueOrchestratorRunner(
            session=session,
            user_pipeline_factory=user_pipeline_factory,
            merchant_pipeline_factory=merchant_pipeline_factory,
        )

    app = FastAPI()
    register_ws_routes(app, registry=registry, runner_factory=runner_factory)

    with TestClient(app) as tc:
        with tc.websocket_connect(f"/ws/sessions/{s.session_id}") as ws:
            ws.send_text(json.dumps({
                "type": "text_input",
                "text": "现在打吧",
                "lang_hint": "zh",
            }))
            recv = _make_bounded_receiver(ws)
            deadline = _t.monotonic() + 5.0
            while _t.monotonic() < deadline:
                msg = recv(timeout=0.25)
                if msg is not None and msg["type"] == "readiness_change":
                    break
            else:
                pytest.fail("readiness_change not received")

            ws.send_bytes(b"STALE-PREFLIGHT")
            _t.sleep(0.05)
            ws.send_text(json.dumps({"type": "mode_change", "mode": "call_listening"}))
            mode_ack = recv(timeout=2.0)
            assert mode_ack == {"type": "mode_ack", "mode": "call_listening"}

            assert merchant_stt.ready.wait(0.2) is False

            ws.send_bytes(b"FRESH-CALL")
            assert merchant_stt.ready.wait(2.0) is True

    assert merchant_stt.blocks == [b"FRESH-CALL"]


def test_runner_ignores_call_listening_before_readiness_passes() -> None:
    import threading
    import time as _t

    from fastapi import FastAPI
    from tests.conftest import make_scripted_llm
    from tests.test_dialogue_orchestrator import _task_planner_script
    from tests.test_pipeline import FakeSTT, FakeTTS
    from vocalize.pipeline import VoicePipeline
    from vocalize.server.state import SessionRegistry
    from vocalize.server.ws import register_ws_routes

    class FirstBlockMerchantSTT:
        def __init__(self) -> None:
            self.blocks: list[bytes] = []
            self.ready = threading.Event()

        async def stream_transcribe(self, audio_chunks, **_kwargs):
            async for block in audio_chunks:
                self.blocks.append(block)
                self.ready.set()
                return
            if False:
                yield None

    registry = SessionRegistry()
    s = registry.create()
    registry.set_task(s.session_id, "帮我订海底捞")

    llm = make_scripted_llm(_task_planner_script())
    merchant_stt = FirstBlockMerchantSTT()

    def user_pipeline_factory(transport):
        return VoicePipeline(
            transport=transport,
            stt=FakeSTT([]),
            llm=llm,
            tts=FakeTTS([]),
            system_prompt="",
        )

    def merchant_pipeline_factory(transport):
        return VoicePipeline(
            transport=transport,
            stt=merchant_stt,
            llm=llm,
            tts=FakeTTS([]),
            system_prompt="",
        )

    app = FastAPI()
    register_ws_routes(
        app,
        registry=registry,
        runner_factory=lambda session: DialogueOrchestratorRunner(
            session=session,
            user_pipeline_factory=user_pipeline_factory,
            merchant_pipeline_factory=merchant_pipeline_factory,
        ),
    )

    with TestClient(app) as tc:
        with tc.websocket_connect(f"/ws/sessions/{s.session_id}") as ws:
            recv = _make_bounded_receiver(ws)
            ws.send_text(json.dumps({"type": "mode_change", "mode": "call_listening"}))
            early = None
            deadline = _t.monotonic() + 2.0
            while _t.monotonic() < deadline:
                msg = recv(timeout=0.25)
                if msg is not None and msg["type"] in ("error", "mode_ack"):
                    early = msg
                    break
            assert early is not None
            assert early["type"] == "error"
            assert early["code"] == 1002

            ws.send_text(json.dumps({
                "type": "text_input",
                "text": "现在打吧",
                "lang_hint": "zh",
            }))

            deadline = _t.monotonic() + 5.0
            while _t.monotonic() < deadline:
                msg = recv(timeout=0.25)
                if msg is not None and msg["type"] == "readiness_change":
                    break
            else:
                pytest.fail("readiness_change not received")

            ws.send_bytes(b"POST-READINESS-BEFORE-HANDOVER")
            assert merchant_stt.ready.wait(1.0) is False

            ws.send_text(json.dumps({"type": "mode_change", "mode": "call_listening"}))
            mode_ack = recv(timeout=2.0)
            assert mode_ack == {"type": "mode_ack", "mode": "call_listening"}

            ws.send_bytes(b"FRESH-CALL")
            assert merchant_stt.ready.wait(2.0) is True

    assert merchant_stt.blocks == [b"FRESH-CALL"]


def test_ws_integration_text_input_default_reaches_merchant_hint_queue() -> None:
    from tests.test_dialogue_orchestrator import _task_planner_script
    from vocalize.llm.base import FinishChunk, TextDelta

    captured_user_messages: list[str] = []

    class _RecordingLLM:
        async def stream_chat(self, messages=None, tools=None, **kwargs):
            messages = messages if messages is not None else kwargs["messages"]
            if "emit_task_schema" in _planner_tool_names(tools):
                for chunk in _task_planner_script():
                    yield chunk
                return
            user_msg = next(
                (m for m in reversed(messages) if getattr(m, "role", None) == "user"),
                None,
            )
            if user_msg is not None:
                captured_user_messages.append(user_msg.content)
            yield TextDelta(text="ok")
            yield FinishChunk(reason="stop")

    app = _build_real_runner_app(_RecordingLLM())
    session = _create_task_session(app, "call a restaurant")

    received: list[dict[str, Any]] = []
    with TestClient(app) as tc:
        with tc.websocket_connect(f"/ws/sessions/{session.session_id}") as ws:
            recv = _make_bounded_message_receiver(ws)
            ws.send_text(json.dumps({
                "type": "text_input",
                "text": "call now",
                "lang_hint": "en",
            }))
            received.extend(_drain_mixed_until(
                recv,
                target=lambda item: (
                    item.get("kind") == "json"
                    and item["frame"].get("type") == "readiness_change"
                ),
                timeout_s=5.0,
            ))
            ws.send_text(json.dumps({"type": "mode_change", "mode": "call_listening"}))
            received.extend(_drain_mixed_until(
                recv,
                target=lambda item: (
                    item.get("kind") == "json"
                    and item["frame"] == {
                        "type": "mode_ack",
                        "mode": "call_listening",
                    }
                ),
                timeout_s=2.0,
            ))
            received.extend(_drain_mixed_until(
                recv,
                target=lambda item: (
                    item.get("kind") == "json"
                    and item["frame"].get("type") == "phase_change"
                    and item["frame"].get("current") == "execution_active"
                ),
                timeout_s=2.0,
            ))
            ws.send_text(json.dumps({
                "type": "text_input",
                "text": "they have a private room",
                "lang_hint": "en",
            }))
            received.extend(_drain_mixed_until(
                recv,
                target=lambda item: (
                    item.get("kind") == "json"
                    and item["frame"].get("type") == "transcript_update"
                    and item["frame"].get("subtype") == "user_supplement"
                ),
                timeout_s=2.0,
            ))

            ws.send_bytes(b"Hi, what would you like?")
            received.extend(_drain_mixed_until(
                recv,
                target=lambda _item: bool(captured_user_messages),
                timeout_s=2.0,
            ))

    assert captured_user_messages, f"merchant LLM never called; got {received[-10:]}"
    assert "[USER HINT" in captured_user_messages[-1]
    assert "private room" in captured_user_messages[-1]


def test_ws_integration_cross_lingual_transcript_pair() -> None:
    from tests.test_dialogue_orchestrator import _task_planner_script
    from vocalize.llm.base import FinishChunk, TextDelta

    class _RelayLLM:
        async def stream_chat(self, messages=None, tools=None, **kwargs):
            messages = messages if messages is not None else kwargs["messages"]
            if "emit_task_schema" in _planner_tool_names(tools):
                for chunk in _task_planner_script():
                    yield chunk
                return
            if tools is None:
                yield TextDelta(text="你好")
                yield FinishChunk(reason="stop")
                return
            yield TextDelta(text="ok")
            yield FinishChunk(reason="stop")

    app = _build_real_runner_app(_RelayLLM())
    session = _create_task_session(app, "帮我打电话")

    received: list[dict[str, Any]] = []
    with TestClient(app) as tc:
        with tc.websocket_connect(f"/ws/sessions/{session.session_id}") as ws:
            recv = _make_bounded_message_receiver(ws)
            ws.send_text(json.dumps({
                "type": "text_input",
                "text": "现在打吧",
                "lang_hint": "zh",
            }))
            received.extend(_drain_mixed_until(
                recv,
                target=lambda item: (
                    item.get("kind") == "json"
                    and item["frame"].get("type") == "readiness_change"
                ),
                timeout_s=5.0,
            ))
            app.state.last_runner._session.task_state.merchant_lang = "en"

            ws.send_text(json.dumps({"type": "mode_change", "mode": "call_listening"}))
            received.extend(_drain_mixed_until(
                recv,
                target=lambda item: (
                    item.get("kind") == "json"
                    and item["frame"] == {
                        "type": "mode_ack",
                        "mode": "call_listening",
                    }
                ),
                timeout_s=2.0,
            ))
            received.extend(_drain_mixed_until(
                recv,
                target=lambda item: (
                    item.get("kind") == "json"
                    and item["frame"].get("type") == "phase_change"
                    and item["frame"].get("current") == "execution_active"
                ),
                timeout_s=2.0,
            ))

            ws.send_bytes(b"Hello there")
            saw_original = False
            saw_translation = False

            def saw_pair(item: dict[str, Any]) -> bool:
                nonlocal saw_original, saw_translation
                if item.get("kind") != "json":
                    return False
                frame = item["frame"]
                saw_original = saw_original or (
                    frame.get("subtype") == "original"
                    and frame.get("role") == "merchant_to_ai"
                )
                saw_translation = saw_translation or (
                    frame.get("subtype") == "translation"
                )
                return saw_original and saw_translation

            received.extend(_drain_mixed_until(
                recv,
                target=saw_pair,
                timeout_s=3.0,
            ))

    json_frames = [
        item["frame"] for item in received if item.get("kind") == "json"
    ]
    originals = [
        frame for frame in json_frames
        if frame.get("subtype") == "original"
        and frame.get("role") == "merchant_to_ai"
    ]
    translations = [
        frame for frame in json_frames
        if frame.get("subtype") == "translation"
    ]
    assert len(originals) == 1
    assert len(translations) == 1
    assert translations[0]["parent_id"] == originals[0]["id"]
    assert translations[0]["lang"] == "zh"
    assert translations[0]["text"] == "你好"


def test_ws_integration_same_lang_no_translation() -> None:
    from tests.test_dialogue_orchestrator import _task_planner_script
    from vocalize.llm.base import FinishChunk, TextDelta

    non_planner_calls = 0

    class _CountingLLM:
        async def stream_chat(self, messages=None, tools=None, **kwargs):
            nonlocal non_planner_calls
            if "emit_task_schema" in _planner_tool_names(tools):
                for chunk in _task_planner_script():
                    yield chunk
                return
            non_planner_calls += 1
            yield TextDelta(text="ok")
            yield FinishChunk(reason="stop")

    app = _build_real_runner_app(_CountingLLM())
    session = _create_task_session(app, "帮我打电话")

    received: list[dict[str, Any]] = []
    with TestClient(app) as tc:
        with tc.websocket_connect(f"/ws/sessions/{session.session_id}") as ws:
            recv = _make_bounded_message_receiver(ws)
            ws.send_text(json.dumps({
                "type": "text_input",
                "text": "现在打吧",
                "lang_hint": "zh",
            }))
            received.extend(_drain_mixed_until(
                recv,
                target=lambda item: (
                    item.get("kind") == "json"
                    and item["frame"].get("type") == "readiness_change"
                ),
                timeout_s=5.0,
            ))
            ws.send_text(json.dumps({"type": "mode_change", "mode": "call_listening"}))
            received.extend(_drain_mixed_until(
                recv,
                target=lambda item: (
                    item.get("kind") == "json"
                    and item["frame"] == {
                        "type": "mode_ack",
                        "mode": "call_listening",
                    }
                ),
                timeout_s=2.0,
            ))
            received.extend(_drain_mixed_until(
                recv,
                target=lambda item: (
                    item.get("kind") == "json"
                    and item["frame"].get("type") == "phase_change"
                    and item["frame"].get("current") == "execution_active"
                ),
                timeout_s=2.0,
            ))

            ws.send_bytes("你好".encode("utf-8"))
            received.extend(_drain_mixed_until(
                recv,
                target=lambda item: (
                    item.get("kind") == "json"
                    and item["frame"].get("subtype") == "original"
                    and item["frame"].get("role") == "merchant_to_ai"
                ),
                timeout_s=2.0,
            ))
            import time as _t

            deadline = _t.monotonic() + 0.3
            while _t.monotonic() < deadline:
                msg = recv(timeout=0.05)
                if msg is not None:
                    payload = _ws_message_payload(msg)
                    if payload is not None:
                        received.append(payload)

    json_frames = [
        item["frame"] for item in received if item.get("kind") == "json"
    ]
    translations = [
        frame for frame in json_frames
        if frame.get("subtype") == "translation"
    ]
    assert translations == []
    assert non_planner_calls == 1


def test_ws_integration_full_callback_chain(monkeypatch: pytest.MonkeyPatch) -> None:
    from tests.conftest import make_scripted_llm
    from tests.test_dialogue_orchestrator import _task_planner_script, _tool_call_chunks
    from vocalize.llm.base import FinishChunk, TextDelta, ToolCallDelta

    async def instant_timeout(
        self,
        prompt: str,
        lang: str,
        timeout_s: float,
        field: str | None = None,
    ):
        raise asyncio.TimeoutError("scripted timeout")

    monkeypatch.setattr(
        "vocalize.dialogue.user_channel.WebSocketUserChannel.request_clarification",
        instant_timeout,
    )

    llm = make_scripted_llm(
        _task_planner_script(),
        _tool_call_chunks(
            0,
            "call_clarify",
            "request_user_clarification",
            {
                "field_name": "party_size",
                "question_text": "How many people?",
                "target_lang": "en",
                "urgency": "normal",
            },
        ),
        [TextDelta(text="I will follow up later."), FinishChunk(reason="stop")],
        [TextDelta(text="Sorry, actually 6 people."), FinishChunk(reason="stop")],
        [
            ToolCallDelta(
                tool_call_index=0,
                tool_call_id="call_finalize",
                name="finalize_task",
                arguments_delta=json.dumps({"success": True}),
            ),
            FinishChunk(reason="tool_calls"),
        ],
    )
    app = _build_real_runner_app(llm)
    session = _create_task_session(app, "call a restaurant")

    received: list[dict[str, Any]] = []
    with TestClient(app) as tc:
        with tc.websocket_connect(f"/ws/sessions/{session.session_id}") as ws:
            recv = _make_bounded_message_receiver(ws)
            ws.send_text(json.dumps({
                "type": "text_input",
                "text": "call now",
                "lang_hint": "en",
            }))
            received.extend(_drain_mixed_until(
                recv,
                target=lambda item: (
                    item.get("kind") == "json"
                    and item["frame"].get("type") == "readiness_change"
                ),
                timeout_s=5.0,
            ))
            ws.send_text(json.dumps({"type": "mode_change", "mode": "call_listening"}))
            received.extend(_drain_mixed_until(
                recv,
                target=lambda item: (
                    item.get("kind") == "json"
                    and item["frame"] == {
                        "type": "mode_ack",
                        "mode": "call_listening",
                    }
                ),
                timeout_s=2.0,
            ))
            received.extend(_drain_mixed_until(
                recv,
                target=lambda item: (
                    item.get("kind") == "json"
                    and item["frame"].get("type") == "phase_change"
                    and item["frame"].get("current") == "execution_active"
                ),
                timeout_s=2.0,
            ))

            ws.send_bytes(b"How many people?")
            received.extend(_drain_mixed_until(
                recv,
                target=lambda item: (
                    item.get("kind") == "json"
                    and item["frame"].get("type") == "uncertain_assumption_added"
                ),
                timeout_s=3.0,
            ))
            assumption = next(
                item["frame"] for item in received
                if item.get("kind") == "json"
                and item["frame"].get("type") == "uncertain_assumption_added"
            )
            assumption_id = assumption["assumption"]["id"]

            ws.send_text(json.dumps({"type": "hangup"}))
            received.extend(_drain_mixed_until(
                recv,
                target=lambda item: (
                    item.get("kind") == "json"
                    and item["frame"].get("type") == "phase_change"
                    and item["frame"].get("current") == "post_call_review"
                ),
                timeout_s=2.0,
            ))

            ws.send_text(json.dumps({
                "type": "confirm_assumption",
                "assumption_id": assumption_id,
                "choice": "wrong",
                "correction": "6",
                "note": None,
            }))
            received.extend(_drain_mixed_until(
                recv,
                target=lambda item: (
                    item.get("kind") == "json"
                    and item["frame"].get("type") == "pending_callback_added"
                ),
                timeout_s=2.0,
            ))
            callback_frame = next(
                item["frame"] for item in received
                if item.get("kind") == "json"
                and item["frame"].get("type") == "pending_callback_added"
            )
            callback_id = callback_frame["callback"]["id"]

            ws.send_text(json.dumps({
                "type": "trigger_callback",
                "callback_id": callback_id,
            }))
            saw_callback_segment = False

            def saw_callback(item: dict[str, Any]) -> bool:
                nonlocal saw_callback_segment
                if item.get("kind") != "json":
                    return False
                frame = item["frame"]
                saw_callback_segment = saw_callback_segment or (
                    frame.get("type") == "transcript_update"
                    and frame.get("subtype") == "callback_segment"
                )
                return saw_callback_segment

            received.extend(_drain_mixed_until(
                recv,
                target=saw_callback,
                timeout_s=3.0,
            ))
            ws.send_bytes(b"OK noted")

            def callback_finished(item: dict[str, Any]) -> bool:
                nonlocal saw_callback_segment
                if item.get("kind") != "json":
                    return False
                frame = item["frame"]
                saw_callback_segment = saw_callback_segment or (
                    frame.get("type") == "transcript_update"
                    and frame.get("subtype") == "callback_segment"
                )
                return (
                    saw_callback_segment
                    and frame.get("type") == "phase_change"
                    and frame.get("current") == "post_call_review"
                )

            received.extend(_drain_mixed_until(
                recv,
                target=callback_finished,
                timeout_s=3.0,
            ))

    json_frames = [
        item["frame"] for item in received if item.get("kind") == "json"
    ]
    callback_segments = [
        frame for frame in json_frames
        if frame.get("type") == "transcript_update"
        and frame.get("subtype") == "callback_segment"
    ]
    assert callback_segments
    segment_ids = {frame["segment_id"] for frame in callback_segments}
    assert len(segment_ids) == 1
    assert any(
        frame.get("type") == "phase_change"
        and frame.get("current") == "post_call_review"
        for frame in json_frames
    )


def test_ws_integration_merchant_ai_audio_suppressed_during_user_takeover() -> None:
    import time as _t

    from tests.conftest import make_scripted_llm
    from tests.test_dialogue_orchestrator import _task_planner_script
    from vocalize.llm.base import FinishChunk, TextDelta

    llm = make_scripted_llm(
        _task_planner_script(),
        [TextDelta(text="AI-QUEUED-DURING-TAKEOVER"), FinishChunk(reason="stop")],
        [TextDelta(text="AI-FRESH-AFTER-TAKEOVER"), FinishChunk(reason="stop")],
    )
    app = _build_real_runner_app(llm)
    session = _create_task_session(app, "call a restaurant")

    received: list[dict[str, Any]] = []
    with TestClient(app) as tc:
        with tc.websocket_connect(f"/ws/sessions/{session.session_id}") as ws:
            recv = _make_bounded_message_receiver(ws)
            ws.send_text(json.dumps({
                "type": "text_input",
                "text": "call now",
                "lang_hint": "en",
            }))
            received.extend(_drain_mixed_until(
                recv,
                target=lambda item: (
                    item.get("kind") == "json"
                    and item["frame"].get("type") == "readiness_change"
                ),
                timeout_s=5.0,
            ))

            ws.send_text(json.dumps({"type": "mode_change", "mode": "call_listening"}))
            received.extend(_drain_mixed_until(
                recv,
                target=lambda item: (
                    item.get("kind") == "json"
                    and item["frame"] == {
                        "type": "mode_ack",
                        "mode": "call_listening",
                    }
                ),
                timeout_s=2.0,
            ))
            received.extend(_drain_mixed_until(
                recv,
                target=lambda item: (
                    item.get("kind") == "json"
                    and item["frame"].get("type") == "phase_change"
                    and item["frame"].get("current") == "execution_active"
                ),
                timeout_s=2.0,
            ))

            ws.send_text(json.dumps({"type": "mode_change", "mode": "user_takeover"}))
            received.extend(_drain_mixed_until(
                recv,
                target=lambda item: (
                    item.get("kind") == "json"
                    and item["frame"] == {
                        "type": "mode_ack",
                        "mode": "user_takeover",
                    }
                ),
                timeout_s=2.0,
            ))

            ws.send_text(json.dumps({
                "type": "text_input",
                "text": "USER-TYPED-TO-MERCHANT",
                "lang_hint": "en",
                "mode": "user_takeover",
            }))
            received.extend(_drain_mixed_until(
                recv,
                target=lambda item: (
                    item.get("kind") == "audio"
                    and item.get("role") == "ai_to_merchant"
                    and item.get("pcm") == b"tts:USER-TYPED-TO-MERCHANT"
                ),
                timeout_s=2.0,
            ))

            ws.send_bytes(b"merchant turn one")
            runner = app.state.last_runner
            deadline = _t.monotonic() + 0.3
            while _t.monotonic() < deadline:
                msg = recv(timeout=0.05)
                if msg is not None:
                    payload = _ws_message_payload(msg)
                    if payload is not None:
                        received.append(payload)
            assert runner._pending_ai_outputs == []
            takeover_audio = [
                item for item in received
                if item.get("kind") == "audio"
                and item.get("role") == "ai_to_merchant"
            ]
            assert all(
                b"AI-QUEUED-DURING-TAKEOVER" not in item["pcm"]
                for item in takeover_audio
            )

            ws.send_text(json.dumps({"type": "mode_change", "mode": "call_listening"}))
            received.extend(_drain_mixed_until(
                recv,
                target=lambda item: (
                    item.get("kind") == "json"
                    and item["frame"] == {
                        "type": "mode_ack",
                        "mode": "call_listening",
                    }
                ),
                timeout_s=2.0,
            ))
            assert runner._pending_ai_outputs == []

            ws.send_bytes(b"merchant turn two")
            received.extend(_drain_mixed_until(
                recv,
                target=lambda item: (
                    item.get("kind") == "audio"
                    and item.get("role") == "ai_to_merchant"
                    and item.get("pcm") == b"tts:AI-FRESH-AFTER-TAKEOVER"
                ),
                timeout_s=2.0,
            ))

    merchant_audio = [
        item["pcm"] for item in received
        if item.get("kind") == "audio"
        and item.get("role") == "ai_to_merchant"
    ]
    assert b"tts:AI-FRESH-AFTER-TAKEOVER" in merchant_audio
    assert all(b"AI-QUEUED-DURING-TAKEOVER" not in pcm for pcm in merchant_audio)


# -- Task 15: Clarification round-trip + outbound audio tests -------------------


def test_ws_clarification_round_trip(fake_voice_pipeline_factory) -> None:
    """A clarification_request frame from server → client triggers an
    ack_clarification frame from client → server, which the channel
    surfaces to the orchestrator as a ClarificationReply.

    We drive the round-trip via a tiny custom runner that exercises the
    channel directly, since wiring a full clarification through the real
    orchestrator would require fixtures from Plan A scenarios.
    """
    from fastapi import FastAPI
    from vocalize.dialogue.user_channel import ClarificationReply
    from vocalize.server.state import SessionRegistry
    from vocalize.server.ws import register_ws_routes

    registry = SessionRegistry()
    s = registry.create()

    received_reply: list[ClarificationReply] = []

    class _ClarifyRunner:
        text_frames: list[str] = []
        audio_blocks: list[bytes] = []
        stop = asyncio.Event()

        def attach_session_queues(
            self,
            *,
            merchant_hint_queue: asyncio.Queue,
            user_takeover_queue: asyncio.Queue,
        ) -> None:
            return None

        async def run(self, *, channel, transport):
            reply = await channel.request_clarification(
                prompt="一共几位？",
                lang="zh",
                timeout_s=5.0,
            )
            received_reply.append(reply)

    app = FastAPI()
    register_ws_routes(app, registry=registry, runner_factory=lambda _: _ClarifyRunner())

    with TestClient(app) as tc:
        with tc.websocket_connect(f"/ws/sessions/{s.session_id}") as ws:
            req = ws.receive_json()
            assert req["type"] == "clarification_request"
            assert req["question"] == "一共几位？"
            ws.send_text(json.dumps({"type": "ack_clarification", "slot_value": "三位"}))
            import time as _t
            for _ in range(20):
                if received_reply:
                    break
                _t.sleep(0.05)

    assert len(received_reply) == 1
    assert received_reply[0].answer == "三位"


def test_ws_outbound_audio_is_role_prefixed() -> None:
    from fastapi import FastAPI
    from vocalize.server.state import SessionRegistry
    from vocalize.server.ws import register_ws_routes

    registry = SessionRegistry()
    s = registry.create()

    class _AudioRunner:
        text_frames: list[str] = []
        audio_blocks: list[bytes] = []
        stop = asyncio.Event()

        def attach_session_queues(
            self,
            *,
            merchant_hint_queue: asyncio.Queue,
            user_takeover_queue: asyncio.Queue,
        ) -> None:
            return None

        async def run(self, *, channel, transport):
            transport.set_outbound_role("ai_to_merchant")

            async def gen():
                yield b"\xaa\xbb"
                yield b"\xcc"

            await transport.output_stream(gen())

    app = FastAPI()
    register_ws_routes(app, registry=registry, runner_factory=lambda _: _AudioRunner())

    with TestClient(app) as tc:
        with tc.websocket_connect(f"/ws/sessions/{s.session_id}") as ws:
            chunk1 = ws.receive_bytes()
            chunk2 = ws.receive_bytes()
            assert chunk1 == b"M\xaa\xbb"
            assert chunk2 == b"M\xcc"


def test_runner_configures_web_channel_audio_io(fake_voice_pipeline_factory) -> None:
    from vocalize.dialogue.user_channel import WebSocketUserChannel
    from vocalize.server.state import Session
    from vocalize.transports.web import WebUserTransport

    session = Session(session_id="s", task_description="帮我订海底捞")
    runner = DialogueOrchestratorRunner(
        session=session,
        user_pipeline_factory=fake_voice_pipeline_factory,
        merchant_pipeline_factory=fake_voice_pipeline_factory,
    )

    configured: dict[str, object] = {}

    class RecordingChannel(WebSocketUserChannel):
        def configure_audio_io(self, *, transport, stt, tts):
            configured["transport"] = transport
            configured["stt"] = stt
            configured["tts"] = tts

    async def send_json(_frame):
        return None

    channel = RecordingChannel(
        send_json=send_json,
        text_input_queue=asyncio.Queue(),
        ack_clarification_queue=asyncio.Queue(),
    )
    transport = WebUserTransport(
        inbound_queue=asyncio.Queue(),
        outbound_send=lambda role, pcm: asyncio.sleep(0),
    )

    async def _run_and_cancel():
        task = asyncio.create_task(runner.run(channel=channel, transport=transport))
        for _ in range(50):
            if configured:
                runner.stop.set()
                break
            await asyncio.sleep(0.01)
        await asyncio.wait_for(task, timeout=2.0)

    asyncio.run(_run_and_cancel())

    assert configured["transport"].__class__.__name__ == "_RoleTaggedTransport"
    assert configured["stt"].__class__.__name__ == "FakeSTT"
    assert configured["tts"].__class__.__name__ == "FakeTTS"


def test_runner_configures_web_channel_phase_getter(
    fake_voice_pipeline_factory,
) -> None:
    from vocalize.dialogue.state import TaskPhase
    from vocalize.dialogue.user_channel import WebSocketUserChannel
    from vocalize.server.state import Session
    from vocalize.transports.web import WebUserTransport

    session = Session(
        session_id="s",
        task_description="帮我订海底捞",
        preferred_voice_id="voice-42",
        auto_translate_merchant=False,
    )
    runner = DialogueOrchestratorRunner(
        session=session,
        user_pipeline_factory=fake_voice_pipeline_factory,
        merchant_pipeline_factory=fake_voice_pipeline_factory,
    )

    configured: dict[str, object] = {}

    class RecordingChannel(WebSocketUserChannel):
        def configure_phase_getter(self, get_phase):
            configured["get_phase"] = get_phase
            runner.stop.set()

    async def send_json(_frame):
        return None

    channel = RecordingChannel(
        send_json=send_json,
        text_input_queue=asyncio.Queue(),
        ack_clarification_queue=asyncio.Queue(),
    )
    transport = WebUserTransport(
        inbound_queue=asyncio.Queue(),
        outbound_send=lambda role, pcm: asyncio.sleep(0),
    )

    async def _run_and_cancel():
        task = asyncio.create_task(runner.run(channel=channel, transport=transport))
        for _ in range(50):
            if configured:
                runner.stop.set()
                break
            await asyncio.sleep(0.01)
        await asyncio.wait_for(task, timeout=2.0)

    asyncio.run(_run_and_cancel())

    get_phase = configured["get_phase"]
    assert callable(get_phase)
    assert isinstance(get_phase(), TaskPhase)
    assert session.task_state is not None
    assert session.task_state.preferred_voice_id == "voice-42"
    assert session.task_state.auto_translate_merchant is False


def test_runner_reuses_post_call_review_state_on_reconnect_without_pipelines() -> None:
    from vocalize.dialogue.user_channel import WebSocketUserChannel
    from vocalize.server.state import Session
    from vocalize.transports.web import WebUserTransport

    state = TaskState(
        session_id="s",
        user_task_description="demo",
        phase=TaskPhase.POST_CALL_REVIEW,
    )
    assumption = state.record_uncertain_assumption(
        slot="party_size",
        question="How many?",
        assumed_value=4,
        source="user_timeout",
    )
    session = Session(session_id="s", task_description="demo", task_state=state)

    def fail_pipeline_factory(_transport):
        raise AssertionError("post-call reconnect must not initialize audio pipelines")

    runner = DialogueOrchestratorRunner(
        session=session,
        user_pipeline_factory=fail_pipeline_factory,
        merchant_pipeline_factory=fail_pipeline_factory,
    )

    async def send_json(_frame):
        return None

    channel = WebSocketUserChannel(
        send_json=send_json,
        text_input_queue=asyncio.Queue(),
        ack_clarification_queue=asyncio.Queue(),
    )
    transport = WebUserTransport(
        inbound_queue=asyncio.Queue(),
        outbound_send=lambda role, pcm: asyncio.sleep(0),
    )

    async def _run_and_stop():
        task = asyncio.create_task(runner.run(channel=channel, transport=transport))
        await asyncio.sleep(0.05)
        assert session.task_state is state
        assert session.task_state.uncertain_assumptions[0].id == assumption.id
        runner.stop.set()
        await asyncio.wait_for(task, timeout=2.0)

    asyncio.run(_run_and_stop())


def test_runner_reuses_terminal_state_on_reconnect_without_overwrite() -> None:
    from vocalize.dialogue.user_channel import WebSocketUserChannel
    from vocalize.server.state import Session
    from vocalize.transports.web import WebUserTransport

    state = TaskState(
        session_id="s",
        user_task_description="demo",
        phase=TaskPhase.COMPLETED,
    )
    state.slots["confirmation"] = "abc123"
    session = Session(session_id="s", task_description="demo", task_state=state)

    def fail_pipeline_factory(_transport):
        raise AssertionError("terminal reconnect must not initialize audio pipelines")

    runner = DialogueOrchestratorRunner(
        session=session,
        user_pipeline_factory=fail_pipeline_factory,
        merchant_pipeline_factory=fail_pipeline_factory,
    )

    async def send_json(_frame):
        return None

    channel = WebSocketUserChannel(
        send_json=send_json,
        text_input_queue=asyncio.Queue(),
        ack_clarification_queue=asyncio.Queue(),
    )
    transport = WebUserTransport(
        inbound_queue=asyncio.Queue(),
        outbound_send=lambda role, pcm: asyncio.sleep(0),
    )

    async def _run_and_stop():
        task = asyncio.create_task(runner.run(channel=channel, transport=transport))
        await asyncio.sleep(0.05)
        assert session.task_state is state
        assert session.task_state.slots["confirmation"] == "abc123"
        runner.stop.set()
        await asyncio.wait_for(task, timeout=2.0)

    asyncio.run(_run_and_stop())


def test_runner_spoken_preflight_ignores_ws_binary_audio_and_uses_text_input() -> None:
    import time as _t

    from fastapi import FastAPI
    from tests.conftest import make_scripted_llm
    from tests.test_dialogue_orchestrator import _task_planner_script
    from tests.test_pipeline import FakeTTS
    from vocalize.pipeline import VoicePipeline
    from vocalize.server.state import SessionRegistry
    from vocalize.server.ws import register_ws_routes
    from vocalize.stt.base import Transcript

    class TransportDrivenSTT:
        """Records whether preflight accidentally drives audio STT."""

        def __init__(self) -> None:
            self.saw_audio = False

        async def stream_transcribe(self, audio_chunks, **_kwargs):
            async for block in audio_chunks:
                if block:
                    self.saw_audio = True
                    break
            yield Transcript(
                text="现在打吧",
                is_final=True,
                confidence=0.95,
                start_time=0.0,
                end_time=1.0,
                utterance_id=1,
                language="zh",
            )

    class EmptySTT:
        async def stream_transcribe(self, audio_chunks, **_kwargs):
            if False:
                yield None

    user_stt = TransportDrivenSTT()
    llm = make_scripted_llm(_task_planner_script())

    def user_pipeline_factory(transport):
        return VoicePipeline(
            transport=transport,
            stt=user_stt,
            llm=llm,
            tts=FakeTTS([[b"USER-TTS"]]),
            system_prompt="",
        )

    def merchant_pipeline_factory(transport):
        return VoicePipeline(
            transport=transport,
            stt=EmptySTT(),
            llm=llm,
            tts=FakeTTS([]),
            system_prompt="",
        )

    registry = SessionRegistry()
    s = registry.create()
    registry.set_task(s.session_id, "帮我订海底捞")

    app = FastAPI()
    register_ws_routes(
        app,
        registry=registry,
        runner_factory=lambda session: DialogueOrchestratorRunner(
            session=session,
            user_pipeline_factory=user_pipeline_factory,
            merchant_pipeline_factory=merchant_pipeline_factory,
        ),
    )

    with TestClient(app) as tc:
        with tc.websocket_connect(f"/ws/sessions/{s.session_id}") as ws:
            ws.send_bytes(b"\x01\x02\x03\x04")
            ws.send_text(json.dumps({
                "type": "text_input",
                "text": "现在打吧",
                "lang_hint": "zh",
            }))
            recv = _make_bounded_receiver(ws)
            seen: list[dict[str, object]] = []
            deadline = _t.monotonic() + 5.0
            while _t.monotonic() < deadline:
                msg = recv(timeout=0.25)
                if msg is None:
                    continue
                seen.append(msg)
                if msg["type"] == "readiness_change":
                    break

    assert user_stt.saw_audio is False
    assert any(msg["type"] == "readiness_change" for msg in seen)


def test_ws_preflight_speak_text_returns_transcript_only() -> None:
    from fastapi import FastAPI
    from vocalize.server.state import SessionRegistry
    from vocalize.server.ws import register_ws_routes

    class EmptySTT:
        async def stream_transcribe(self, audio_chunks, **_kwargs):
            if False:
                yield None

    class ExplodingTTS:
        output_sample_rate = 24000
        output_encoding = "pcm_s16le"

        def stream_synthesize(self, text_chunks):
            raise AssertionError("preflight speak_text must not synthesize audio")

    class SpeakRunner:
        text_frames: list[str] = []

        def __init__(self) -> None:
            self.error: BaseException | None = None

        def attach_session_queues(
            self,
            *,
            merchant_hint_queue: asyncio.Queue,
            user_takeover_queue: asyncio.Queue,
        ) -> None:
            return None

        async def run(self, *, channel, transport):
            channel.configure_audio_io(
                transport=transport,
                stt=EmptySTT(),
                tts=ExplodingTTS(),
            )
            try:
                await channel.speak_text("好的，我来确认信息。", lang="zh")
            except BaseException as exc:
                self.error = exc
                raise

    registry = SessionRegistry()
    s = registry.create()
    app = FastAPI()
    runner = SpeakRunner()
    register_ws_routes(app, registry=registry, runner_factory=lambda _s: runner)

    with TestClient(app) as tc:
        with tc.websocket_connect(f"/ws/sessions/{s.session_id}") as ws:
            transcript = ws.receive_json()

    assert transcript["type"] == "transcript_update"
    assert transcript["role"] == "ai_to_user"
    assert transcript["text"] == "好的，我来确认信息。"
    assert transcript["lang"] == "zh"
    assert transcript["is_final"] is True
    assert transcript["subtype"] == "original"
    assert transcript["parent_id"] is None
    assert transcript["segment_id"] is None
    assert transcript["id"] and len(transcript["id"]) >= 8
    assert transcript["created_at"] and "T" in transcript["created_at"]
    assert runner.error is None


def test_b2_loopback_speak_text_sends_ai_to_user_audio() -> None:
    import queue
    import threading

    from tests.integration.b2_loopback_server import build_app

    app = build_app()

    with TestClient(app) as tc:
        created = tc.post("/api/sessions").json()
        session_id = created["session_id"]
        tc.post(
            f"/api/sessions/{session_id}/task",
            json={"task": "book a table tonight"},
        )

        with tc.websocket_connect(f"/ws/sessions/{session_id}") as ws:
            q: queue.Queue = queue.Queue()
            stop = object()

            def _pump() -> None:
                while True:
                    try:
                        q.put(ws.receive())
                    except Exception:
                        q.put(stop)
                        return

            threading.Thread(target=_pump, daemon=True).start()
            ws.send_bytes(b"\x01\x02\x03\x04")

            seen_texts: list[dict[str, object]] = []
            seen_user_audio = False
            while True:
                try:
                    msg = q.get(timeout=1.0)
                except queue.Empty:
                    break
                if msg is stop:
                    break
                if msg.get("text") is not None:
                    seen_texts.append(json.loads(msg["text"]))
                elif msg.get("bytes", b"").startswith(b"U"):
                    seen_user_audio = True
                    break

    assert any(
        msg.get("type") == "transcript_update"
        and msg.get("text") == "loopback audio received"
        for msg in seen_texts
    )
    assert seen_user_audio is True


# -- Task 17: Real-GPU smoke test ----------------------------------------------


@pytest.mark.skipif(
    "GPU_HOST" not in os.environ,
    reason="real-GPU smoke is opt-in via GPU_HOST",
)
def test_real_gpu_smoke_through_ws() -> None:
    """Connect WS, post a task, send a preflight text input, then observe at
    least one ``transcript_update`` from the real SenseVoice + DeepSeek +
    CosyVoice stack within an overall deadline.

    Without an explicit ``text_input``, the orchestrator can sit waiting on
    user input forever — the receive loop would block on
    ``ws.receive_json`` and the test session would hang. We send one text
    line right after the WS opens to drive preflight forward, and we cap
    the whole observation phase at ``DEADLINE_S`` so a stuck stack fails
    the test instead of stalling CI.

    Run with:
        GPU_HOST=100.x.y.z SENSEVOICE_WS_PORT=8000 COSYVOICE_WS_PORT=8001 pytest -k real_gpu_smoke
    """
    import time as _t

    from vocalize.server import create_app

    # Real DeepSeek + task-planner + preflight can exceed 2 minutes even when
    # all services are healthy. This smoke is opt-in and not part of normal CI,
    # so prefer a realistic default over a flaky short gate.
    DEADLINE_S = float(os.getenv("VOCALIZE_REAL_GPU_SMOKE_DEADLINE_S", "180"))

    app = create_app()
    with TestClient(app) as tc:
        resp = tc.post("/api/sessions")
        assert resp.status_code == 200
        sid = resp.json()["session_id"]

        resp = tc.post(
            f"/api/sessions/{sid}/task",
            json={"task": "帮我订海底捞"},
        )
        assert resp.status_code == 200

        with tc.websocket_connect(f"/ws/sessions/{sid}") as ws:
            ws.send_text(json.dumps({
                "type": "text_input",
                "text": "晚上7点4个人",
                "lang_hint": "zh",
            }))

            recv = _make_bounded_receiver(ws)
            deadline = _t.monotonic() + DEADLINE_S
            seen_transcript = False
            while _t.monotonic() < deadline:
                remaining = max(0.05, deadline - _t.monotonic())
                msg = recv(timeout=remaining)
                if msg is None:
                    break
                if msg.get("type") == "transcript_update":
                    seen_transcript = True
                    break
            assert seen_transcript, (
                "no transcript_update within "
                f"{DEADLINE_S}s — GPU stack may be unresponsive"
            )
