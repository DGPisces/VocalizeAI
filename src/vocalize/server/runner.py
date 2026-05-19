"""Wires the v1 ``DialogueOrchestrator`` into the WS handler.

The runner is constructed once per WS connection. Its ``run`` method:
1. Builds a ``TaskState`` seeded with the session's task description.
2. Builds two ``VoicePipeline`` instances (one user, one merchant) sharing
   the same ``WebUserTransport`` for audio I/O. (The transport is shared
   because the laptop has only one mic + one speaker; the orchestrator
   ensures only one pipeline is active at a time per spec §4.1 state
   machine.)
3. Constructs the ``DialogueOrchestrator`` and concurrently runs
   ``orchestrator.run(task)`` and ``orchestrator.event_stream()``, the
   latter forwarding events to ``channel.push_event``.
4. Forwards buffered ``text_frames`` from the WS recv loop into orchestrator
   commands (mode_change → mode_ack frame echo; hangup → cancel orchestrator;
   set_devices → session observability metadata).

This file is the only place that imports ``DialogueOrchestrator`` from the
server package, so the rest of ``server/`` has zero LLM-stack coupling.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from collections.abc import AsyncIterator, Awaitable
from datetime import datetime, timezone
from typing import Callable, Literal, cast

from vocalize.dialogue.orchestrator import DialogueOrchestrator
from vocalize.dialogue.state import (
    CallbackEntry,
    DialogueOrchestratorError,
    TaskPhase,
    TaskState,
)
from vocalize.dialogue.user_channel import WebSocketUserChannel
from vocalize.llm.base import LLMService
from vocalize.pipeline import VoicePipeline
from vocalize.server.frames import TranscriptRole
from vocalize.server.state import DeviceSelection, Session
from vocalize.tts.base import TTSService, TextChunk
from vocalize.transports.base import AudioEncoding, AudioTransport
from vocalize.transports.web import WebUserTransport

log = logging.getLogger(__name__)

PipelineFactory = Callable[[AudioTransport], VoicePipeline]
CALLBACK_REPLY_TIMEOUT_S = 12.0

_RECOVER_EXECUTION_ACTIVE_RECONNECT_PHASES = (
    TaskPhase.EXECUTION_ACTIVE,  # _recover_execution_active_reconnect
    TaskPhase.NEEDS_CLARIFICATION,  # _recover_execution_active_reconnect
    TaskPhase.AWAIT_USER_CLARIFICATION,  # _recover_execution_active_reconnect
)

_CONTROL_RECONNECT_PHASES = {
    *_RECOVER_EXECUTION_ACTIVE_RECONNECT_PHASES,
    TaskPhase.POST_CALL_REVIEW,
    TaskPhase.CALLBACK_ACTIVE,
    TaskPhase.COMPLETED,
    TaskPhase.FAILED,
}


class _RoleTaggedTransport:
    """Wraps a shared ``WebUserTransport`` to pin the outbound role.

    Both user and merchant pipelines share the same underlying transport,
    but the frontend needs to distinguish audio streams by role tag
    (``b'U'`` vs ``b'M'``). This wrapper passes the role with each outbound
    stream so overlapping TTS calls cannot mutate a shared role mid-stream.
    """

    sample_rate: int
    channels: int
    encoding: AudioEncoding

    def __init__(
        self,
        delegate: WebUserTransport,
        role: Literal["ai_to_user", "ai_to_merchant"],
    ) -> None:
        self._delegate = delegate
        self._role = role
        self.sample_rate = delegate.sample_rate
        self.channels = delegate.channels
        self.encoding = delegate.encoding

    async def input_stream(self) -> AsyncIterator[bytes]:
        async for block in self._delegate.input_stream():
            yield block

    @property
    def _on_eos(self) -> Callable[[], Awaitable[None]] | None:
        return self._delegate._on_eos

    @_on_eos.setter
    def _on_eos(self, handler: Callable[[], Awaitable[None]] | None) -> None:
        self._delegate._on_eos = handler

    def pop_speech_end_ts(self) -> float | None:
        return self._delegate.pop_speech_end_ts()

    async def output_stream(self, audio: AsyncIterator[bytes]) -> None:
        await self._delegate.output_stream_for_role(self._role, audio)

    async def output_stream_force(self, audio: AsyncIterator[bytes]) -> None:
        await self._delegate.output_stream_force_for_role(self._role, audio)

    async def close(self) -> None:
        await self._delegate.close()

    async def pause_outbound(self) -> None:
        await self._delegate.pause_outbound()

    async def resume_outbound(self) -> None:
        await self._delegate.resume_outbound()

    def drain_inbound(self) -> int:
        return self._delegate.drain_inbound()


def _translate_event(event: dict) -> dict:
    """Map a ``DialogueOrchestrator.event_stream`` event to the dict shape
    ``WebSocketUserChannel.push_event`` expects.

    Three event families have dedicated frame types:

    - ``readiness_passed`` → ``readiness_change(passed=True, ...)``
    - ``failed`` → ``error(code, message_zh, message_en)``
    - complete ``transition`` events → ``phase_change(previous, current)``

    All other events become a generic ``state_update`` so the frontend can
    render lifecycle chrome by filtering on ``diff.event``. The channel also
    has a defensive fallback for the same shape — this translator just
    centralises the mapping in the layer that knows the orchestrator vocabulary.
    """
    kind = event.get("event")
    if kind == "readiness_passed":
        verdict = event.get("verdict") or {}
        return {
            "event": "readiness_change",
            "passed": True,
            "missing_critical": verdict.get("missing_critical", []),
            "confidence": verdict.get("confidence", 1.0),
        }
    if kind == "failed":
        stage = event.get("stage", "unknown")
        err = event.get("error") or event.get("reason") or "failure"
        return {
            "event": "error",
            "code": 2000,
            "message_zh": f"处理失败（{stage}）：{err}",
            "message_en": f"Processing failed ({stage}): {err}",
        }
    if kind == "state_update":
        return event
    if kind == "readiness_change":
        return event
    if kind in (
        "phase_change",
        "call_segment_added",
        "segment_interrupted",
        "uncertain_assumption_added",
        "pending_callback_added",
        "escalation_warning",
    ):
        return event
    if kind == "transition":
        previous = event.get("from")
        current = event.get("to")
        if previous and current:
            return {
                "event": "phase_change",
                "previous": str(previous).lower(),
                "current": str(current).lower(),
            }
    return {"event": "state_update", "diff": event}


class _ReadinessChangeDebouncer:
    """Coalesce readiness_change frames so fast verdict churn does not flicker."""

    def __init__(
        self,
        push_event: Callable[[dict], object],
        *,
        delay_s: float = 0.1,
    ) -> None:
        self._push_event = push_event
        self._delay_s = delay_s
        self._pending: dict | None = None
        self._task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()

    async def submit(self, frame: dict) -> None:
        async with self._lock:
            self._pending = frame
            if self._task is not None and not self._task.done():
                self._task.cancel()
            self._task = asyncio.create_task(self._emit_later())

    async def flush(self) -> None:
        task = self._task
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        await self._emit_pending()

    async def _emit_later(self) -> None:
        try:
            await asyncio.sleep(self._delay_s)
            await self._emit_pending()
        except asyncio.CancelledError:
            pass

    async def _emit_pending(self) -> None:
        async with self._lock:
            frame = self._pending
        if frame is not None:
            result = self._push_event(frame)
            if asyncio.iscoroutine(result):
                await result
            async with self._lock:
                if self._pending is frame:
                    self._pending = None
                task = self._task
                current = asyncio.current_task()
                if task is current or (task is not None and task.done()):
                    self._task = None


class DialogueOrchestratorRunner:
    """Production ``OrchestratorRunner`` impl.

    Public attributes (kept compatible with the Protocol in ws.py for tests):
        text_frames: list of raw text frames the WS recv loop buffered for us.
        audio_blocks: list of inbound audio blocks (test introspection only).
        stop: ``asyncio.Event`` set when ``run`` should exit early (e.g.
            hangup frame received).
    """

    text_frames: list[str]
    audio_blocks: list[bytes]
    stop: asyncio.Event

    def __init__(
        self,
        *,
        session: Session,
        user_pipeline_factory: PipelineFactory,
        merchant_pipeline_factory: PipelineFactory,
    ) -> None:
        self._session = session
        self._user_pf = user_pipeline_factory
        self._merchant_pf = merchant_pipeline_factory
        self.text_frames = []
        self.audio_blocks = []
        self.stop = asyncio.Event()
        self._hint_q: asyncio.Queue | None = None
        self._takeover_q: asyncio.Queue | None = None
        self._merchant_transcript_cache = session.merchant_transcript_cache
        self._merchant_tts: TTSService | None = None
        self._merchant_transport: _RoleTaggedTransport | None = None
        self._user_pipeline: VoicePipeline | None = None
        self._merchant_pipeline: VoicePipeline | None = None
        self._merchant_lang_supplier: Callable[[], str] = lambda: "zh"
        self._llm: LLMService | None = None
        self._pending_ai_outputs: list[tuple[str, str]] = []
        self._handover_ready: asyncio.Event | None = None
        self._readiness_passed: asyncio.Event | None = None
        self._web_transport: WebUserTransport | None = None
        self._orchestrator_task: asyncio.Task | None = None
        self._forward_task: asyncio.Task | None = None
        self._orchestrator: DialogueOrchestrator | None = None
        self._user_channel: WebSocketUserChannel | None = None

    def _cache_merchant_transcript(self, *, id: str, text: str, lang: str) -> None:
        if len(self._merchant_transcript_cache) >= 200:
            oldest = next(iter(self._merchant_transcript_cache))
            self._merchant_transcript_cache.pop(oldest, None)
        self._merchant_transcript_cache[id] = (text, lang)

    async def _resolve_task_text(
        self,
        *,
        channel: WebSocketUserChannel,
    ) -> str | None:
        task_text = (self._session.task_description or "").strip()
        if task_text:
            self._session.task_description = task_text
            return task_text

        try:
            task_text, _lang = await channel.receive_text()
        except EOFError:
            await channel.push_event({
                "event": "error",
                "code": 1001,
                "message_zh": "尚未提交任务描述",
                "message_en": "Task description not posted",
            })
            return None

        self._session.task_description = task_text
        return task_text

    @staticmethod
    def _requeue_callback(callback: CallbackEntry) -> None:
        callback.status = "queued"
        callback.started_at = None
        callback.completed_at = None
        callback.transcript_segment_id = None

    async def _push_pending_callbacks_state(
        self,
        *,
        channel: WebSocketUserChannel,
        state: TaskState,
    ) -> None:
        await channel.push_event({
            "event": "state_update",
            "diff": {
                "pending_callbacks": [
                    callback.model_dump(mode="json")
                    for callback in state.pending_callbacks
                ],
            },
        })

    def attach_session_queues(
        self,
        *,
        merchant_hint_queue: asyncio.Queue,
        user_takeover_queue: asyncio.Queue,
    ) -> None:
        self._hint_q = merchant_hint_queue
        self._takeover_q = user_takeover_queue

    def consume_pending_hints(self) -> list[tuple[str, str]]:
        """Drain merchant hint queue without blocking the merchant turn."""
        out: list[tuple[str, str]] = []
        if self._hint_q is None:
            return out
        while True:
            try:
                out.append(self._hint_q.get_nowait())
            except asyncio.QueueEmpty:
                return out

    def _ensure_audio_pipelines(
        self,
        *,
        channel: WebSocketUserChannel,
        transport: WebUserTransport,
        state: TaskState,
    ) -> tuple[VoicePipeline, VoicePipeline]:
        user_pipeline = self._user_pipeline
        merchant_pipeline = self._merchant_pipeline
        if (
            user_pipeline is not None
            and merchant_pipeline is not None
            and self._merchant_tts is not None
            and self._merchant_transport is not None
            and self._llm is not None
        ):
            return user_pipeline, merchant_pipeline

        user_transport = _RoleTaggedTransport(transport, "ai_to_user")
        user_pipeline = self._user_pf(user_transport)
        self._user_pipeline = user_pipeline
        self._llm = user_pipeline._llm
        channel.configure_audio_io(
            transport=user_transport,
            stt=user_pipeline.stt_service,
            tts=user_pipeline.tts_service,
        )
        merchant_transport = _RoleTaggedTransport(transport, "ai_to_merchant")
        merchant_pipeline = self._merchant_pf(merchant_transport)
        self._merchant_pipeline = merchant_pipeline
        self._merchant_tts = merchant_pipeline.tts_service
        self._merchant_transport = merchant_transport
        self._merchant_lang_supplier = (
            lambda: state.merchant_lang or state.user_lang or "zh"
        )
        return user_pipeline, merchant_pipeline

    async def _consume_takeover_q(self) -> None:
        """Translate user-takeover typed text before merchant TTS when needed."""
        from vocalize.dialogue.relay import user_to_merchant
        from vocalize.server.frames import build_transcript_update

        if self._takeover_q is None:
            return
        assert self._merchant_transport is not None
        assert self._merchant_tts is not None
        while not self.stop.is_set():
            text, user_lang, passthrough_id = await self._takeover_q.get()
            merchant_lang: Literal["zh", "en"] = (
                "en" if self._merchant_lang_supplier() == "en" else "zh"
            )
            speak_text = text

            if user_lang != merchant_lang:
                assert self._llm is not None
                result = await user_to_merchant(
                    text,
                    src=user_lang,
                    dst=merchant_lang,
                    llm=self._llm,
                )
                if not result.failed and result.translated:
                    speak_text = result.translated
                    assert self._orchestrator is not None
                    assert self._user_channel is not None
                    segment_id = self._orchestrator.current_segment_id
                    frame = build_transcript_update(
                        role="ai_to_merchant",
                        text=result.translated,
                        lang=merchant_lang,
                        is_final=True,
                        subtype="translation",
                        parent_id=passthrough_id,
                        segment_id=segment_id,
                    )
                    await self._user_channel.push_event({
                        "event": "transcript_update",
                        **frame.model_dump(mode="json"),
                    })
                else:
                    log.warning("user_to_merchant relay failed; using original text")

            async def _one_chunk() -> AsyncIterator[TextChunk]:
                yield TextChunk(
                    text=speak_text,
                    language=merchant_lang,
                    is_final_segment=True,
                )

            try:
                await self._merchant_transport.resume_outbound()
                await self._merchant_transport.output_stream(
                    self._merchant_tts.stream_synthesize(_one_chunk())
                )
            except Exception:
                log.exception("takeover-tts: merchant pipeline raised; ignoring")
            finally:
                state = self._session.task_state
                if state is not None and state.user_takeover_active:
                    try:
                        await self._merchant_transport.pause_outbound()
                    except Exception:
                        log.exception("takeover-tts: re-pause failed")

    async def _handle_on_demand_translate(
        self,
        raw: str,
        *,
        channel: WebSocketUserChannel,
    ) -> None:
        from vocalize.dialogue.relay import merchant_text_to_user_lang
        from vocalize.server.frames import build_transcript_update

        payload = json.loads(raw)
        transcript_id = payload.get("transcript_id")
        cached = self._merchant_transcript_cache.get(transcript_id)
        if cached is None:
            await channel.push_event({
                "event": "error",
                "code": 1003,
                "message_zh": f"原始消息已过期或不存在（id={transcript_id}）",
                "message_en": (
                    f"Original transcript not found (id={transcript_id})"
                ),
            })
            return

        text, src_lang = cached
        state = self._session.task_state
        dst_lang: Literal["zh", "en"] = (
            "en" if state is not None and state.user_lang == "en" else "zh"
        )
        src: Literal["zh", "en"] = "en" if src_lang == "en" else "zh"
        assert self._llm is not None
        result = await merchant_text_to_user_lang(
            text,
            src=src,
            dst=dst_lang,
            llm=self._llm,
        )
        if result.failed or result.translated is None:
            await channel.push_event({
                "event": "error",
                "code": 1004,
                "message_zh": "翻译服务暂不可用",
                "message_en": "Translation unavailable",
            })
            return

        frame = build_transcript_update(
            role="ai_to_user",
            text=result.translated,
            lang=dst_lang,
            is_final=True,
            subtype="translation",
            parent_id=transcript_id,
        )
        await channel.push_event({
            "event": "transcript_update",
            **frame.model_dump(mode="json"),
        })

    async def _merchant_speak(
        self,
        text: str,
        lang: str,
        *,
        force: bool = False,
    ) -> None:
        state = self._session.task_state
        if state is not None and state.user_takeover_active and not force:
            return

        assert self._merchant_transport is not None
        assert self._merchant_tts is not None

        async def _one_chunk() -> AsyncIterator[TextChunk]:
            yield TextChunk(text=text, language=lang, is_final_segment=True)

        output = (
            self._merchant_transport.output_stream_force
            if force and hasattr(self._merchant_transport, "output_stream_force")
            else self._merchant_transport.output_stream
        )
        await output(
            self._merchant_tts.stream_synthesize(_one_chunk())
        )

    async def _read_callback_merchant_reply(self) -> str:
        merchant_pipeline = self._merchant_pipeline
        assert merchant_pipeline is not None
        async for transcript in merchant_pipeline._stt.stream_transcribe(
            merchant_pipeline._transport.input_stream(),
        ):
            if transcript.is_final and transcript.text.strip():
                return transcript.text.strip()
        raise RuntimeError("merchant STT exhausted during callback")

    async def _await_callback_merchant_reply(self) -> str:
        try:
            return await asyncio.wait_for(
                self._read_callback_merchant_reply(),
                timeout=CALLBACK_REPLY_TIMEOUT_S,
            )
        except asyncio.TimeoutError as exc:
            raise RuntimeError("merchant STT timed out during callback") from exc

    async def _handle_trigger_callback(
        self,
        raw: str,
        *,
        channel: WebSocketUserChannel,
    ) -> None:
        from vocalize.dialogue.callback import run_callback
        from vocalize.server.frames import build_transcript_update

        payload = json.loads(raw)
        callback_id = payload.get("callback_id")
        state = self._session.task_state
        callback = None if state is None else next(
            (item for item in state.pending_callbacks if item.id == callback_id),
            None,
        )
        if state is None or callback is None:
            await channel.push_event({
                "event": "error",
                "code": 1005,
                "message_zh": f"找不到回拨条目 {callback_id}",
                "message_en": f"Callback {callback_id} not found",
            })
            return
        if callback.status != "queued":
            await channel.push_event({
                "event": "error",
                "code": 1008,
                "message_zh": f"回拨条目 {callback_id} 不可再次拨打",
                "message_en": f"Callback {callback_id} is not queued",
            })
            return
        if getattr(self, "_llm", None) is None:
            transport = getattr(self, "_web_transport", None)
            if transport is None:
                await channel.push_event({
                    "event": "error",
                    "code": 1011,
                    "message_zh": "回拨音频通道尚未准备好",
                    "message_en": "Callback audio channel is not ready",
                })
                return
            self._ensure_audio_pipelines(
                channel=channel,
                transport=transport,
                state=state,
            )

        previous_phase = state.phase.value
        try:
            state.transition(
                TaskPhase.CALLBACK_ACTIVE,
                reason="user-triggered callback",
            )
        except DialogueOrchestratorError:
            await channel.push_event({
                "event": "error",
                "code": 1009,
                "message_zh": "当前阶段不能回拨",
                "message_en": "Cannot trigger callback from the current phase",
            })
            return
        await channel.push_event({
            "event": "phase_change",
            "previous": previous_phase,
            "current": state.phase.value,
        })

        async def emit_transcript(
            role: str,
            text: str,
            segment_id: str,
        ) -> None:
            if role not in ("ai_to_merchant", "merchant_to_ai"):
                raise ValueError(f"unexpected callback transcript role: {role}")
            role_t = cast(TranscriptRole, role)
            lang: Literal["zh", "en"] = (
                "en" if state.merchant_lang == "en" else "zh"
            )
            frame = build_transcript_update(
                role=role_t,
                text=text,
                lang=lang,
                is_final=True,
                subtype="callback_segment",
                segment_id=segment_id,
            )
            await channel.push_event({
                "event": "transcript_update",
                **frame.model_dump(mode="json"),
            })
            if role == "ai_to_merchant":
                await self._merchant_speak(text, lang)

        try:
            await run_callback(
                state=state,
                callback=callback,
                llm=self._llm,
                emit_transcript=emit_transcript,
                await_merchant_reply=self._await_callback_merchant_reply,
                lang="en" if state.merchant_lang == "en" else "zh",
                transition_to_active=False,
            )
        except Exception as exc:
            self._requeue_callback(callback)
            await channel.push_event({
                "event": "error",
                "code": 1010,
                "message_zh": f"回拨失败: {exc}",
                "message_en": f"Callback failed: {exc}",
            })
        await channel.push_event({
            "event": "phase_change",
            "previous": "callback_active",
            "current": state.phase.value,
        })
        await self._push_pending_callbacks_state(channel=channel, state=state)

    async def _handle_cancel_callback(
        self,
        raw: str,
        *,
        channel: WebSocketUserChannel,
    ) -> None:
        payload = json.loads(raw)
        callback_id = payload.get("callback_id")
        state = self._session.task_state
        callback = None if state is None else next(
            (item for item in state.pending_callbacks if item.id == callback_id),
            None,
        )
        if state is None or callback is None:
            await channel.push_event({
                "event": "error",
                "code": 1005,
                "message_zh": f"找不到回拨条目 {callback_id}",
                "message_en": f"Callback {callback_id} not found",
            })
            return
        if callback.status == "queued":
            callback.status = "cancelled"
        await channel.push_event({
            "event": "state_update",
            "diff": {
                "pending_callbacks": [
                    item.model_dump(mode="json")
                    for item in state.pending_callbacks
                ],
            },
        })

    async def _handle_restore_callback(
        self,
        raw: str,
        *,
        channel: WebSocketUserChannel,
    ) -> None:
        payload = json.loads(raw)
        callback_id = payload.get("callback_id")
        state = self._session.task_state
        callback = None if state is None else next(
            (item for item in state.pending_callbacks if item.id == callback_id),
            None,
        )
        if state is None or callback is None:
            await channel.push_event({
                "event": "error",
                "code": 1005,
                "message_zh": f"找不到回拨条目 {callback_id}",
                "message_en": f"Callback {callback_id} not found",
            })
            return
        if callback.status != "cancelled":
            await channel.push_event({
                "event": "error",
                "code": 1012,
                "message_zh": f"回拨条目 {callback_id} 不是已取消状态",
                "message_en": f"Callback {callback_id} is not in cancelled state",
            })
            return
        callback.status = "queued"
        await self._push_pending_callbacks_state(channel=channel, state=state)

    async def _handle_confirm_assumption(
        self,
        raw: str,
        *,
        channel: WebSocketUserChannel,
    ) -> None:
        payload = json.loads(raw)
        state = self._session.task_state
        if state is None:
            await channel.push_event({
                "event": "error",
                "code": 1006,
                "message_zh": "找不到 assumption",
                "message_en": "Assumption not found",
            })
            return
        try:
            callback = state.confirm_assumption(
                payload["assumption_id"],
                choice=payload["choice"],
                correction=payload.get("correction"),
                note=payload.get("note"),
            )
        except KeyError:
            assumption_id = payload.get("assumption_id")
            await channel.push_event({
                "event": "error",
                "code": 1006,
                "message_zh": f"找不到 assumption {assumption_id!r}",
                "message_en": f"Assumption {assumption_id!r} not found",
            })
            return
        except ValueError as exc:
            await channel.push_event({
                "event": "error",
                "code": 1007,
                "message_zh": f"参数错误: {exc}",
                "message_en": f"Bad request: {exc}",
            })
            return

        if callback is not None:
            await channel.push_event({
                "event": "pending_callback_added",
                "callback": callback.model_dump(mode="json"),
            })
        await channel.push_event({
            "event": "state_update",
            "diff": {
                "uncertain_assumptions": [
                    item.model_dump(mode="json")
                    for item in state.uncertain_assumptions
                ],
                "pending_callbacks": [
                    item.model_dump(mode="json")
                    for item in state.pending_callbacks
                ],
            },
        })

    async def _handle_clarification_timeout(
        self,
        *,
        assumption_id: str,
        state: TaskState,
        channel: WebSocketUserChannel,
    ) -> None:
        assumption = state.find_assumption_by_id(assumption_id)
        if assumption is None:
            await channel.push_event({
                "event": "error",
                "code": 1006,
                "message_zh": f"找不到 assumption {assumption_id!r}",
                "message_en": f"Assumption {assumption_id!r} not found",
            })
            return

        existing = next(
            (
                callback for callback in state.pending_callbacks
                if callback.assumption_id == assumption.id
                and callback.status in {"queued", "in_progress"}
            ),
            None,
        )
        callback = existing
        if callback is None:
            callback = CallbackEntry(
                id=uuid.uuid4().hex,
                assumption_id=assumption.id,
                correction=(
                    ""
                    if assumption.assumed_value is None
                    else str(assumption.assumed_value)
                ),
                note="clarification timeout",
                created_at=datetime.now(timezone.utc),
            )
            state.pending_callbacks.append(callback)
            assumption.callback_id = callback.id
            await channel.push_event({
                "event": "pending_callback_added",
                "callback": callback.model_dump(mode="json"),
            })

        await channel.push_event({
            "event": "state_update",
            "diff": {
                "uncertain_assumptions": [
                    item.model_dump(mode="json")
                    for item in state.uncertain_assumptions
                ],
                "pending_callbacks": [
                    item.model_dump(mode="json")
                    for item in state.pending_callbacks
                ],
            },
        })
        await self._transition_phase(
            TaskPhase.POST_CALL_REVIEW,
            reason="clarification timeout",
            channel=channel,
        )

    async def _handle_set_auto_translate(
        self,
        raw: str,
        *,
        channel: WebSocketUserChannel,
    ) -> None:
        from vocalize.server.frames import SetAutoTranslateFrame, parse_client_frame

        frame = parse_client_frame(raw)
        if not isinstance(frame, SetAutoTranslateFrame):
            return
        state = self._session.task_state
        if state is None:
            return
        self._session.auto_translate_merchant = frame.value
        state.auto_translate_merchant = frame.value
        await channel.push_event({
            "event": "state_update",
            "diff": {"auto_translate_merchant": state.auto_translate_merchant},
        })

    async def _handle_set_devices(self, raw: str) -> None:
        from vocalize.server.frames import SetDevicesFrame, parse_client_frame

        frame = parse_client_frame(raw)
        if not isinstance(frame, SetDevicesFrame):
            return
        self._session.device_selection = DeviceSelection(
            input_id=frame.input_id,
            output_id=frame.output_id,
            aec=frame.aec,
        )
        log.info(
            "set_devices: input=%s output=%s aec=%s",
            frame.input_id,
            frame.output_id,
            frame.aec,
        )

    async def _handle_merchant_text_inject(
        self,
        raw: str,
        *,
        channel: WebSocketUserChannel,
    ) -> None:
        from vocalize.dialogue import clarification
        from vocalize.server.frames import MerchantTextInjectFrame, parse_client_frame

        if os.getenv("VOCALIZE_ENABLE_TEST_FRAMES") != "1":
            await channel.push_event({
                "event": "error",
                "code": 1013,
                "message_zh": (
                    "测试用 merchant_text_inject 未启用；"
                    "请设置 VOCALIZE_ENABLE_TEST_FRAMES=1"
                ),
                "message_en": (
                    "merchant_text_inject is disabled; set "
                    "VOCALIZE_ENABLE_TEST_FRAMES=1 to enable test frames"
                ),
            })
            return

        frame = parse_client_frame(raw)
        if not isinstance(frame, MerchantTextInjectFrame):
            return
        orchestrator = getattr(self, "_orchestrator", None)
        if orchestrator is None:
            await channel.push_event({
                "event": "error",
                "code": 1014,
                "message_zh": "商家文本注入通道尚未准备好",
                "message_en": "Merchant text injection is not ready",
            })
            return

        await orchestrator._emit_merchant_transcript(frame.text)
        text_for_llm = orchestrator._prepend_user_hints(frame.text)
        try:
            await orchestrator._drive_turn(
                orchestrator._merchant,
                user_text=text_for_llm,
            )
            state = self._session.task_state
            if (
                state is not None
                and state.phase == TaskPhase.POST_CALL_REVIEW
                and hasattr(orchestrator, "_end_current_call_segment")
            ):
                await orchestrator._end_current_call_segment()
        except clarification.MerchantImpatienceError:
            if hasattr(orchestrator, "_end_current_call_segment"):
                await orchestrator._end_current_call_segment(
                    interrupted=True,
                    reason="merchant_impatience",
                )

    async def _transition_phase(
        self,
        target: TaskPhase,
        *,
        reason: str,
        channel: WebSocketUserChannel,
    ) -> bool:
        state = self._session.task_state
        if state is None:
            return False
        previous = state.phase.value
        try:
            state.transition(target, reason=reason)
        except DialogueOrchestratorError:
            log.warning(
                "%s: illegal transition; staying on %s",
                reason,
                state.phase.value,
            )
            return False
        await channel.push_event({
            "event": "phase_change",
            "previous": previous,
            "current": state.phase.value,
        })
        return True

    async def _handle_hangup(self, *, channel: WebSocketUserChannel) -> None:
        state = self._session.task_state
        if state is not None and state.phase == TaskPhase.EXECUTION_ACTIVE:
            segment_id = (
                state.call_segments[-1].id
                if state.call_segments and state.call_segments[-1].ended_at is None
                else None
            )
            state.end_current_segment(interrupted=True, reason="user_hangup")
            if segment_id is not None:
                from vocalize.server.frames import SegmentInterruptedFrame

                frame = SegmentInterruptedFrame(
                    segment_id=segment_id,
                    reason="user_hangup",
                )
                await channel.push_event({
                    "event": frame.type,
                    "segment_id": frame.segment_id,
                    "reason": frame.reason,
                })
            target = (
                TaskPhase.POST_CALL_REVIEW
                if state.uncertain_assumptions
                else TaskPhase.COMPLETED
            )
            await self._transition_phase(target, reason="user hangup", channel=channel)
            if target == TaskPhase.POST_CALL_REVIEW:
                for task in (
                    getattr(self, "_orchestrator_task", None),
                    getattr(self, "_forward_task", None),
                ):
                    if task is not None and not task.done():
                        task.cancel()
                return
        self.stop.set()

    async def _handle_mode_ended(self, *, channel: WebSocketUserChannel) -> None:
        state = self._session.task_state
        if state is not None and state.phase in (
            TaskPhase.EXECUTION_ACTIVE,
            TaskPhase.POST_CALL_REVIEW,
        ):
            await self._transition_phase(
                TaskPhase.COMPLETED,
                reason="mode_change(ended)",
                channel=channel,
            )
        self.stop.set()

    async def _handle_mode_takeover_on(
        self,
        *,
        channel: WebSocketUserChannel,
    ) -> None:
        state = self._session.task_state
        if state is None or state.phase != TaskPhase.EXECUTION_ACTIVE:
            return
        state.set_user_takeover(active=True)
        assert self._merchant_transport is not None
        try:
            await self._merchant_transport.pause_outbound()
        except Exception:
            log.exception("takeover-on: pause_outbound failed")
        await channel.push_event({"event": "mode_ack", "mode": "user_takeover"})

    async def _handle_mode_takeover_off(
        self,
        *,
        channel: WebSocketUserChannel,
    ) -> None:
        state = self._session.task_state
        if state is None:
            return
        state.set_user_takeover(active=False)
        self._pending_ai_outputs.clear()
        assert self._merchant_transport is not None
        try:
            await self._merchant_transport.resume_outbound()
        except Exception:
            log.exception("takeover-off: resume_outbound failed")
        await channel.push_event({"event": "mode_ack", "mode": "call_listening"})

    async def _dispatch_text_frame(
        self,
        raw: str,
        *,
        channel: WebSocketUserChannel,
    ) -> None:
        payload = json.loads(raw)
        kind = payload.get("type") if isinstance(payload, dict) else None
        if kind == "mode_change":
            mode = payload.get("mode")
            if not mode:
                log.warning("handle_text_frames: mode_change missing 'mode'; dropping")
                return
            state = self._session.task_state
            if mode == "user_takeover":
                await self._handle_mode_takeover_on(channel=channel)
                return
            if (
                mode == "call_listening"
                and state is not None
                and state.user_takeover_active
            ):
                await self._handle_mode_takeover_off(channel=channel)
                return
            if mode == "call_listening":
                readiness_passed = self._readiness_passed
                state_ready = (
                    state is not None
                    and state.phase in (
                        TaskPhase.READY_TO_DIAL,
                        TaskPhase.EXECUTION_ACTIVE,
                    )
                    and state.readiness is not None
                    and state.readiness.passed
                )
                if (
                    readiness_passed is None
                    or not readiness_passed.is_set()
                    or not state_ready
                ):
                    if readiness_passed is not None:
                        readiness_passed.clear()
                    if self._handover_ready is not None:
                        self._handover_ready.clear()
                    await channel.push_event({
                        "event": "error",
                        "code": 1002,
                        "message_zh": "信息尚未准备好，不能接管",
                        "message_en": (
                            "Cannot hand over before readiness passes"
                        ),
                    })
                    return
                transport = self._web_transport
                if transport is not None:
                    drained = transport.drain_inbound()
                    if drained:
                        log.info(
                            "handover: drained %s stale inbound audio blocks",
                            drained,
                        )
                    transport.set_drop_inbound(False)
            await channel.push_event({"event": "mode_ack", "mode": mode})
            if mode == "call_listening":
                if self._handover_ready is not None:
                    self._handover_ready.set()
            elif mode == "ended":
                await self._handle_mode_ended(channel=channel)
        elif kind == "hangup":
            await self._handle_hangup(channel=channel)
        elif kind == "trigger_callback":
            await self._handle_trigger_callback(raw, channel=channel)
        elif kind == "cancel_callback":
            await self._handle_cancel_callback(raw, channel=channel)
        elif kind == "restore_callback":
            await self._handle_restore_callback(raw, channel=channel)
        elif kind == "confirm_assumption":
            await self._handle_confirm_assumption(raw, channel=channel)
        elif kind == "set_auto_translate":
            await self._handle_set_auto_translate(raw, channel=channel)
        elif kind == "on_demand_translate":
            await self._handle_on_demand_translate(raw, channel=channel)
        elif kind == "set_devices":
            await self._handle_set_devices(raw)
        elif kind == "merchant_text_inject":
            await self._handle_merchant_text_inject(raw, channel=channel)

    async def _handle_text_frames_loop(
        self,
        *,
        channel: WebSocketUserChannel,
    ) -> None:
        while not self.stop.is_set():
            if not self.text_frames:
                await asyncio.sleep(0.02)
                continue
            raw = self.text_frames.pop(0)
            try:
                await self._dispatch_text_frame(raw, channel=channel)
            except Exception:
                log.exception(
                    "handle_text_frames: error processing frame; continuing"
                )

    async def _recover_callback_active_reconnect(
        self,
        *,
        state: TaskState,
        channel: WebSocketUserChannel,
    ) -> None:
        if state.phase != TaskPhase.CALLBACK_ACTIVE:
            return

        for callback in state.pending_callbacks:
            if callback.status == "in_progress":
                self._requeue_callback(callback)

        previous = state.phase.value
        try:
            state.transition(
                TaskPhase.POST_CALL_REVIEW,
                reason="callback reconnect recovery",
            )
        except DialogueOrchestratorError:
            return

        await channel.push_event({
            "event": "phase_change",
            "previous": previous,
            "current": state.phase.value,
        })
        await self._push_pending_callbacks_state(channel=channel, state=state)

    async def _recover_execution_active_reconnect(
        self,
        *,
        state: TaskState,
        channel: WebSocketUserChannel,
    ) -> None:
        if state.phase not in _RECOVER_EXECUTION_ACTIVE_RECONNECT_PHASES:
            return

        state.merchant_held = False
        state.clarification_holds_used = 0
        segment_id = (
            state.call_segments[-1].id
            if state.call_segments and state.call_segments[-1].ended_at is None
            else None
        )
        state.mark_current_segment_interrupted(reason="ws_close")
        if segment_id is not None:
            from vocalize.server.frames import SegmentInterruptedFrame

            frame = SegmentInterruptedFrame(segment_id=segment_id, reason="ws_close")
            await channel.push_event({
                "event": frame.type,
                "segment_id": frame.segment_id,
                "reason": frame.reason,
            })

        await self._transition_phase(
            TaskPhase.POST_CALL_REVIEW,
            reason="ws disconnect during execution",
            channel=channel,
        )

    async def run(
        self,
        *,
        channel: WebSocketUserChannel,
        transport: WebUserTransport,
    ) -> None:
        task_text = await self._resolve_task_text(channel=channel)
        if task_text is None:
            return

        existing_state = self._session.task_state
        if (
            existing_state is not None
            and existing_state.phase in _CONTROL_RECONNECT_PHASES
        ):
            state = existing_state
            reused_control_state = True
        else:
            state = TaskState(
                session_id=self._session.session_id,
                user_task_description=task_text,
                auto_translate_merchant=self._session.auto_translate_merchant,
                preferred_voice_id=self._session.preferred_voice_id,
            )
            self._session.task_state = state
            reused_control_state = False
        channel.configure_phase_getter(
            lambda: (
                self._session.task_state.phase
                if self._session.task_state is not None
                else TaskPhase.DRAFT
            )
        )

        handover_ready = asyncio.Event()
        readiness_passed = asyncio.Event()
        self._handover_ready = handover_ready
        self._readiness_passed = readiness_passed
        self._web_transport = transport

        if reused_control_state:
            await self._recover_callback_active_reconnect(
                state=state,
                channel=channel,
            )
            await self._recover_execution_active_reconnect(state=state, channel=channel)
            text_task = asyncio.create_task(
                self._handle_text_frames_loop(channel=channel)
            )
            try:
                await self.stop.wait()
            finally:
                for t in (text_task,):
                    if not t.done():
                        t.cancel()
                for t in (text_task,):
                    try:
                        await t
                    except (asyncio.CancelledError, Exception):
                        pass
            return

        user_pipeline, merchant_pipeline = self._ensure_audio_pipelines(
            channel=channel,
            transport=transport,
            state=state,
        )

        async def wait_for_handover() -> None:
            await handover_ready.wait()

        orchestrator = DialogueOrchestrator(
            state=state,
            user_pipeline=user_pipeline,
            merchant_pipeline=merchant_pipeline,
            user_channel=channel,
            wait_for_handover=wait_for_handover,
            cache_merchant_transcript=self._cache_merchant_transcript,
            consume_user_hints=self.consume_pending_hints,
            merchant_speak=self._merchant_speak,
        )
        self._orchestrator = orchestrator
        self._user_channel = channel

        async def forward_events() -> None:
            """Translate orchestrator events into WS-frame-shaped payloads.

            ``DialogueOrchestrator.event_stream`` emits a vocabulary
            wider than the four spec §4.3 frame types: ``turn_complete``,
            ``tool_dispatched``, ``clarification_started/_failed/_resolved``,
            ``task_planning_started``, ``preflight_started``,
            ``readiness_passed``, ``transition``, ``failed``,
            ``completed``, etc. The runner is the layer that knows this
            vocabulary; the channel is intentionally a thin frame
            emitter. We translate here, then delegate.
            """
            async for event in orchestrator.event_stream():
                if event.get("event") == "readiness_passed":
                    readiness_passed.set()
                    transport.set_drop_inbound(True)
                elif event.get("event") == "clarification_timed_out":
                    await self._handle_clarification_timeout(
                        assumption_id=str(event["assumption_id"]),
                        state=state,
                        channel=channel,
                    )
                    continue
                elif (
                    event.get("event") == "readiness_change"
                    and event.get("passed") is False
                ):
                    readiness_passed.clear()
                    handover_ready.clear()
                    transport.set_drop_inbound(True)
                frame = _translate_event(event)
                if frame.get("event") == "readiness_change":
                    await readiness_debouncer.submit(frame)
                    continue
                await readiness_debouncer.flush()
                await channel.push_event(frame)

        readiness_debouncer = _ReadinessChangeDebouncer(channel.push_event)

        orchestrator_task = asyncio.create_task(orchestrator.run(task_text))
        forward_task = asyncio.create_task(forward_events())
        self._orchestrator_task = orchestrator_task
        self._forward_task = forward_task
        text_task = asyncio.create_task(self._handle_text_frames_loop(channel=channel))
        takeover_task = asyncio.create_task(self._consume_takeover_q())
        stop_wait = asyncio.create_task(self.stop.wait())

        done: set[asyncio.Task] = set()
        try:
            try:
                done, _pending = await asyncio.wait(
                    {forward_task, stop_wait, orchestrator_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
            finally:
                if not stop_wait.done():
                    stop_wait.cancel()
                    try:
                        await stop_wait
                    except (asyncio.CancelledError, Exception):
                        pass

            # Surface orchestrator exceptions to logs as soon as observed.
            if orchestrator_task in done:
                exc = orchestrator_task.exception() if not orchestrator_task.cancelled() else None
                if exc is not None:
                    log.exception(
                        "orchestrator.run raised before terminal event",
                        exc_info=exc,
                    )

            if forward_task in done:
                pass
            elif orchestrator_task in done:
                try:
                    await forward_task
                except (asyncio.CancelledError, Exception):
                    pass
            else:
                # External stop fired (hangup frame, WS closed).
                # Cancel the orchestrator immediately — the user has
                # hung up and it must not continue processing.
                orchestrator_task.cancel()
                # Drain any pending events for up to 2 s so the
                # terminal (error) frame reaches the client.
                try:
                    await asyncio.wait_for(
                        asyncio.shield(forward_task), timeout=2.0
                    )
                except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
                    pass
            current_state = self._session.task_state
            if (
                current_state is not None
                and current_state.phase == TaskPhase.POST_CALL_REVIEW
                and not self.stop.is_set()
            ):
                await self.stop.wait()
        finally:
            # Always cancel orphan tasks — when ws.py cancels
            # runner.run(), the CancelledError can arrive inside
            # asyncio.wait, skipping the normal cleanup path.
            for t in (forward_task, text_task, takeover_task, orchestrator_task):
                if not t.done():
                    t.cancel()
            for t in (forward_task, text_task, takeover_task, orchestrator_task):
                try:
                    await t
                except (asyncio.CancelledError, Exception):
                    pass


__all__ = ["DialogueOrchestratorRunner", "PipelineFactory", "_translate_event"]
