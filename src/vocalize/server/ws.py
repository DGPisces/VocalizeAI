"""WebSocket handler for ``/ws/sessions/{id}``.

Per-connection lifecycle:

1. Client opens WS → handler resolves the session via ``SessionRegistry``.
   Unknown id → close with code 4404.
2. Handler builds a ``WebUserTransport`` and a ``WebSocketUserChannel``,
   plus the per-session inbound queues for ``text_input`` and
   ``ack_clarification`` frames.
3. ``runner_factory(session)`` is called to obtain an ``OrchestratorRunner``;
   in production this is the wiring helper from Task 14 that constructs the
   ``DialogueOrchestrator``. Tests pass a fake.
4. Two coroutines run concurrently:
    - ``recv_loop``: pulls inbound WS frames forever and routes them.
    - ``runner.run(channel, transport)``: drives the orchestrator round-trip.
5. When either returns, the other is cancelled and the WS is closed.

This file does NOT import ``DialogueOrchestrator`` directly — Task 14 adds a
wiring module that holds that import. Keeping the boundary clean lets
``test_server_ws_integration.py`` exercise the framing without dragging the
LLM stack into every test fixture.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Callable, Protocol

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.websockets import WebSocketState

from vocalize.dialogue.state import TaskPhase
from vocalize.dialogue.user_channel import WebSocketUserChannel
from vocalize.server.frames import (
    decode_inbound_audio_chunk,
    encode_outbound_audio_chunk,
    parse_client_frame,
)
from vocalize.server.metrics import WS_SESSIONS_CLOSED_TOTAL, WS_SESSIONS_OPENED_TOTAL
from vocalize.server.state import Session, SessionRegistry
from vocalize.transports.web import WebUserTransport

log = logging.getLogger(__name__)


async def _close_ws_safely(ws: WebSocket, *, code: int = 1000) -> None:
    with contextlib.suppress(RuntimeError, AttributeError):
        await ws.close(code=code)


class OrchestratorRunner(Protocol):
    """The orchestrator-driving abstraction the WS handler depends on.

    Production impl: ``server.runner.DialogueOrchestratorRunner`` (Task 14).
    Test impl: see ``tests/test_server_ws_integration.py``.

    Surface:
    - ``text_frames``: the WS handler appends raw text frames the runner
      needs to dispatch itself (mode_change / hangup / set_devices).
      Other text frame types are routed by the WS handler directly to
      the channel's queues.
    - ``run(channel, transport)``: awaited for the connection's lifetime.
      Internal runner state (``stop``, ``audio_blocks``, etc.) is an
      implementation detail and does not appear on the Protocol.
    """

    text_frames: list[str]

    async def run(
        self,
        *,
        channel: WebSocketUserChannel,
        transport: WebUserTransport,
    ) -> None: ...

    def attach_session_queues(
        self,
        *,
        merchant_hint_queue: asyncio.Queue,
        user_takeover_queue: asyncio.Queue,
    ) -> None: ...


RunnerFactory = Callable[[Session], OrchestratorRunner]


def register_ws_routes(
    app: FastAPI,
    *,
    registry: SessionRegistry,
    runner_factory: RunnerFactory,
) -> None:
    @app.websocket("/ws/sessions/{session_id}")
    async def ws_endpoint(ws: WebSocket, session_id: str) -> None:
        await ws.accept()
        session = registry.get(session_id)
        if session is None:
            await _close_ws_safely(ws, code=4404)
            return
        # Reject concurrent WS connections for the same session
        # (two browser tabs, fast reconnect). claim is atomic; only
        # the first caller wins.
        if not registry.claim(session_id):
            await _close_ws_safely(ws, code=4404)
            return

        # Session is now claimed; count it as opened and track close reason.
        WS_SESSIONS_OPENED_TOTAL.inc()
        close_reason: str = "normal"

        transport: WebUserTransport | None = None
        try:
            inbound_audio: asyncio.Queue = asyncio.Queue()
            text_input_q: asyncio.Queue = asyncio.Queue()
            ack_q: asyncio.Queue = asyncio.Queue()
            hint_q: asyncio.Queue = asyncio.Queue()
            takeover_q: asyncio.Queue = asyncio.Queue()

            send_lock = asyncio.Lock()

            async def send_json_locked(frame: dict) -> None:
                async with send_lock:
                    if ws.application_state == WebSocketState.CONNECTED:
                        await ws.send_json(frame)

            async def outbound_send(role: str, pcm: bytes) -> None:
                async with send_lock:
                    if ws.application_state != WebSocketState.CONNECTED:
                        return
                    tagged = encode_outbound_audio_chunk(role, pcm)  # type: ignore[arg-type]
                    await ws.send_bytes(tagged)

            transport = WebUserTransport(
                inbound_queue=inbound_audio,
                outbound_send=outbound_send,
            )
            runner = runner_factory(session)
            runner.attach_session_queues(
                merchant_hint_queue=hint_q,
                user_takeover_queue=takeover_q,
            )

            def phase_from_session() -> TaskPhase:
                return (
                    session.task_state.phase
                    if session.task_state is not None
                    else TaskPhase.DRAFT
                )

            channel = WebSocketUserChannel(
                send_json=send_json_locked,
                text_input_queue=text_input_q,
                ack_clarification_queue=ack_q,
                transport=transport,
                get_phase=phase_from_session,
                merchant_hint_queue=hint_q,
                user_takeover_queue=takeover_q,
            )

            async def recv_loop() -> None:
                try:
                    while True:
                        msg = await ws.receive()
                        if msg.get("type") == "websocket.disconnect":
                            return
                        if msg.get("text") is not None:
                            raw = msg["text"]
                            try:
                                frame = parse_client_frame(raw)
                            except Exception:
                                log.warning("ws: unparseable frame dropped: %r", raw[:200])
                                # Invalid control frames are dropped at the WS
                                # boundary — they must not reach the runner's
                                # dispatch loop where they could trigger
                                # side-effects (e.g. a malformed mode_change
                                # producing a mode_ack).
                                continue
                            kind = frame.type
                            if kind == "text_input":
                                await text_input_q.put((
                                    frame.text,
                                    frame.lang_hint,
                                    frame.mode,
                                ))
                                if phase_from_session() in (
                                    TaskPhase.READY_TO_DIAL,
                                    TaskPhase.EXECUTION_ACTIVE,
                                    TaskPhase.NEEDS_CLARIFICATION,
                                    TaskPhase.AWAIT_USER_CLARIFICATION,
                                ):
                                    await channel.dispatch_one_input()
                            elif kind == "ack_clarification":
                                await ack_q.put(frame.slot_value)
                            else:
                                runner.text_frames.append(raw)
                            registry.touch(session_id)
                        elif msg.get("bytes") is not None:
                            try:
                                chunk = decode_inbound_audio_chunk(msg["bytes"])
                            except ValueError:
                                log.warning("ws: empty binary frame ignored")
                                continue
                            transport.push_inbound(chunk.pcm)
                            registry.touch(session_id)
                except WebSocketDisconnect:
                    return

            recv_task = asyncio.create_task(recv_loop())
            run_task = asyncio.create_task(
                runner.run(channel=channel, transport=transport)
            )
            done, pending = await asyncio.wait(
                {recv_task, run_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            # Surface exceptions from whichever task finished first
            # so they don't silently disappear into the ether.
            for t in done:
                if not t.cancelled():
                    exc = t.exception()
                    if exc is not None:
                        log.error("ws: task %r raised", t, exc_info=exc)
                        close_reason = "error"
            for t in pending:
                t.cancel()
            for t in pending:
                try:
                    await t
                except (asyncio.CancelledError, Exception):
                    pass
        finally:
            try:
                if transport is not None:
                    await transport.close()
                if ws.application_state == WebSocketState.CONNECTED:
                    await _close_ws_safely(ws)
            finally:
                registry.release(session_id)
                WS_SESSIONS_CLOSED_TOTAL.labels(reason=close_reason).inc()


__all__ = ["OrchestratorRunner", "RunnerFactory", "register_ws_routes"]
