from __future__ import annotations

import asyncio
import datetime as dt
import json
from collections.abc import AsyncIterator
from typing import Literal

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from vocalize.dialogue.state import (
    DialogueOrchestratorError,
    ReadinessVerdict,
    TaskPhase,
    TaskState,
)
from vocalize.server.health import register_health_routes
from vocalize.server.sessions import register_session_routes
from vocalize.server.state import Session, SessionRegistry
from vocalize.server.ws import register_ws_routes
from vocalize.stt.base import Transcript
from vocalize.tts.base import TextChunk


class LoopbackSTT:
    async def stream_transcribe(
        self,
        audio_chunks: AsyncIterator[bytes],
        **_kwargs: object,
    ) -> AsyncIterator[Transcript]:
        async for block in audio_chunks:
            if not block:
                continue
            yield Transcript(
                text="synthetic loopback audio",
                is_final=True,
                confidence=1.0,
                start_time=0.0,
                end_time=0.02,
                utterance_id=1,
                language="en",
            )
            return


class LoopbackTTS:
    output_sample_rate = 24000
    output_encoding = "pcm_s16le"

    async def stream_synthesize(
        self,
        text_chunks: AsyncIterator[TextChunk],
    ) -> AsyncIterator[bytes]:
        async for _chunk in text_chunks:
            yield b"\x00\x00\x10\x00\x00\x00\x10\x00"


class LoopbackRunner:
    text_frames: list[str]

    def __init__(self, session: Session) -> None:
        self.text_frames = []
        self.session = session

    def attach_session_queues(
        self,
        *,
        merchant_hint_queue: asyncio.Queue,
        user_takeover_queue: asyncio.Queue,
    ) -> None:
        return None

    async def run(self, *, channel, transport) -> None:
        channel.configure_audio_io(
            transport=transport,
            stt=LoopbackSTT(),
            tts=LoopbackTTS(),
        )
        state = self._ensure_state()
        channel.configure_phase_getter(lambda: state.phase)

        if self.session.task_description:
            await self._run_audio_loopback(channel, transport, state)
            return

        await self._transition(channel, state, TaskPhase.TASK_PLANNING)
        await self._transition(channel, state, TaskPhase.COLLECTING)

        first_text, user_lang = await channel.receive_text()
        self.session.task_description = first_text
        state.user_task_description = first_text
        state.user_lang = user_lang
        await channel.speak_text("好的，我来确认关键信息。请补充日期、时间、人数和姓名。", lang="zh")

        answers = 0
        while answers < 4:
            _text, _lang = await channel.receive_text()
            answers += 1
            await channel.speak_text("收到，我继续记录。", lang="zh")

        state.readiness = ReadinessVerdict(
            missing_critical=[],
            confidence=1.0,
            decided_at=asyncio.get_running_loop().time(),
        )
        await channel.push_event({
            "event": "readiness_change",
            "passed": True,
            "missing_critical": [],
            "confidence": 1.0,
        })
        await self._transition(channel, state, TaskPhase.READY_TO_DIAL)

        while True:
            frame = await self._next_control_frame()
            kind = frame.get("type")
            if kind == "mode_change" and frame.get("mode") == "call_listening":
                await self._transition(channel, state, TaskPhase.EXECUTION_ACTIVE)
                await channel.push_event({"event": "mode_ack", "mode": "call_listening"})
                await self._transcript(channel, role="ai_to_merchant", text="您好，我想确认一笔预订。", lang="zh")
            elif kind == "hangup":
                assumption = state.record_uncertain_assumption(
                    slot="party_size",
                    question="请确认预订人数。",
                    assumed_value="四个人",
                    source="user_timeout",
                )
                await channel.push_event({
                    "event": "uncertain_assumption_added",
                    "assumption": assumption.model_dump(mode="json"),
                })
                await self._transition(channel, state, TaskPhase.POST_CALL_REVIEW)
            elif kind == "confirm_assumption":
                callback = state.confirm_assumption(
                    frame["assumption_id"],
                    choice=frame["choice"],
                    correction=frame.get("correction"),
                    note=frame.get("note"),
                )
                if callback is not None:
                    await channel.push_event({
                        "event": "pending_callback_added",
                        "callback": callback.model_dump(mode="json"),
                    })
            elif kind == "trigger_callback":
                await self._transition(channel, state, TaskPhase.CALLBACK_ACTIVE)
                await self._transcript(
                    channel,
                    role="ai_to_merchant",
                    text="回拨通话：我来更正预订人数。",
                    lang="zh",
                    subtype="callback_segment",
                    segment_id="callback-loopback",
                )

    async def _run_audio_loopback(
        self,
        channel,
        transport,
        state: TaskState,
    ) -> None:
        state.phase = TaskPhase.EXECUTION_ACTIVE
        audio = transport.input_stream()
        block = await anext(audio)
        await channel.push_event({
            "event": "state_update",
            "diff": {
                "event": "binary_audio_received",
                "bytes": len(block),
            },
        })
        await channel.push_event({
            "event": "readiness_change",
            "passed": True,
            "missing_critical": [],
            "confidence": 1.0,
        })
        await channel.speak_text("loopback audio received", lang="en")

    def _ensure_state(self) -> TaskState:
        if self.session.task_state is None:
            self.session.task_state = TaskState(
                session_id=self.session.session_id,
                auto_translate_merchant=self.session.auto_translate_merchant,
                preferred_voice_id=self.session.preferred_voice_id,
            )
        return self.session.task_state

    async def _transition(
        self,
        channel,
        state: TaskState,
        phase: TaskPhase,
    ) -> None:
        previous = state.phase
        if previous != phase:
            try:
                state.transition(phase, reason="loopback-e2e")
            except DialogueOrchestratorError:
                # Loopback is a test driver: it may replay phase markers
                # across WS reconnects or after the production state machine
                # has already advanced past the target. Skipping the call
                # keeps the client UI moving without weakening the production
                # invariant.
                pass
        await channel.push_event({
            "event": "phase_change",
            "previous": previous.value,
            "current": phase.value,
        })

    async def _next_control_frame(self) -> dict:
        while not self.text_frames:
            await asyncio.sleep(0.05)
        return json.loads(self.text_frames.pop(0))

    async def _transcript(
        self,
        channel,
        *,
        role: Literal["ai_to_merchant", "merchant_to_ai", "system"],
        text: str,
        lang: Literal["zh", "en"] | None,
        subtype: Literal["original", "callback_segment"] = "original",
        segment_id: str | None = None,
    ) -> None:
        await channel.push_event({
            "event": "transcript_update",
            "id": f"loopback-{dt.datetime.now(dt.timezone.utc).timestamp()}",
            "role": role,
            "text": text,
            "lang": lang,
            "is_final": True,
            "subtype": subtype,
            "parent_id": None,
            "segment_id": segment_id,
            "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        })


def build_app() -> FastAPI:
    app = FastAPI(title="VocalizeAI B2 Loopback")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "http://127.0.0.1:3000",
            "http://localhost:3000",
        ],
        allow_methods=["*"],
        allow_headers=["*"],
    )
    registry = SessionRegistry()
    register_session_routes(app, registry=registry)
    register_health_routes(app, gpu_probe=lambda: asyncio.sleep(0, result=False))
    register_ws_routes(
        app,
        registry=registry,
        runner_factory=lambda session: LoopbackRunner(session),
    )
    return app


if __name__ == "__main__":
    uvicorn.run(build_app(), host="127.0.0.1", port=8000, log_level="warning")
