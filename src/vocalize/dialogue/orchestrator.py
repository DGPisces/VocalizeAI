"""dialogue.orchestrator — v1 universal task orchestrator (composition root).

Refactored 2026-05-04 for the 5-layer prompt architecture:
- Layer 1 (task_planner) runs at session startup.
- Layer 2 (preflight_collector) drives user-side slot collection.
- Layer 3 (merchant_agent) drives the merchant-side call.
- Layer 4 (clarification_collector) handles mid-call clarification.
- Layer 5 (relay_*_to_*) handles cross-lingual translation.

Dual-channel isolation (D-14): ``self._user.messages is not self._merchant.messages``;
the only cross-channel data path is ``TaskState``.

Key changes from Phase 4:
- ``TaskState`` / ``TaskPhase`` replace ``BookingState`` / ``BookingPhase``.
- ``run()`` takes a user task description and calls ``generate_task_schema`` first.
- System prompts rendered via ``_render_prompt(layer, state)`` with dynamic
  placeholder substitution from the task schema.
- Clarification uses callback-based API (no direct transport manipulation).
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from vocalize.dialogue import clarification
from vocalize.dialogue.keepalive import KeepaliveTimer
from vocalize.dialogue.language import is_cross_lingual
from vocalize.dialogue.prompts import load_prompt
from vocalize.dialogue.reactive_holding import ReactiveHolding
from vocalize.dialogue.relay import merchant_text_to_user_lang
from vocalize.dialogue.state import (
    DialogueOrchestratorError,
    TaskPhase,
    TaskState,
)
from vocalize.dialogue.language import detect_lang_from_text
from vocalize.dialogue.task_planner import generate_task_schema
from vocalize.dialogue.tools import (
    MERCHANT_CHANNEL_TOOLS,
    TOOLS,
    USER_CHANNEL_TOOLS,
    _require_args,
    dispatch_tool,
)
from vocalize.dialogue.user_channel import UserChannel
from vocalize.llm.base import (
    ChatMessage,
    FinishChunk,
    TextDelta,
    ToolCall,
    ToolCallDelta,
    ToolDef,
)
from vocalize.pipeline import TurnTiming, VoicePipeline
from vocalize.tts.base import TextChunk

log = logging.getLogger(__name__)


# T-04-16 mitigation: tool re-invoke upper bound. Each LLM round of tool
# dispatch counts as 1; exceeding this raises DialogueOrchestratorError.
_MAX_TOOL_INVOCATIONS: int = 10

# Directory containing Layer 1-5 prompt templates.
PROMPT_DIR = Path(__file__).parent / "prompts"


# ---------------------------------------------------------------------------
# Prompt rendering helpers — dynamic placeholder substitution
# ---------------------------------------------------------------------------


def _format_filled_slots(state: TaskState) -> str:
    """Render collected slots as a markdown bullet list."""
    if not state.slots:
        return "(none)"  # en or zh context-dependent; caller can override
    return "\n".join(f"- {k}: {v}" for k, v in state.slots.items())


def _format_missing_slots(state: TaskState, criticality: str) -> str:
    """Render missing slots of given criticality (H/M/L).

    Searches both ``slots_schema`` (H-level) and ``optional_slots_schema``
    (M/L-level) so that M/L optional slots are visible in prompts.
    """
    all_schemas = state.slots_schema + state.optional_slots_schema
    missing = [
        s for s in all_schemas
        if s.criticality == criticality and s.name not in state.slots
    ]
    if not missing:
        return "(none)"
    return "\n".join(
        f"- {s.name} ({s.description_zh} / {s.description_en})"
        for s in missing
    )


def _render_prompt(layer: str, state: TaskState, **extra: object) -> str:
    """Load prompt file and substitute placeholders.

    layer: "preflight_collector" | "merchant_agent" | "clarification_collector"
    """
    user_lang = state.user_lang or "zh"
    if layer == "merchant_agent":
        # merchant prompt uses merchant_lang. If it is still unknown
        # (e.g. preflight short-circuited via dial-now before
        # merchant_lang was collected), fall back to user_lang rather
        # than hardcoding zh — otherwise the merchant LLM receives a
        # zh prompt while the channel may actually be running in en.
        lang = state.merchant_lang or user_lang
    else:
        lang = user_lang

    path = PROMPT_DIR / f"{layer}_{lang}.md"
    template = path.read_text(encoding="utf-8")

    substitutions: dict[str, str] = {
        "task_category": state.task_category or "",
        "merchant_lang_or_unknown": state.merchant_lang or (
            "(unknown)" if user_lang == "en" else "（未填）"
        ),
        "user_lang": user_lang,
        "filled_slots_pretty": _format_filled_slots(state),
        "missing_h_slots_pretty": _format_missing_slots(state, "H"),
        "optional_slots_pretty": (
            _format_missing_slots(state, "M")
            + "\n"
            + _format_missing_slots(state, "L")
        ),
        "readiness_criteria_text": state.readiness_criteria_text or "(not yet generated)",
        "conversation_goals_pretty": "\n".join(
            f"- {g}" for g in state.conversation_goals
        ),
        "merchant_etiquette_notes": state.merchant_etiquette_notes or "",
        "relay_strategy": state.relay_strategy or "",
    }
    # Extra kwargs override / extend base substitutions.
    for key, val in extra.items():
        substitutions[key] = str(val)
    for key, val in substitutions.items():
        template = template.replace(f"{{{key}}}", val)
    return template


# ---------------------------------------------------------------------------
# Channel runtime state
# ---------------------------------------------------------------------------


@dataclass
class Channel:
    """Single-channel runtime state: messages + tools + associated pipeline + language.

    ``Channel.messages`` is the sole ChatMessage list for this channel
    (D-14 isolation): ``_run_llm_turn`` passes it to ``stream_chat`` and
    appends assistant / tool messages only to this list.
    """

    messages: list[ChatMessage]
    tools: list[ToolDef]
    pipeline: VoicePipeline
    system_prompt: str
    lang: Literal["zh", "en"]
    name: Literal["user", "merchant"]


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


class DialogueOrchestrator:
    """v1 composition root — see module docstring."""

    def __init__(
        self,
        state: TaskState,
        user_pipeline: VoicePipeline,
        merchant_pipeline: VoicePipeline,
        user_channel: UserChannel,
        tools_registry: dict[str, ToolDef] | None = None,
        wait_for_handover: Callable[[], Awaitable[None]] | None = None,
        cache_merchant_transcript: Callable[..., None] | None = None,
        consume_user_hints: Callable[[], list[tuple[str, str]]] | None = None,
        merchant_speak: Callable[..., Awaitable[None]] | None = None,
    ) -> None:
        self._state = state
        self._user_channel = user_channel
        self._tools_registry = tools_registry if tools_registry is not None else TOOLS
        self._wait_for_handover = wait_for_handover
        self._cache_merchant_transcript = cache_merchant_transcript
        self._consume_user_hints = consume_user_hints
        self._merchant_speak = merchant_speak

        # Shared LLM service object — both pipelines use stateless services
        # (STT / LLM / TTS); messages are independently owned per channel.
        self._llm = user_pipeline._llm

        # Language: pick from state if set, otherwise default to "zh".
        user_lang: Literal["zh", "en"] = state.user_lang or "zh"  # type: ignore[assignment]
        merchant_lang: Literal["zh", "en"] = state.merchant_lang or user_lang  # type: ignore[assignment]

        # Build initial system prompts via dynamic renderer.
        # Schema-dependent placeholders (slots, goals, etc.) will be empty
        # until task_planner populates the TaskState in run().
        user_prompt = _render_prompt("preflight_collector", state)
        merchant_prompt = _render_prompt("merchant_agent", state)

        self._user = Channel(
            messages=[ChatMessage(role="system", content=user_prompt)],
            tools=list(USER_CHANNEL_TOOLS),
            pipeline=user_pipeline,
            system_prompt=user_prompt,
            lang=user_lang,
            name="user",
        )
        self._merchant = Channel(
            messages=[ChatMessage(role="system", content=merchant_prompt)],
            tools=list(MERCHANT_CHANNEL_TOOLS),
            pipeline=merchant_pipeline,
            system_prompt=merchant_prompt,
            lang=merchant_lang,
            name="merchant",
        )

        # D-14 invariant sentry: assert distinct list instances.
        assert self._user.messages is not self._merchant.messages

        # Current speaking channel (D-02 addressee marker decision).
        self._current_addressee: Literal["user", "merchant"] | None = None
        self._current_segment_id: str | None = None

        # Lifecycle event queue — consumed by event_stream.
        self._events_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._relay_tasks: set[asyncio.Task[None]] = set()

    @property
    def current_segment_id(self) -> str | None:
        return self._current_segment_id

    # -----------------------------------------------------------------
    # event_stream — async yield lifecycle events (telemetry hook)
    # -----------------------------------------------------------------
    async def event_stream(self) -> AsyncIterator[dict[str, Any]]:
        """Yield ``{event: ..., ...}`` until ``completed`` / ``failed`` appears.

        Consumer should ``async for`` this iterator concurrently with ``run()``.
        """
        while True:
            event = await self._events_queue.get()
            yield event
            if event.get("event") in ("completed", "failed", "post_call_review"):
                return

    async def _emit(self, event: dict[str, Any]) -> None:
        """Fire-and-forget event into the unbounded queue."""
        await self._events_queue.put(event)

    async def _start_call_segment(self) -> None:
        from vocalize.server.frames import CallSegmentAddedFrame

        segment = self._state.start_call_segment()
        self._current_segment_id = segment.id
        frame = CallSegmentAddedFrame(segment=segment.model_dump(mode="json"))
        await self._emit({"event": frame.type, "segment": frame.segment})

    async def _end_current_call_segment(
        self,
        *,
        interrupted: bool = False,
        reason: Literal["ws_close", "user_hangup", "merchant_impatience"] | None = None,
    ) -> None:
        segment_id = (
            self._state.call_segments[-1].id
            if self._state.call_segments
            and self._state.call_segments[-1].ended_at is None
            else None
        )
        if interrupted and reason is not None:
            self._state.mark_current_segment_interrupted(reason=reason)
        else:
            self._state.end_current_segment()
        if interrupted and segment_id is not None and reason is not None:
            from vocalize.server.frames import SegmentInterruptedFrame

            frame = SegmentInterruptedFrame(segment_id=segment_id, reason=reason)
            await self._emit({
                "event": frame.type,
                "segment_id": frame.segment_id,
                "reason": frame.reason,
            })
        if segment_id is not None:
            self._current_segment_id = None

    def _prepend_user_hints(self, text: str) -> str:
        if self._consume_user_hints is None:
            return text
        hints = self._consume_user_hints()
        if not hints:
            return text
        bullets = "\n".join(f"- ({lang}) {hint}" for hint, lang in hints)
        return (
            "[USER HINT] absorb naturally without metaphrasing\n"
            f"{bullets}\n\n"
            f"{text}"
        )

    async def _process_ready_to_dial_hints(
        self,
        hints: list[tuple[str, str]],
    ) -> bool:
        """Re-assess readiness when the user edits details before handover.

        Returns True when READY_TO_DIAL regressed to COLLECTING.
        """
        if not hints or self._state.phase is not TaskPhase.READY_TO_DIAL:
            return False

        for hint, _lang in hints:
            await self._run_llm_turn(self._user, user_text=hint)

        verdict = self._state.readiness
        if verdict is None or verdict.passed:
            if verdict is not None:
                await self._emit(
                    {
                        "event": "readiness_passed",
                        "verdict": {
                            "missing_critical": verdict.missing_critical,
                            "confidence": verdict.confidence,
                            "override": verdict.override,
                        },
                    }
                )
            return False

        previous = self._state.phase.value
        self._state.transition(
            TaskPhase.COLLECTING,
            reason="pre-call supplement readiness regression",
            evidence={
                "readiness": {
                    "missing_critical": verdict.missing_critical,
                    "confidence": verdict.confidence,
                    "override": verdict.override,
                },
            },
        )
        await self._emit(
            {
                "event": "transition",
                "from": previous.upper(),
                "to": self._state.phase.value.upper(),
            }
        )
        await self._emit(
            {
                "event": "readiness_change",
                "passed": False,
                "missing_critical": verdict.missing_critical,
                "confidence": verdict.confidence,
            }
        )
        return True

    async def _wait_for_handover_or_readiness_regression(self) -> bool:
        """Wait for handover; return True if a pre-call supplement regresses."""
        if self._wait_for_handover is None:
            return False

        handover_task = asyncio.create_task(self._wait_for_handover())
        try:
            while True:
                if (
                    self._state.phase is TaskPhase.READY_TO_DIAL
                    and self._consume_user_hints is not None
                ):
                    hints = self._consume_user_hints()
                    if hints and await self._process_ready_to_dial_hints(hints):
                        return True
                if handover_task.done():
                    await handover_task
                    return False
                await asyncio.sleep(0.02)
        finally:
            if not handover_task.done():
                handover_task.cancel()
                try:
                    await handover_task
                except asyncio.CancelledError:
                    pass

    async def _speak_merchant(
        self,
        text: str,
        lang: str,
        *,
        force: bool = False,
    ) -> None:
        if self._merchant_speak is not None:
            if force:
                import inspect

                params = inspect.signature(self._merchant_speak).parameters
                accepts_force = "force" in params or any(
                    p.kind == inspect.Parameter.VAR_KEYWORD
                    for p in params.values()
                )
                if accepts_force:
                    await self._merchant_speak(text, lang, force=True)
                    return
            await self._merchant_speak(text, lang)
            return
        if force:
            transport = self._merchant.pipeline._transport
            output_force = getattr(transport, "output_stream_force", None)
            if callable(output_force):
                async def _one_chunk() -> AsyncIterator[TextChunk]:
                    yield TextChunk(
                        text=text,
                        language=lang,
                        is_final_segment=True,
                    )

                await output_force(
                    self._merchant.pipeline.tts_service.stream_synthesize(
                        _one_chunk()
                    )
                )
                return
        await self._merchant.pipeline.speak(text, lang)

    async def _emit_merchant_transcript(self, transcript_text: str) -> None:
        """Emit a merchant transcript and optional Layer 5 translation pair."""
        from vocalize.server.frames import build_transcript_update

        original = build_transcript_update(
            role="merchant_to_ai",
            text=transcript_text,
            lang=self._merchant.lang,
            is_final=True,
            segment_id=getattr(self, "_current_segment_id", None),
        )
        original_payload = original.model_dump(mode="json")
        await self._user_channel.push_event({
            "event": "transcript_update",
            **original_payload,
        })

        if self._cache_merchant_transcript is not None:
            self._cache_merchant_transcript(
                id=original.id,
                text=transcript_text,
                lang=self._merchant.lang,
            )

        user_lang: Literal["zh", "en"] = (
            "en" if self._state.user_lang == "en" else "zh"
        )
        if (
            not self._state.auto_translate_merchant
            or user_lang == self._merchant.lang
        ):
            return

        task = asyncio.create_task(
            self._emit_merchant_translation(
                original_id=original.id,
                transcript_text=transcript_text,
                src=self._merchant.lang,
                dst=user_lang,
            )
        )
        self._relay_tasks.add(task)

        def _discard_relay_task(done_task: asyncio.Task[None]) -> None:
            self._relay_tasks.discard(done_task)
            try:
                done_task.result()
            except asyncio.CancelledError:
                pass
            except Exception:
                log.exception("[orchestrator] merchant relay task crashed")

        task.add_done_callback(_discard_relay_task)

    async def _emit_ai_to_merchant_transcript(
        self,
        text: str,
        *,
        subtype: Literal["original", "filler", "keepalive"] = "original",
    ) -> None:
        """Emit merchant-directed AI speech so the live transcript matches TTS."""
        from vocalize.server.frames import build_transcript_update

        frame = build_transcript_update(
            role="ai_to_merchant",
            text=text,
            lang=self._merchant.lang,
            is_final=True,
            subtype=subtype,
            segment_id=getattr(self, "_current_segment_id", None),
        )
        await self._user_channel.push_event({
            "event": "transcript_update",
            **frame.model_dump(mode="json"),
        })

    async def _finish_relay_tasks(self, *, timeout: float = 1.0) -> None:
        """Flush pending merchant relay tasks before terminal event shutdown."""
        if not self._relay_tasks:
            return
        done, pending = await asyncio.wait(
            list(self._relay_tasks),
            timeout=timeout,
        )
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        for task in done:
            try:
                task.result()
            except asyncio.CancelledError:
                pass
            except Exception:
                log.exception("[orchestrator] merchant relay task crashed")

    async def _emit_merchant_translation(
        self,
        *,
        original_id: str,
        transcript_text: str,
        src: Literal["zh", "en"],
        dst: Literal["zh", "en"],
    ) -> None:
        from vocalize.server.frames import build_transcript_update

        try:
            result = await merchant_text_to_user_lang(
                transcript_text,
                src=src,
                dst=dst,
                llm=self._llm,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("[orchestrator] merchant relay task failed: %s", exc)
            result = None

        if result is None or result.failed or result.translated is None:
            await self._emit({
                "event": "state_update",
                "diff": {
                    "relay_failed": True,
                    "original_id": original_id,
                },
            })
            return
        if result.skipped:
            return

        translation = build_transcript_update(
            role="ai_to_user",
            text=result.translated,
            lang=dst,
            is_final=True,
            subtype="translation",
            parent_id=original_id,
        )
        await self._user_channel.push_event({
            "event": "transcript_update",
            **translation.model_dump(mode="json"),
        })

    # -----------------------------------------------------------------
    # _run_llm_turn — D-13 strict tool round-trip core loop
    # -----------------------------------------------------------------
    async def _run_llm_turn(
        self,
        channel: Channel,
        user_text: str | None = None,
    ) -> str:
        """Drive one LLM round-trip on ``channel``: stream chat + tool
        dispatch loop until a non-tool finish_reason.

        Mutates ``channel.messages`` (appends user message if given,
        appends assistant + any tool messages). Emits ``turn_complete``
        event on the NL path. Returns the assistant natural-language
        text (empty string when LLM only produced tool calls and a
        terminal-state tool ran without subsequent NL).

        Does NOT speak — caller routes the returned text to the right
        transport / channel.
        """
        if user_text is not None:
            channel.messages.append(ChatMessage(role="user", content=user_text))

        invocation_count = 0
        while True:
            invocation_count += 1
            if invocation_count > _MAX_TOOL_INVOCATIONS:
                raise DialogueOrchestratorError(
                    f"tool re-invoke loop exceeded {_MAX_TOOL_INVOCATIONS} "
                    f"iterations on channel={channel.name}"
                )

            accum: dict[int, dict[str, Any]] = {}
            text_pieces: list[str] = []
            finish_reason: str | None = None

            async for chunk in self._llm.stream_chat(
                channel.messages, tools=channel.tools
            ):
                if isinstance(chunk, TextDelta):
                    text_pieces.append(chunk.text)
                elif isinstance(chunk, ToolCallDelta):
                    slot = accum.setdefault(
                        chunk.tool_call_index,
                        {"id": None, "name": "", "args": ""},
                    )
                    if chunk.tool_call_id:
                        slot["id"] = chunk.tool_call_id
                    if chunk.name:
                        slot["name"] = chunk.name
                    slot["args"] += chunk.arguments_delta
                elif isinstance(chunk, FinishChunk):
                    finish_reason = chunk.reason

            if finish_reason != "tool_calls":
                assistant_text = "".join(text_pieces)
                channel.messages.append(
                    ChatMessage(role="assistant", content=assistant_text)
                )
                await self._emit(
                    {
                        "event": "turn_complete",
                        "channel": channel.name,
                        "assistant_text": assistant_text,
                    }
                )
                return assistant_text

            # tool_calls path — reassemble + assistant message + dispatch loop.
            # preceding_text captures any NL filler the LLM emitted before the
            # tool call (consumed by request_user_clarification filler check).
            # The assistant message itself is recorded with empty content so
            # the openai_compat serializer emits ``content: null`` alongside
            # ``tool_calls`` — OpenAI/DeepSeek strict mode rejects assistant
            # messages that carry both content and tool_calls.
            preceding_text = "".join(text_pieces)
            tool_calls = [
                ToolCall(id=v["id"] or "", name=v["name"], arguments=v["args"])
                for _, v in sorted(accum.items())
            ]
            channel.messages.append(
                ChatMessage(
                    role="assistant",
                    content="",
                    tool_calls=tool_calls,
                )
            )
            for tc in tool_calls:
                result = await self._dispatch_one_tool(
                    channel, tc, preceding_message=preceding_text
                )
                channel.messages.append(
                    ChatMessage(
                        role="tool",
                        tool_call_id=tc.id,
                        content=json.dumps(result),
                    )
                )
                await self._emit(
                    {
                        "event": "tool_dispatched",
                        "tool": tc.name,
                        "channel": channel.name,
                        "result": result,
                    }
                )
            # If a terminal-state tool ran (finalize_task → COMPLETED /
            # FAILED), exit the loop without invoking stream_chat again —
            # there is no NL turn to drive after the call has terminated.
            # Preserve any merchant-facing close emitted before the terminal
            # tool call so the spoken call does not end abruptly.
            if self._state.phase in (
                TaskPhase.COMPLETED,
                TaskPhase.FAILED,
            ):
                return preceding_text
            # loop back into stream_chat with the appended tool results
            continue

    async def _drive_turn(
        self,
        channel: Channel,
        user_text: str | None = None,
    ) -> None:
        """In-call turn driver: run LLM, then speak via ``channel.pipeline``.

        LLM logic is delegated to ``_run_llm_turn`` so preflight can
        route the assistant text differently.
        """
        assistant_text = await self._run_llm_turn(channel, user_text=user_text)
        if assistant_text:
            try:
                if channel.name == "merchant":
                    await self._speak_merchant(assistant_text, channel.lang)
                    if not self._state.user_takeover_active:
                        await self._emit_ai_to_merchant_transcript(assistant_text)
                else:
                    await channel.pipeline.speak(assistant_text, channel.lang)
            except Exception as exc:  # pragma: no cover - defensive
                log.warning(
                    "[orchestrator] TTS speak failed on channel=%s: %s",
                    channel.name, exc,
                )

    async def _dispatch_one_tool(
        self, channel: Channel, tc: ToolCall, *, preceding_message: str = "",
    ) -> dict[str, Any]:
        """Route by tool name + channel. Key branches:

        - ``request_user_clarification`` (merchant): call
          ``clarification.request_clarification`` with callback functions.
        - ``relay_to_user`` (cross-lingual; both channels): load
          ``relay_<source>_to_<target>.md`` + one-shot ``stream_chat``
          translation; speak via the *opposite* pipeline; never write to
          either channel.messages (D-14). Same-language relay falls through
          to ``dispatch_tool`` (echo).
        - Everything else: pure state mutation via ``dispatch_tool``.
        """
        if tc.name == "request_user_clarification" and channel.name == "merchant":
            try:
                args = json.loads(tc.arguments)
            except json.JSONDecodeError as exc:
                return {
                    "ok": False,
                    "error": f"tool {tc.name!r} arguments not JSON: {exc}",
                }
            if not isinstance(args, dict):
                return {
                    "ok": False,
                    "error": (
                        f"tool {tc.name!r} arguments must be a JSON object; "
                        f"got {type(args).__name__}"
                    ),
                }
            missing = _require_args(
                tc.name, args, ("field_name", "question_text", "target_lang"),
            )
            if missing is not None:
                return missing

            # Speak a hold/filler to the merchant before the clarification
            # wait so they don't hear silence. If the LLM emitted contextual
            # text alongside the tool call, prefer that — but we still have
            # to speak it ourselves: ``_run_llm_turn`` only routes assistant
            # text to TTS on ``FinishChunk(reason="stop")``, not on
            # ``reason="tool_calls"``. Otherwise fall back to a default.
            llm_filler = (preceding_message or "").strip()
            filler_text = llm_filler or (
                "好的，请您稍等一下，我确认一下"
                if self._merchant.lang == "zh"
                else "One moment please, let me check on that."
            )
            try:
                await self._emit_ai_to_merchant_transcript(
                    filler_text,
                    subtype="filler",
                )
                await self._speak_merchant(filler_text, self._merchant.lang)
            except Exception as exc:  # pragma: no cover - defensive
                log.warning(
                    "[orchestrator] clarification filler speak failed: %s",
                    exc,
                )

            # Build callback functions for the new clarification API.
            async def _user_request_fn(
                slot_name: str, question: str, lang: str,
            ) -> str:
                reply = await self._user_channel.request_clarification(
                    prompt=question,
                    lang=lang,
                    timeout_s=20.0,
                    field=slot_name,
                )
                return reply.answer

            async def _merchant_speak_fn(text: str) -> None:
                await self._speak_merchant(text, self._merchant.lang, force=True)

            async def _emit_keepalive_transcript(text: str) -> None:
                await self._emit_ai_to_merchant_transcript(
                    text,
                    subtype="keepalive",
                )

            async def _emit_reactive_filler_transcript(text: str) -> None:
                await self._emit_ai_to_merchant_transcript(
                    text,
                    subtype="filler",
                )

            # Wire the merchant transport's hold hooks to the clarification
            # coordinator so a real call leg actually enters/exits hold for
            # the duration of the user wait (P1: hold-contract enforcement).
            merchant_transport = self._merchant.pipeline._transport

            async def _merchant_pause_fn() -> None:
                await merchant_transport.pause_outbound()

            async def _merchant_resume_fn() -> None:
                await merchant_transport.resume_outbound()

            await self._emit(
                {"event": "clarification_started", "field": args["field_name"]}
            )
            best_guess = self._compute_best_guess_default(args["field_name"])
            keepalive_timer = KeepaliveTimer(
                merchant_speak=_merchant_speak_fn,
                lang=self._merchant.lang,
                emit_transcript=_emit_keepalive_transcript,
            )
            assumptions_before = len(self._state.uncertain_assumptions)
            try:
                answer = await clarification.request_clarification(
                    state=self._state,
                    slot_name=args["field_name"],
                    merchant_question=args["question_text"],
                    target_lang=args["target_lang"],
                    user_channel_request_fn=_user_request_fn,
                    merchant_speak_fn=_merchant_speak_fn,
                    merchant_pause_fn=_merchant_pause_fn,
                    merchant_resume_fn=_merchant_resume_fn,
                    merchant_lang=self._merchant.lang,
                    timeout_s=20.0,
                    reactive_holding=ReactiveHolding(
                        state=self._state,
                        merchant_speak=_merchant_speak_fn,
                        lang=self._merchant.lang,
                        current_slot=args["field_name"],
                        current_question=args["question_text"],
                        default_value=best_guess,
                        on_keepalive_reset=keepalive_timer.note_reactive_filler,
                        emit_filler=_emit_reactive_filler_transcript,
                    ),
                    keepalive_timer=keepalive_timer,
                    merchant_audio_source=merchant_transport,
                    assumed_value=best_guess,
                )
                for assumption in self._state.uncertain_assumptions[
                    assumptions_before:
                ]:
                    await self._emit(
                        {
                            "event": "uncertain_assumption_added",
                            "assumption": assumption.model_dump(mode="json"),
                        }
                    )
            except clarification.ClarificationTimedOut as exc:
                answer = exc.fallback_answer
                for assumption in self._state.uncertain_assumptions[
                    assumptions_before:
                ]:
                    await self._emit(
                        {
                            "event": "uncertain_assumption_added",
                            "assumption": assumption.model_dump(mode="json"),
                        }
                    )
                    if assumption.id == exc.assumption_id:
                        announcement = load_prompt(
                            f"clarification_callback_intent_{self._merchant.lang}"
                        ).strip()
                        await self._emit_ai_to_merchant_transcript(announcement)
                        await self._emit({
                            "event": "clarification_timed_out",
                            "assumption_id": exc.assumption_id,
                        })
            except clarification.MerchantImpatienceError:
                if not self._state.uncertain_assumptions:
                    raise
                assumption = self._state.uncertain_assumptions[-1]
                await self._emit(
                    {
                        "event": "uncertain_assumption_added",
                        "assumption": assumption.model_dump(mode="json"),
                    }
                )
                await self._emit(
                    {
                        "event": "escalation_warning",
                        "reason": "merchant_impatience",
                        "holds_used": self._state.clarification_holds_used,
                        "message_zh": "商家催了三次，这一通先挂电话",
                        "message_en": (
                            "Merchant interrupted 3 times; ending this call"
                        ),
                    }
                )
                previous = self._state.phase.value
                await self._end_current_call_segment(
                    interrupted=True,
                    reason="merchant_impatience",
                )
                self._state.transition(
                    TaskPhase.POST_CALL_REVIEW,
                    reason="merchant impatience",
                )
                await self._emit(
                    {
                        "event": "phase_change",
                        "previous": previous,
                        "current": self._state.phase.value,
                    }
                )
                raise
            except DialogueOrchestratorError as exc:
                await self._emit(
                    {
                        "event": "clarification_failed",
                        "field": args["field_name"],
                        "error": str(exc),
                    }
                )
                return {"ok": False, "error": str(exc)}
            await self._emit(
                {
                    "event": "clarification_resolved",
                    "field": args["field_name"],
                    "answer": answer,
                }
            )
            return {"ok": True, "answer": answer}

        if tc.name == "relay_to_user" and is_cross_lingual(
            self._user.lang, self._merchant.lang
        ):
            try:
                args = json.loads(tc.arguments)
            except json.JSONDecodeError as exc:
                return {
                    "ok": False,
                    "error": f"tool {tc.name!r} arguments not JSON: {exc}",
                }
            if not isinstance(args, dict):
                return {
                    "ok": False,
                    "error": (
                        f"tool {tc.name!r} arguments must be a JSON object; "
                        f"got {type(args).__name__}"
                    ),
                }
            missing = _require_args(tc.name, args, ("text", "target_lang"))
            if missing is not None:
                return missing
            translated = await self._run_relay(
                calling_channel=channel,
                target_lang=args["target_lang"],
                source_text=args["text"],
            )
            return {
                "ok": True,
                "translated": translated,
                "direction": f"{channel.name}->{args['target_lang']}",
            }

        if tc.name == "collect_user_intent" and channel.name == "merchant":
            try:
                args = json.loads(tc.arguments)
            except json.JSONDecodeError:
                args = None
            if isinstance(args, dict):
                slot = args.get("slot")
                value = args.get("value")
                if (
                    isinstance(slot, str)
                    and slot in self._state.slots
                    and self._state.slots[slot] != value
                ):
                    return {
                        "ok": False,
                        "error": (
                            f"merchant cannot overwrite user slot {slot!r}; "
                            "call request_user_clarification before changing it"
                        ),
                    }

        # Default — pure state mutation tools (collect_user_intent /
        # assess_readiness_to_dial / transition_to_calling /
        # finalize_task / same-language relay_to_user echo).
        return await dispatch_tool(tc, self._state, preceding_message=preceding_message)

    def _compute_best_guess_default(self, slot_name: str) -> object | None:
        for schema in (
            self._state.slots_schema,
            self._state.optional_slots_schema,
        ):
            for slot_def in schema:
                if slot_def.name != slot_name:
                    continue
                if slot_def.enum_values:
                    return slot_def.enum_values[0]
                if slot_def.expected_type == "number":
                    return 0
                if slot_def.expected_type == "date":
                    from datetime import date

                    return date.today().isoformat()
                return ""
        return None

    async def _run_relay(
        self,
        *,
        calling_channel: Channel,
        target_lang: Literal["zh", "en"],
        source_text: str,
    ) -> str:
        """D-15 cross-lingual relay: one-shot stream_chat translation + speak on opposite side.

        Direction (B-3):
        - merchant-channel trigger → translate for zh user → user_pipeline speaks.
        - user-channel trigger → translate for en merchant → merchant_pipeline speaks.

        Critical invariant: ``relay_messages`` references no channel.messages,
        and the output is never appended back to either channel (D-14).

        ``target_lang`` is the LLM tool argument; we use the channel-derived
        target instead and warn on mismatch, since drifting tool arguments
        could otherwise produce a same-language relay filename like
        ``relay_en_to_en`` which has no template (P1: language-pair guard).
        """
        source_lang = calling_channel.lang
        canonical_target: Literal["zh", "en"] = (
            self._user.lang if calling_channel.name == "merchant"
            else self._merchant.lang
        )
        if target_lang != canonical_target:
            log.warning(
                "[orchestrator] relay target_lang drift: tool arg=%r, "
                "canonical=%r (channel=%s); using canonical",
                target_lang, canonical_target, calling_channel.name,
            )
        if source_lang == canonical_target:
            # Same-language relay would load a non-existent template; fall
            # back to echoing the source text so the call doesn't abort.
            log.warning(
                "[orchestrator] relay called for same-language pair "
                "(source=%s, target=%s); echoing source text",
                source_lang, canonical_target,
            )
            return source_text
        relay_prompt = load_prompt(f"relay_{source_lang}_to_{canonical_target}")
        # Substitute the same single-brace placeholders the layered system
        # prompts use, so the relay LLM sees real task context instead of
        # literal "{task_category}" / "{relay_strategy}" tokens. The source
        # utterance is delivered in the user-role message.
        relay_prompt = relay_prompt.replace(
            "{task_category}", self._state.task_category or "",
        ).replace(
            "{relay_strategy}", self._state.relay_strategy or "",
        )
        relay_messages: list[ChatMessage] = [
            ChatMessage(role="system", content=relay_prompt),
            ChatMessage(role="user", content=source_text),
        ]
        translated_pieces: list[str] = []
        async for c in self._llm.stream_chat(relay_messages, tools=None):
            if isinstance(c, TextDelta):
                translated_pieces.append(c.text)
            # FinishChunk / ToolCallDelta — no action; relay uses no tools.
        translated = "".join(translated_pieces).strip()

        try:
            if calling_channel.name == "merchant":
                await self._user.pipeline.speak(translated, canonical_target)
            else:
                await self._speak_merchant(translated, canonical_target)
        except Exception as exc:  # pragma: no cover - defensive
            log.warning("[orchestrator] relay TTS speak failed: %s", exc)
        return translated

    # -----------------------------------------------------------------
    # run — high-level entry point (task_planning → preflight → merchant loop)
    # -----------------------------------------------------------------
    async def run(self, user_task_description: str) -> None:
        """Single-session orchestration:

        1. DRAFT → TASK_PLANNING; call ``generate_task_schema()``.
        2. If refused: transition to FAILED, return.
        3. Apply schema fields to TaskState; update user system prompt.
        4. TASK_PLANNING → COLLECTING; run preflight.
        5. If readiness passed, transition to EXECUTION_ACTIVE and run
           merchant loop (STT-driven dialogue with tool dispatch).
        """
        # Precondition: state must be in DRAFT.
        if self._state.phase != TaskPhase.DRAFT:
            raise DialogueOrchestratorError(
                f"run() called but state is already {self._state.phase.value}"
            )

        # Store the original task description.
        self._state.user_task_description = user_task_description

        # If the caller did not pre-seed user_lang, infer it from the task
        # description so Layer 1 is prompted in the user's actual language.
        # Without this, an English task always hit the zh task_planner
        # prompt and produced lower-quality schemas. Sync the user channel
        # so downstream TTS / cross-lingual routing reads the same value.
        if not self._state.user_lang:
            detected = detect_lang_from_text(user_task_description)
            self._state.user_lang = detected
            self._user.lang = detected  # type: ignore[assignment]
            self._user.system_prompt = _render_prompt(
                "preflight_collector", self._state,
            )
            self._user.messages[0] = ChatMessage(
                role="system", content=self._user.system_prompt,
            )

        # ---- Phase 1: Task Planning (Layer 1) ----
        self._state.transition(
            TaskPhase.TASK_PLANNING, reason="orchestrator.run() started",
        )
        await self._emit({"event": "task_planning_started"})

        try:
            schema = await generate_task_schema(
                user_task_description,
                user_lang=self._user.lang,
                llm=self._llm,
            )
        except Exception as exc:
            log.error("task_planner failed: %s", exc)
            try:
                self._state.transition(
                    TaskPhase.FAILED, reason=f"task_planner: {exc}",
                )
            except DialogueOrchestratorError:
                pass
            await self._emit(
                {"event": "failed", "stage": "task_planning", "error": str(exc)},
            )
            return

        # Handle refusal.
        if schema.refused:
            try:
                self._state.transition(
                    TaskPhase.FAILED, reason="task_planner refused",
                )
            except DialogueOrchestratorError:
                pass
            await self._emit(
                {"event": "failed", "stage": "task_planning", "reason": "refused"},
            )
            return

        # Apply schema fields to TaskState.
        self._state.task_category = schema.task_category
        self._state.slots_schema = schema.slots_schema
        self._state.optional_slots_schema = schema.optional_slots_schema
        self._state.conversation_goals = schema.conversation_goals
        self._state.merchant_etiquette_notes = schema.merchant_etiquette_notes
        self._state.readiness_criteria_text = schema.readiness_criteria_text
        self._state.relay_strategy = schema.relay_strategy

        # Update user channel system prompt now that schema is populated.
        self._user.system_prompt = _render_prompt("preflight_collector", self._state)
        self._user.messages[0] = ChatMessage(
            role="system", content=self._user.system_prompt,
        )

        # ---- Phase 2: Preflight (Layer 2) ----
        self._state.transition(
            TaskPhase.COLLECTING, reason="task_planner schema applied",
        )

        from vocalize.dialogue.preflight import run_preflight

        async def _preflight_drive_turn(user_text: str, lang: str) -> None:
            """Per-turn LLM driver passed to run_preflight.

            Runs the LLM on the user channel via _run_llm_turn (so tool
            dispatch mutates state.slots + state.readiness), then routes
            the assistant text to user_channel.speak_text.
            """
            assistant_text = await self._run_llm_turn(
                self._user, user_text=user_text,
            )
            if assistant_text:
                try:
                    await self._user_channel.speak_text(
                        assistant_text, lang=self._user.lang,
                    )
                except Exception as exc:  # pragma: no cover - defensive
                    log.warning(
                        "[orchestrator] preflight speak_text failed: %s", exc,
                    )

        initial_preflight_turn: tuple[str, str] | None = (
            user_task_description, self._user.lang,
        )
        while True:
            await self._emit({"event": "preflight_started"})
            try:
                phase_before_preflight = self._state.phase
                verdict = await run_preflight(
                    self._user_channel,
                    self._state,
                    drive_turn=_preflight_drive_turn,
                    initial_turn=initial_preflight_turn,
                )
                initial_preflight_turn = None
                if self._state.phase is not phase_before_preflight:
                    await self._emit(
                        {
                            "event": "phase_change",
                            "previous": phase_before_preflight.value,
                            "current": self._state.phase.value,
                        }
                    )
            except DialogueOrchestratorError as exc:
                log.error("preflight failed: %s", exc)
                try:
                    self._state.transition(
                        TaskPhase.FAILED, reason=f"preflight: {exc}",
                    )
                except DialogueOrchestratorError:
                    pass
                await self._emit(
                    {"event": "failed", "stage": "preflight", "error": str(exc)},
                )
                return

            await self._emit(
                {
                    "event": "readiness_passed",
                    "verdict": {
                        "missing_critical": verdict.missing_critical,
                        "confidence": verdict.confidence,
                        "override": verdict.override,
                    },
                }
            )

            if not await self._wait_for_handover_or_readiness_regression():
                break

        # ---- Phase 3: Merchant execution (Layer 3) ----
        # Transition to EXECUTION_ACTIVE (replaces old DIALING + IN_CALL).
        if self._state.phase == TaskPhase.READY_TO_DIAL:
            try:
                self._state.transition(
                    TaskPhase.EXECUTION_ACTIVE,
                    reason="post-readiness execution",
                )
                await self._start_call_segment()
                await self._emit(
                    {
                        "event": "transition",
                        "from": "READY_TO_DIAL",
                        "to": "EXECUTION_ACTIVE",
                    }
                )
            except DialogueOrchestratorError as exc:
                log.warning("EXECUTION_ACTIVE transition failed: %s", exc)

        # Sync merchant channel language before re-rendering prompt —
        # ``is_cross_lingual()`` and relay direction both read
        # ``self._merchant.lang``. Two cases to handle:
        # (a) preflight collected merchant_lang explicitly → use it;
        # (b) preflight short-circuited (dial-now) without collecting it →
        #     fall back to user_lang. Otherwise sessions where the user
        #     turned out to speak English keep the constructor's "zh"
        #     default on the merchant channel and break cross-lingual
        #     routing (P1).
        effective_merchant_lang = (
            self._state.merchant_lang
            or self._state.user_lang
            or self._merchant.lang
        )
        if self._state.merchant_lang != effective_merchant_lang:
            self._state.merchant_lang = effective_merchant_lang
        if self._merchant.lang != effective_merchant_lang:
            self._merchant.lang = effective_merchant_lang  # type: ignore[assignment]

        # Update merchant system prompt with filled slots from preflight.
        self._merchant.system_prompt = _render_prompt("merchant_agent", self._state)
        self._merchant.messages[0] = ChatMessage(
            role="system", content=self._merchant.system_prompt,
        )

        self._current_addressee = "merchant"

        # Merchant loop: consume STT stream; each final transcript drives
        # one merchant-side turn. The loop also runs during
        # NEEDS_CLARIFICATION — clarification internally holds the merchant,
        # but the loop itself stays on the EXECUTION_ACTIVE stream.
        try:
            audio_in = self._merchant.pipeline._transport.input_stream()
            # Plan 04-04: pass transport so the STT provider can register the
            # client-side webrtcvad EOS handler.
            try:
                stt_iter = self._merchant.pipeline._stt.stream_transcribe(  # type: ignore[call-arg]
                    audio_in, transport=self._merchant.pipeline._transport,
                )
            except TypeError:
                stt_iter = self._merchant.pipeline._stt.stream_transcribe(audio_in)
            try:
                async for transcript in stt_iter:
                    if not transcript.is_final:
                        continue
                    text = transcript.text.strip()
                    if not text:
                        continue
                    if self._state.phase in (
                        TaskPhase.COMPLETED,
                        TaskPhase.FAILED,
                    ):
                        break
                    # Plan 04-10 next-step #2: capture per-turn timing on
                    # the orchestrator path.
                    turn_final_at = time.monotonic()
                    mt = self._merchant.pipeline._transport
                    last_speech_end_real = (
                        mt.pop_speech_end_ts()
                        if hasattr(mt, "pop_speech_end_ts") else None
                    )
                    await self._emit_merchant_transcript(text)
                    text_for_llm = self._prepend_user_hints(text)
                    try:
                        await self._drive_turn(
                            self._merchant,
                            user_text=text_for_llm,
                        )
                        if self._state.phase == TaskPhase.POST_CALL_REVIEW:
                            await self._end_current_call_segment()
                    except clarification.MerchantImpatienceError:
                        await self._end_current_call_segment(
                            interrupted=True,
                            reason="merchant_impatience",
                        )
                        break
                    except DialogueOrchestratorError as exc:
                        log.error(
                            "orchestrator state error in merchant turn: %s", exc,
                        )
                        try:
                            self._state.transition(
                                TaskPhase.FAILED, reason=str(exc),
                            )
                        except DialogueOrchestratorError:
                            pass
                        break
                    self._merchant.pipeline._last_turn_timing = TurnTiming(
                        user_text=text,
                        final_at=turn_final_at,
                        last_speech_end_real=last_speech_end_real,
                        t_first_audible=(
                            mt.pop_first_audible_ts()
                            if hasattr(mt, "pop_first_audible_ts") else None
                        ),
                        queue_depth_at_first_audio=(
                            mt.pop_queue_depth_at_first_audio()
                            if hasattr(mt, "pop_queue_depth_at_first_audio")
                            else None
                        ),
                    )
                    if self._state.phase in (
                        TaskPhase.COMPLETED,
                        TaskPhase.FAILED,
                    ):
                        break
                    if self._state.phase == TaskPhase.POST_CALL_REVIEW:
                        await self._end_current_call_segment()
                        break
            finally:
                aclose = getattr(stt_iter, "aclose", None)
                if aclose is not None:
                    try:
                        await aclose()
                    except Exception:  # pragma: no cover - defensive cleanup
                        log.debug(
                            "merchant STT iter close failed", exc_info=True,
                        )
        finally:
            # Only report "completed" when the task machine actually
            # reached COMPLETED. Any other terminating path (merchant STT
            # stream closed mid-call, exception bubble, etc.) is a
            # failure from the consumer's perspective — falsely emitting
            # "completed" would let event_stream() consumers report
            # success while ``state.phase`` is still e.g. EXECUTION_ACTIVE.
            if self._state.phase == TaskPhase.COMPLETED:
                terminal_event = "completed"
            elif self._state.phase == TaskPhase.POST_CALL_REVIEW:
                terminal_event = "post_call_review"
            else:
                terminal_event = "failed"
            await self._finish_relay_tasks()
            await self._emit(
                {
                    "event": terminal_event,
                    "phase": self._state.phase.value,
                    "summary": _last_finalize_summary(self._state),
                }
            )


def _last_finalize_summary(state: TaskState) -> str:
    """Extract summary from the last ``finalize_task`` audit entry, if any."""
    for entry in reversed(state.audit_log):
        if entry.to_phase in (TaskPhase.COMPLETED, TaskPhase.FAILED):
            evidence = entry.evidence or {}
            summary = evidence.get("summary")
            if isinstance(summary, str):
                return summary
            return ""
    return ""


__all__ = [
    "Channel",
    "DialogueOrchestrator",
]
