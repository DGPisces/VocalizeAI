"""dialogue.user_channel — 抽象用户接入通道（mid-call clarification 路径用）。

Phase 4 only ships ``LocalMicUserChannel``（消费 ``MicrophoneTransport``
+ Provider API STT/TTS clients，对应本地 Mac speech helper 或用户自定义 provider
里的 ``--user-mic-device`` 链路）。Phase 5.5 会再补一个 ``WebSocketUserChannel``
让前端浏览器接入；两者满足同一 ``UserChannel`` Protocol，所以编排器
（Plan 04-08 clarification + Plan 04-09 orchestrator）的代码无须根据 transport
类型分支。

Design notes (CONTEXT D-03 + refactor-plan §"接口契约" L230-266):

- ``request_clarification(prompt, lang, timeout_s)`` 为编排器提供的"原子化中途
  问答"操作：先把 prompt 通过 TTS 朗读出去（共享扬声器场景下用
  ``[对用户]`` marker 提示该谁回话——marker 由调用方拼到 prompt 文本里），
  再等待来自 STT 的第一个 ``is_final=True`` Transcript。整个过程被
  ``asyncio.wait_for`` 包住，超时抛 ``asyncio.TimeoutError``。
- ``LocalMicUserChannel.__init__`` 签名固定为
  ``(transport, stt, tts)`` — 这条契约被 ``test_local_mic_user_channel_init_signature``
  钉死，Plan 10 demo wiring 直接按这个三元位置参数串起来。任何漂移都会让
  demo 链路断掉，所以不要随手加 kwargs。
- 半双工 (Phase 4 D-01) 由 ``MicrophoneTransport`` 自身在 ``output_stream``
  播音过程里把 ``input_stream`` 闸住；本模块不重复实现。我们只保证：
  ``output_stream`` 真正 drain 完之后，再开始等 STT 的 final transcript。
- ``push_event`` 在 Phase 4 仅做 INFO 日志（T-04-11 mitigation：本地日志，
  不外发）。Phase 5.5 会改写成 emit 给 web 前端，仍走同一个 Protocol 方法。
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections.abc import AsyncIterator, Awaitable
from dataclasses import dataclass
from typing import Callable, Literal, Protocol, runtime_checkable

from vocalize.stt.base import STTService
from vocalize.transports.base import AudioTransport
from vocalize.tts.base import TextChunk, TTSService

log = logging.getLogger(__name__)


@dataclass
class ClarificationReply:
    """用户对一次中途澄清问题的最终回答（用于回填 BookingState）。

    Fields:
        answer: 用户回答的纯文本（来自 STT final Transcript，已 ``.strip()``）。
        user_lang: 该回答所在的语言；当前由调用方按 prompt 的 lang 透传，
            未来若 STT 的 ``Transcript.language`` 与 prompt 不一致可在
            上层做语言路由（Plan 04-08 范围）。
        received_at: ``time.monotonic()`` 时间戳，用于在 audit log 里关联
            "提问 → 回答"的 latency。
    """

    answer: str
    user_lang: Literal["zh", "en"]
    received_at: float


@runtime_checkable
class UserChannel(Protocol):
    """中途澄清通道 + preflight I/O Protocol.

    runtime_checkable 让 Plan 04-09 orchestrator 在装配阶段可以
    ``isinstance(channel, UserChannel)`` 做轻量校验，不需要导入具体实现类。

    Method surface:
    - ``request_clarification`` — Phase 4 mid-call clarification (Plan 04-08).
    - ``push_event`` — fire-and-forget transcript / state diff stream
      (Phase 4 = INFO log; Phase 5.5 = WS push to browser).
    - ``receive_text`` — preflight outer-loop input. Returns next user
      utterance + its language. ``TextUserChannel`` reads stdin;
      ``LocalMicUserChannel`` waits on the next STT final transcript;
      future ``WebSocketUserChannel`` reads next text/audio WS frame.
    - ``speak_text`` — preflight outer-loop output. Delivers a single AI
      reply to the user. Text impl prints to stdout with ``[AI → 用户]``
      prefix; mic impl synthesises via TTS to the speaker; web impl
      pushes audio_chunk frames over WS.
    """

    async def request_clarification(
        self,
        prompt: str,
        lang: Literal["zh", "en"],
        timeout_s: float,
        field: str | None = None,
    ) -> ClarificationReply: ...

    async def push_event(self, event: dict[str, object]) -> None: ...

    async def receive_text(
        self,
    ) -> tuple[str, Literal["zh", "en"]]: ...

    async def speak_text(
        self,
        text: str,
        *,
        lang: Literal["zh", "en"],
    ) -> None: ...


class LocalMicUserChannel:
    """对接本地 ``MicrophoneTransport`` 的 UserChannel 实现。

    Phase 4 demo 共用一个扬声器；prompt 里的 ``[对用户]`` marker 提示用户
    该回话。调用流：

    1. 把 ``prompt`` 包成单段 ``TextChunk(is_final_segment=True, language=lang)``，
       喂给 ``tts.stream_synthesize``，再把 audio 灌进
       ``transport.output_stream``。``output_stream`` 必须在所有音频实际
       播完后才返回（``MicrophoneTransport`` 已经按这个语义实现）。
    2. ``transport.output_stream`` 返回后，从 ``stt.stream_transcribe(transport.input_stream())``
       拿第一个 ``is_final=True`` 且非空的 Transcript。
    3. 包装成 ``ClarificationReply`` 返回。
    4. 整段被 ``asyncio.wait_for(timeout=timeout_s)`` 包住——CONSTRAINT-013
       要求 clarification < 30s（T-04-10 mitigation）；调用方一般传 30.0。
    """

    def __init__(
        self,
        transport: AudioTransport,
        stt: STTService,
        tts: TTSService,
    ) -> None:
        self._transport = transport
        self._stt = stt
        self._tts = tts

    async def request_clarification(
        self,
        prompt: str,
        lang: Literal["zh", "en"],
        timeout_s: float,
        field: str | None = None,
    ) -> ClarificationReply:
        async def _run() -> ClarificationReply:
            # Step 1: speak the prompt as a single final-segment TTS chunk.
            async def _one_chunk() -> AsyncIterator[TextChunk]:
                yield TextChunk(text=prompt, language=lang, is_final_segment=True)

            await self._transport.output_stream(self._tts.stream_synthesize(_one_chunk()))

            # Step 2: wait for first non-empty final transcript from STT.
            audio_in = self._transport.input_stream()
            transcript_iter = self._stt.stream_transcribe(audio_in)
            answer_text: str | None = None
            try:
                async for transcript in transcript_iter:
                    if not transcript.is_final:
                        continue
                    text = transcript.text.strip()
                    if not text:
                        continue
                    answer_text = text
                    break
            finally:
                # Best-effort close so STT releases its upstream connection
                # and the input audio iterator unwinds. ``aclose`` is the
                # documented cancellation contract on STTService impls.
                aclose = getattr(transcript_iter, "aclose", None)
                if aclose is not None:
                    try:
                        await aclose()
                    except Exception:  # pragma: no cover - defensive cleanup
                        log.exception("LocalMicUserChannel: STT aclose failed")

            if answer_text is None:
                # Iterator exhausted without producing any final transcript;
                # surface this as a TimeoutError so callers (Plan 04-08
                # clarification.py) can treat "no reply" and "explicit
                # timeout" identically.
                raise asyncio.TimeoutError(
                    "LocalMicUserChannel: STT iterator ended with no final transcript"
                )

            return ClarificationReply(
                answer=answer_text,
                user_lang=lang,
                received_at=time.monotonic(),
            )

        return await asyncio.wait_for(_run(), timeout=timeout_s)

    async def push_event(self, event: dict[str, object]) -> None:
        """Phase 4: log at INFO; Phase 5.5 will emit to web frontend (T-04-11)."""
        log.info("user_channel push_event: %s", event)

    async def receive_text(
        self,
    ) -> tuple[str, Literal["zh", "en"]]:
        """Drive STT until first non-empty final Transcript; return
        ``(text, lang)``.

        ``lang`` defaults to ``"zh"`` when STT returns ``language=None``
        (uncertain detection) — Chinese-first product policy. If STT
        signals a concrete language ("zh"/"en"), it is passed through;
        any other value (e.g. "ja", "ko") falls back to "zh" because
        preflight prompts only exist in zh+en.

        Raises ``EOFError`` if the STT iterator ends with no final
        transcript — preflight outer loop decides retry policy.
        """
        audio_in = self._transport.input_stream()
        transcript_iter = self._stt.stream_transcribe(audio_in)
        text: str | None = None
        detected: str | None = None
        try:
            async for transcript in transcript_iter:
                if not transcript.is_final:
                    continue
                stripped = transcript.text.strip()
                if not stripped:
                    continue
                text = stripped
                detected = transcript.language
                break
        finally:
            aclose = getattr(transcript_iter, "aclose", None)
            if aclose is not None:
                try:
                    await aclose()
                except Exception:  # pragma: no cover - defensive cleanup
                    log.debug(
                        "LocalMicUserChannel: STT aclose failed", exc_info=True
                    )

        if text is None:
            raise EOFError(
                "LocalMicUserChannel: STT iterator ended with no final transcript"
            )

        # Normalize via the shared detect_user_lang helper so locale tags
        # like "en-US" / "zh-CN" / "zh-TW" map correctly. Pre-fix
        # exact-match check defaulted "en-US" → "zh", giving English
        # users Chinese behavior (Codex P2 2026-05-04).
        from vocalize.dialogue.language import detect_user_lang
        return text, detect_user_lang(detected)

    async def speak_text(
        self,
        text: str,
        *,
        lang: Literal["zh", "en"],
    ) -> None:
        """Synthesise ``text`` as one final TextChunk and play through
        ``self._transport.output_stream``. Mirrors the TTS step in
        ``request_clarification`` but without the subsequent STT-await."""
        async def _one_chunk() -> AsyncIterator[TextChunk]:
            yield TextChunk(text=text, language=lang, is_final_segment=True)

        await self._transport.output_stream(
            self._tts.stream_synthesize(_one_chunk())
        )


class TextUserChannel:
    """Stdin/stdout-backed UserChannel for the Phase 4 demo.

    Topology rationale: VocalizeAI's product UI for the user side is a
    browser console (PROJECT.md line 13 — "user (browser console) ↔ AI
    relay ↔ merchant (real phone via telephony (v2))"). The user is NEVER
    on a voice call themselves; they type their booking intent and read AI
    replies. Only the merchant side is voice (telephony (v2) in production,
    a local mic+speaker stand-in in this demo).

    For the local demo we approximate the browser console with stdin
    (user types) + stdout (AI prints with a ``[AI → 用户]`` prefix). The
    Phase 5.5 ``WebSocketUserChannel`` will replace this with a real
    browser WS backend; both satisfy the same ``UserChannel`` Protocol
    so the orchestrator (Plan 04-09) is unchanged.

    Methods mirror ``LocalMicUserChannel``:

    - ``request_clarification`` prints the prompt to stdout, then reads
      one line from stdin (via ``asyncio.to_thread(input, ...)`` so the
      event loop is not blocked). The whole exchange is wrapped in
      ``asyncio.wait_for(timeout_s)`` — same CONSTRAINT-013 budget that
      ``LocalMicUserChannel`` honours.
    - ``push_event`` is identical to the LocalMic impl: INFO log only
      in Phase 4; Phase 5.5 will rewrite to emit over the browser WS.

    No transport / STT / TTS dependencies — text I/O has none.
    """

    _PROMPT_PREFIX = "[AI → 用户] "
    _INPUT_PROMPT = "[用户 → AI] "

    async def request_clarification(
        self,
        prompt: str,
        lang: Literal["zh", "en"],
        timeout_s: float,
        field: str | None = None,
    ) -> ClarificationReply:
        async def _run() -> ClarificationReply:
            # Step 1: print the prompt with a clear addressee prefix so the
            # operator visually distinguishes AI→user lines from any other
            # log noise on stdout.
            print(f"{self._PROMPT_PREFIX}{prompt}", flush=True)

            # Step 2: read one line from stdin off the event loop. The
            # builtin ``input(prompt)`` blocks on a real TTY read; running
            # it via ``asyncio.to_thread`` keeps ``asyncio.wait_for`` able
            # to cancel the whole coroutine on timeout (the thread itself
            # leaks until the user presses Enter, but the demo is
            # short-lived and the next process exit reaps it — acceptable
            # for a Phase 4 local demo, not production).
            # Print the input prompt explicitly to stdout. Mac's libedit
            # /readline writes input(prompt)'s prompt to STDERR, not stdout
            # — so when operators redirect stderr to a log (e.g. `2> log`)
            # they lose the prompt visibility entirely. Decoupling the
            # prompt from input()'s prompt arg keeps the visible cue on
            # stdout where it belongs.
            print(self._INPUT_PROMPT, end="", flush=True)
            answer_text = await asyncio.to_thread(input, "")
            answer_text = answer_text.strip()

            if not answer_text:
                # Empty line ≡ "no reply" — surface as TimeoutError so the
                # caller (Plan 04-08 clarification.py) treats it identically
                # to an actual timeout, matching LocalMicUserChannel.
                raise asyncio.TimeoutError(
                    "TextUserChannel: stdin returned empty line (no reply)"
                )

            return ClarificationReply(
                answer=answer_text,
                user_lang=lang,
                received_at=time.monotonic(),
            )

        return await asyncio.wait_for(_run(), timeout=timeout_s)

    async def push_event(self, event: dict[str, object]) -> None:
        """Phase 4: log at INFO (matches LocalMicUserChannel)."""
        log.info("user_channel push_event: %s", event)

    async def receive_text(
        self,
    ) -> tuple[str, Literal["zh", "en"]]:
        """Read one stdin line as the next user utterance.

        Returns ``(text, lang)``. ``lang`` is fixed to ``"zh"`` for the
        text impl: the preflight prompt is Chinese-first by product policy
        (PROJECT.md L13). A future ``detect_user_lang(text)`` heuristic
        could refine this, but is out of scope for the preflight refactor —
        the Web impl (Phase 5.5) will pass the explicit user-selected
        language from the UI.

        Empty / whitespace-only stdin lines (user just hit Enter) are
        returned as ``("", "zh")`` — preflight's outer loop has a
        ``if not user_text: continue`` belt-and-braces that skips them
        and re-prompts. Real ``EOFError`` (Ctrl-D / stdin actually
        closed) propagates naturally from the underlying ``input()``
        call and is mapped by ``run_preflight`` to
        ``DialogueOrchestratorError("user channel exhausted")``. The
        distinction matters: a stray Enter MUST NOT abort the session,
        only true channel closure should. (Codex P2 2026-05-04.)

        Prompt is printed explicitly to stdout (not via ``input(prompt)``)
        because Mac libedit/readline writes ``input``'s prompt arg to
        STDERR — operators redirecting stderr to a log file lose the
        prompt cue entirely. Decoupling the print from input() keeps the
        prompt visible on stdout regardless of stderr redirection.
        """
        print(self._INPUT_PROMPT, end="", flush=True)
        line = await asyncio.to_thread(input, "")
        return line.strip(), "zh"

    async def speak_text(
        self,
        text: str,
        *,
        lang: Literal["zh", "en"],
    ) -> None:
        """Print one AI reply line with the [AI → 用户] prefix.

        ``lang`` is recorded for future structured logging but does not
        change output for the stdout impl (no TTS voice to switch).
        """
        print(f"{self._PROMPT_PREFIX}{text}", flush=True)


__all__ = [
    "ClarificationReply",
    "LocalMicUserChannel",
    "TextUserChannel",
    "UserChannel",
    "WebSocketUserChannel",
]


# ---------------------------------------------------------------------------
# WebSocketUserChannel — v1 web frontend impl
# ---------------------------------------------------------------------------


class WebSocketUserChannel:
    """``UserChannel`` impl backed by a per-session WebSocket.

    Composition (all four collaborators are owned by ``server/ws.py``):

    - ``send_json``: async function that writes a JSON-serialised frame to
      the WS text channel. The channel never calls ``ws.send_text`` directly
      so tests can swap in a recording fake.
    - ``text_input_queue``: ``asyncio.Queue[(str, str | None, str)]`` filled
      by ``server/ws.py`` when ``text_input`` JSON frames arrive. The tuple is
      ``(text, lang_hint, mode)``; the older ``(text, lang_hint)`` shape is
      still accepted for compatibility. The channel's ``receive_text`` blocks
      here.
    - ``ack_clarification_queue``: ``asyncio.Queue[str]`` filled by
      ``server/ws.py`` when ``ack_clarification`` JSON frames arrive. The
      channel's ``request_clarification`` blocks here.
    - ``transport`` / ``stt`` / ``tts``: optional. When all three are wired,
      the channel can do audio-mode I/O (Task 9). Text-mode tests pass
      ``None`` for all three.

    Bilingual lang inference: ``receive_text`` returns ``lang_hint`` if the
    frontend sent one, else falls back to a cheap heuristic — any CJK
    codepoint → ``zh``, otherwise ``en``. Detect-from-codepoint is good
    enough for v1 because the frontend almost always sends a hint, and the
    only fallback case is a buggy test client.
    """

    _AI_ROLE: Literal["ai_to_user"] = "ai_to_user"

    def __init__(
        self,
        send_json: "Callable[[dict[str, object]], Awaitable[None]]",
        text_input_queue: asyncio.Queue,
        ack_clarification_queue: asyncio.Queue,
        transport: AudioTransport | None = None,
        stt: STTService | None = None,
        tts: TTSService | None = None,
        *,
        get_phase: "Callable[[], object] | None" = None,
        merchant_hint_queue: asyncio.Queue | None = None,
        user_takeover_queue: asyncio.Queue | None = None,
    ) -> None:
        self._send_json = send_json
        self._text_q = text_input_queue
        self._ack_q = ack_clarification_queue
        self._transport = transport
        self._stt = stt
        self._tts = tts
        self._get_phase = get_phase
        self._hint_q = merchant_hint_queue
        self._takeover_q = user_takeover_queue

    # -- UserChannel Protocol surface -------------------------------------

    def configure_audio_io(
        self,
        *,
        transport: AudioTransport,
        stt: STTService,
        tts: TTSService,
    ) -> None:
        self._transport = transport
        self._stt = stt
        self._tts = tts

    def configure_phase_getter(
        self,
        get_phase: "Callable[[], object] | None",
    ) -> None:
        self._get_phase = get_phase

    def _has_audio_io(self) -> bool:
        return (
            self._transport is not None
            and self._stt is not None
            and self._tts is not None
        )

    async def receive_text(self) -> tuple[str, Literal["zh", "en"]]:
        from vocalize.dialogue.state import TaskPhase

        in_call = (
            self._has_audio_io()
            and self._get_phase is not None
            and self._get_phase() == TaskPhase.EXECUTION_ACTIVE
        )
        if not in_call:
            return await self._receive_text_frame()

        text_task = asyncio.create_task(self._receive_text_frame())
        audio_task = asyncio.create_task(self._receive_audio_transcript())
        pending: set[asyncio.Task[tuple[str, Literal["zh", "en"]]]] = {
            text_task,
            audio_task,
        }
        last_audio_eof: EOFError | None = None
        try:
            while pending:
                done, _ = await asyncio.wait(
                    pending,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                # Deterministic tie policy: explicit typed text wins if both
                # sources become valid in the same scheduler turn.
                if text_task in done:
                    result = text_task.result()
                    await self._cancel_or_drain_answer_task(audio_task)
                    return result

                if audio_task in done:
                    pending.remove(audio_task)
                    try:
                        result = audio_task.result()
                    except EOFError as exc:
                        last_audio_eof = exc
                        continue
                    except Exception:
                        log.warning(
                            "WebSocketUserChannel: audio transcript failed; "
                            "falling back to text input",
                            exc_info=True,
                        )
                        continue
                    await self._cancel_or_drain_answer_task(text_task)
                    return result
            if last_audio_eof is not None:
                raise last_audio_eof
            raise EOFError("WebSocketUserChannel: no input sources available")
        finally:
            for task in (text_task, audio_task):
                if not task.done():
                    task.cancel()
            for task in (text_task, audio_task):
                if not task.done():
                    await self._cancel_or_drain_answer_task(task)

    async def _cancel_or_drain_answer_task(
        self,
        task: asyncio.Task[tuple[str, Literal["zh", "en"]]],
    ) -> None:
        if not task.done():
            task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task

    async def _receive_text_frame(self) -> tuple[str, Literal["zh", "en"]]:
        while True:
            result = await self.dispatch_one_input()
            if result is not None:
                return result

    async def dispatch_one_input(self) -> tuple[str, Literal["zh", "en"]] | None:
        """Pull one inbound text frame and route it according to phase + mode.

        ``None`` means the text was consumed internally: either it was empty,
        or it was routed to the in-call supplement/takeover queue.
        """
        from vocalize.dialogue.state import TaskPhase
        from vocalize.server.frames import build_transcript_update, serialize_server_frame

        raw = await self._text_q.get()
        if len(raw) == 3:
            text, lang_hint, mode = raw
        else:
            text, lang_hint = raw
            mode = "default"

        text = text.strip()
        if not text:
            return None

        lang: Literal["zh", "en"]
        if lang_hint in ("zh", "en"):
            lang = lang_hint
        else:
            lang = _detect_lang_quick(text)

        phase = self._get_phase() if self._get_phase is not None else None
        in_call = phase == TaskPhase.EXECUTION_ACTIVE
        can_accept_supplement = phase in (
            TaskPhase.READY_TO_DIAL,
            TaskPhase.EXECUTION_ACTIVE,
            TaskPhase.NEEDS_CLARIFICATION,
            TaskPhase.AWAIT_USER_CLARIFICATION,
        )

        if in_call and mode == "user_takeover" and self._takeover_q is not None:
            frame = build_transcript_update(
                role="user_takeover_passthrough",
                text=text,
                lang=lang,
                is_final=True,
                subtype="user_takeover_passthrough",
            )
            passthrough_id = frame.id
            await self._takeover_q.put((text, lang, passthrough_id))
            await self._send_json(json.loads(serialize_server_frame(frame)))
            return None

        if can_accept_supplement and mode == "default" and self._hint_q is not None:
            await self._hint_q.put((text, lang))
            frame = build_transcript_update(
                role="user_supplement",
                text=text,
                lang=lang,
                is_final=True,
                subtype="user_supplement",
            )
            await self._send_json(json.loads(serialize_server_frame(frame)))
            return None

        return text, lang

    async def _receive_audio_transcript(self) -> tuple[str, Literal["zh", "en"]]:
        assert self._transport is not None
        assert self._stt is not None
        audio_in = self._transport.input_stream()
        try:
            transcript_iter = self._stt.stream_transcribe(  # type: ignore[call-arg]
                audio_in, transport=self._transport,
            )
        except TypeError:
            transcript_iter = self._stt.stream_transcribe(audio_in)
        try:
            async for transcript in transcript_iter:
                if not transcript.is_final:
                    continue
                text = transcript.text.strip()
                if not text:
                    continue
                from vocalize.dialogue.language import detect_user_lang
                return text, detect_user_lang(transcript.language)
        finally:
            aclose = getattr(transcript_iter, "aclose", None)
            if aclose is not None:
                with contextlib.suppress(Exception):
                    await aclose()
        raise EOFError("WebSocketUserChannel: audio STT ended with no final transcript")

    async def speak_text(
        self,
        text: str,
        *,
        lang: Literal["zh", "en"],
    ) -> None:
        from vocalize.dialogue.state import TaskPhase
        from vocalize.server.frames import build_transcript_update, serialize_server_frame

        frame = build_transcript_update(
            role=self._AI_ROLE,
            text=text,
            lang=lang,
            is_final=True,
        )
        await self._send_json(json.loads(serialize_server_frame(frame)))

        in_call = (
            self._has_audio_io()
            and self._get_phase is not None
            and self._get_phase() == TaskPhase.EXECUTION_ACTIVE
        )
        if not in_call:
            return

        assert self._transport is not None
        assert self._tts is not None

        set_role = getattr(self._transport, "set_outbound_role", None)
        if callable(set_role):
            set_role(self._AI_ROLE)

        async def _one_chunk() -> AsyncIterator[TextChunk]:
            yield TextChunk(text=text, language=lang, is_final_segment=True)

        await self._transport.output_stream(self._tts.stream_synthesize(_one_chunk()))

    async def request_clarification(
        self,
        prompt: str,
        lang: Literal["zh", "en"],
        timeout_s: float,
        field: str | None = None,
    ) -> ClarificationReply:
        """Emit a ``clarification_request`` frame, then await one
        ``ack_clarification`` from the WS queue.

        ``field`` carries the slot name the merchant asked about so the
        browser can render a specific clarification modal.
        """
        await self._send_json({
            "type": "clarification_request",
            "field": field or "",
            "question": prompt,
            "lang": lang,
            "timeout_s": timeout_s,
        })

        async def _await_ack() -> ClarificationReply:
            slot_value = await self._ack_q.get()
            return ClarificationReply(
                answer=slot_value.strip(),
                user_lang=lang,
                received_at=time.monotonic(),
            )

        try:
            return await asyncio.wait_for(_await_ack(), timeout=timeout_s)
        except asyncio.TimeoutError:
            # Drain any ack that trickled in after the deadline so it
            # doesn't corrupt the next clarification attempt.
            while not self._ack_q.empty():
                try:
                    self._ack_q.get_nowait()
                except asyncio.QueueEmpty:
                    break
            raise

    async def push_event(self, event: dict[str, object]) -> None:
        """Map orchestrator lifecycle events to WS frames.

        Dedicated spec §4.3 frames are emitted for browser-visible lifecycle
        events. Unknown events keep the generic ``state_update`` fallback.

        Anything else (``task_planning_started``, ``preflight_started``,
        ``transition``, ``completed``, ``failed``, etc. — the full set
        emitted by ``DialogueOrchestrator.event_stream``) falls back to
        a generic ``state_update`` with the entire event dict as the
        diff. The frontend filters on ``diff.event`` to render lifecycle
        chrome without B1 hardcoding the full orchestrator event
        taxonomy. This keeps the WS contract forgiving to future
        additions inside the orchestrator.
        """
        kind = event.get("event")
        if kind == "state_update":
            await self._send_json({"type": "state_update", "diff": event["diff"]})
        elif kind == "readiness_change":
            await self._send_json({
                "type": "readiness_change",
                "passed": event["passed"],
                "missing_critical": event["missing_critical"],
                "confidence": event["confidence"],
            })
        elif kind == "mode_ack":
            await self._send_json({"type": "mode_ack", "mode": event["mode"]})
        elif kind == "error":
            await self._send_json({
                "type": "error",
                "code": event["code"],
                "message_zh": event["message_zh"],
                "message_en": event["message_en"],
            })
        elif kind == "phase_change":
            await self._send_json({
                "type": "phase_change",
                "previous": event["previous"],
                "current": event["current"],
            })
        elif kind == "call_segment_added":
            await self._send_json({
                "type": "call_segment_added",
                "segment": event["segment"],
            })
        elif kind == "segment_interrupted":
            await self._send_json({
                "type": "segment_interrupted",
                "segment_id": event["segment_id"],
                "reason": event["reason"],
            })
        elif kind == "uncertain_assumption_added":
            await self._send_json({
                "type": "uncertain_assumption_added",
                "assumption": event["assumption"],
            })
        elif kind == "pending_callback_added":
            await self._send_json({
                "type": "pending_callback_added",
                "callback": event["callback"],
            })
        elif kind == "escalation_warning":
            await self._send_json({
                "type": "escalation_warning",
                "reason": event["reason"],
                "holds_used": event["holds_used"],
                "message_zh": event["message_zh"],
                "message_en": event["message_en"],
            })
        elif kind == "transcript_update":
            await self._send_json({
                "type": "transcript_update",
                **{key: value for key, value in event.items() if key != "event"},
            })
        else:
            await self._send_json({"type": "state_update", "diff": event})


def _detect_lang_quick(text: str) -> Literal["zh", "en"]:
    """Return ``zh`` if ``text`` contains any CJK Unified Ideograph, else
    ``en``. Used only when the frontend omits ``lang_hint``.
    """
    for ch in text:
        if "㐀" <= ch <= "鿿" or "豈" <= ch <= "﫿":
            return "zh"
    return "en"
