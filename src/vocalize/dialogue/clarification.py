"""dialogue.clarification — mid-call cross-channel clarification coordinator (v1 core engine).

The merchant channel asks a slot that was not collected during preflight.
The coordinator briefly holds the merchant, asks the user via the user channel,
and relays the answer back.

Execution sequence:
1. Transition state from EXECUTION_ACTIVE to NEEDS_CLARIFICATION (if not already)
2. Set ``state.merchant_held = True``
3. Start a keepalive loop that synthesizes filler text to the merchant every
   ``keepalive_interval_s`` seconds while waiting for the user reply
4. ``await asyncio.wait_for(user_channel_request_fn(...), timeout=timeout_s)``
   — CONSTRAINT-013: clarification < 30s
5. Store answer in ``state.slots[slot_name]`` and record in
   ``state.pending_clarifications``
6. Cancel keepalive, set ``merchant_held = False``, transition back to
   EXECUTION_ACTIVE

Threat coverage:
- T-04-12 (hung clarification → infinite hold): finally block unconditionally
  cancels keepalive and restores phase / merchant_held.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any, Literal, Protocol

from vocalize.dialogue.state import (
    ClarificationItem,
    DialogueOrchestratorError,
    TaskPhase,
    TaskState,
)
from vocalize.dialogue.prompts import load_prompt

log = logging.getLogger(__name__)

_NO_ASSUMED_VALUE = object()

if TYPE_CHECKING:
    from vocalize.dialogue.reactive_holding import ReactiveHolding


class KeepaliveTimerProtocol(Protocol):
    async def run(self) -> None: ...

    def stop(self) -> None: ...

    def note_reactive_filler(self) -> None: ...


class MerchantImpatienceError(DialogueOrchestratorError):
    """Raised when reactive holding escalates during user clarification."""

    def __init__(self, slot: str) -> None:
        super().__init__(f"merchant impatience escalation on slot {slot!r}")
        self.slot = slot


class ClarificationTimedOut(DialogueOrchestratorError):
    """Raised after a timed-out clarification records its assumption."""

    def __init__(self, *, assumption_id: str, fallback_answer: str) -> None:
        super().__init__(f"clarification timeout: {assumption_id}")
        self.assumption_id = assumption_id
        self.fallback_answer = fallback_answer


async def _keepalive_loop(
    merchant_speak_fn: Callable[[str], Awaitable[None]],
    lang: str,
    interval: float,
) -> None:
    """Synthesize merchant filler every ``interval`` seconds until cancelled."""
    while True:
        await asyncio.sleep(interval)
        msg = "正在确认中，请再稍等" if lang == "zh" else "Just another moment, please."
        try:
            await merchant_speak_fn(msg)
        except asyncio.CancelledError:
            raise
        except Exception:
            pass


async def request_clarification(
    *,
    state: TaskState,
    slot_name: str,
    merchant_question: str,
    target_lang: Literal["zh", "en"],
    user_channel_request_fn: Callable[[str, str, Literal["zh", "en"]], Awaitable[str]],
    merchant_speak_fn: Callable[[str], Awaitable[None]],
    merchant_pause_fn: Callable[[], Awaitable[None]] | None = None,
    merchant_resume_fn: Callable[[], Awaitable[None]] | None = None,
    merchant_lang: Literal["zh", "en"] | None = None,
    keepalive_interval_s: float = 12.0,
    timeout_s: float = 20.0,
    reactive_holding: "ReactiveHolding | None" = None,
    keepalive_timer: KeepaliveTimerProtocol | None = None,
    merchant_audio_source: Any = None,
    assumed_value: object = _NO_ASSUMED_VALUE,
) -> str:
    """Pause merchant → ask user → resume merchant, atomic mid-call clarification.

    Args:
        state: Shared ``TaskState``. Current phase should be
            ``EXECUTION_ACTIVE``; if it is, the function transitions to
            ``NEEDS_CLARIFICATION`` before asking the user.
        slot_name: Name of the slot being clarified (any string — dynamic schema).
        merchant_question: The question text from the merchant, relayed to user.
        target_lang: Language for the user-facing prompt (``"zh"`` or ``"en"``).
        user_channel_request_fn: ``async (slot_name, question, lang) -> str``
            callback that asks the user and returns their answer.
        merchant_speak_fn: ``async (text: str) -> None`` callback that
            synthesizes TTS filler into the merchant channel during keepalive.
        merchant_pause_fn: optional ``async () -> None`` callback that holds
            the merchant transport (e.g. ``transport.pause_outbound()``) for
            the duration of clarification. Wired by the orchestrator so a
            real call leg actually enters its hold state during the wait;
            unit tests that don't have a transport can omit it.
        merchant_resume_fn: optional ``async () -> None`` callback that
            releases the hold. The finally block always invokes it if a
            corresponding pause succeeded, so the merchant cannot be left
            held after timeout / failure.
        keepalive_interval_s: Seconds between keepalive filler messages
            (default 12.0).
        timeout_s: ``asyncio.wait_for`` timeout. CONSTRAINT-013: max 30s.
        reactive_holding: optional reactive merchant-interruption handler.
        keepalive_timer: optional B3a keepalive collaborator. If omitted,
            the legacy inline keepalive loop is used for backward compatibility.
        merchant_audio_source: optional object with ``input_stream()`` yielding
            merchant PCM frames for interruption detection.
        assumed_value: optional fallback value for timeout-default wiring.
            Passing ``None`` records a null assumption; omitting it preserves
            the legacy timeout error behavior.

    Returns:
        The user's answer string.

    Raises:
        DialogueOrchestratorError: User did not reply within ``timeout_s``.
            The finally block *still* cancels keepalive and restores state —
            this is the T-04-12 mitigation contract.
    """
    # Transition to NEEDS_CLARIFICATION if still in EXECUTION_ACTIVE
    if state.phase == TaskPhase.EXECUTION_ACTIVE:
        state.transition(
            TaskPhase.NEEDS_CLARIFICATION,
            reason="merchant asked unknown field",
            evidence={"slot": slot_name},
        )
    state.merchant_held = True

    # Hold the merchant transport (best-effort — production transports
    # ack the hold; test fakes may no-op). Track success so the finally
    # block only resumes a transport we actually paused.
    transport_paused = False
    if merchant_pause_fn is not None:
        try:
            await merchant_pause_fn()
            transport_paused = True
        except Exception as exc:
            log.warning(
                "[clarification] merchant pause failed (continuing): %s", exc,
            )

    # Keepalive language: prefer the caller-provided active channel
    # language (e.g. orchestrator passes ``self._merchant.lang``), then
    # fall back to ``state.merchant_lang``, then ``"en"``. Hardcoding
    # ``"zh"`` here would emit Chinese filler into an English merchant
    # leg whenever ``merchant_lang`` was never collected.
    keepalive_lang: str = (
        merchant_lang or state.merchant_lang or "en"
    )
    keepalive_task: asyncio.Task[None] | None
    if keepalive_timer is not None:
        keepalive_task = asyncio.create_task(keepalive_timer.run())
    else:
        keepalive_task = asyncio.create_task(
            _keepalive_loop(merchant_speak_fn, keepalive_lang, keepalive_interval_s)
        )

    listener_task: asyncio.Task[None] | None = None
    if reactive_holding is not None:
        reactive_holding.start_cycle()

    if reactive_holding is not None and merchant_audio_source is not None:
        from vocalize.dialogue.merchant_vad import (
            AmbientFloorEstimator,
            detect_interruption,
        )

        async def _listen_merchant_audio() -> None:
            estimator = AmbientFloorEstimator(window_ms=2000)
            recent_pcm = b""
            interruption_armed = True
            max_recent_bytes = 32_000 * 2
            async for pcm in merchant_audio_source.input_stream():
                ambient_floor_db = estimator.current_floor_db
                if not interruption_armed:
                    if not detect_interruption(
                        pcm,
                        ambient_floor_db=ambient_floor_db,
                        duration_ms=30,
                    ):
                        interruption_armed = True
                        recent_pcm = b""
                    estimator.feed(pcm)
                    continue

                recent_pcm = (recent_pcm + pcm)[-max_recent_bytes:]
                if detect_interruption(
                    recent_pcm,
                    ambient_floor_db=ambient_floor_db,
                ):
                    if keepalive_timer is not None:
                        keepalive_timer.note_reactive_filler()
                    await reactive_holding.on_interruption()
                    if reactive_holding.escalated:
                        raise MerchantImpatienceError(slot_name)
                    recent_pcm = b""
                    interruption_armed = False
                    continue
                estimator.feed(pcm)

        listener_task = asyncio.create_task(_listen_merchant_audio())

    user_task: asyncio.Future[str] = asyncio.ensure_future(
        user_channel_request_fn(slot_name, merchant_question, target_lang)
    )
    try:
        pending: set[asyncio.Future[Any]] = {user_task}
        if listener_task is not None:
            pending.add(listener_task)

        deadline = time.monotonic() + timeout_s
        answer: str | None = None
        timed_out_assumption_id: str | None = None
        while pending:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            done, pending = await asyncio.wait(
                pending,
                timeout=remaining,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if not done:
                break

            if listener_task in done:
                listener_exc = listener_task.exception()
                if listener_exc is not None:
                    raise listener_exc
                listener_task = None

            if user_task in done:
                try:
                    answer = user_task.result()
                except asyncio.TimeoutError:
                    if assumed_value is _NO_ASSUMED_VALUE:
                        raise
                    answer = None
                break

        if answer is None:
            if assumed_value is _NO_ASSUMED_VALUE:
                raise asyncio.TimeoutError
            assumption = state.record_uncertain_assumption(
                slot=slot_name,
                question=merchant_question,
                assumed_value=assumed_value,
                source="user_timeout",
            )
            timed_out_assumption_id = assumption.id
            announce_lang = (
                merchant_lang or state.merchant_lang or state.user_lang or "zh"
            )
            if announce_lang not in ("zh", "en"):
                announce_lang = "zh"
            announcement = load_prompt(
                f"clarification_callback_intent_{announce_lang}"
            ).strip()
            try:
                await merchant_speak_fn(announcement)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning(
                    "[clarification] callback-intent announcement failed: %s",
                    exc,
                )
            answer = "" if assumed_value is None else str(assumed_value)

        # Store answer in dynamic slots dict
        state.slots[slot_name] = answer
        state.pending_clarifications.append(
            ClarificationItem(
                field=slot_name,
                question=merchant_question,
                answer=answer,
                ts=time.monotonic(),
            )
        )
        if timed_out_assumption_id is not None:
            raise ClarificationTimedOut(
                assumption_id=timed_out_assumption_id,
                fallback_answer=answer,
            )
        return answer
    except asyncio.TimeoutError:
        log.warning(
            "[clarification] user did not reply within %.1fs (slot=%s)",
            timeout_s,
            slot_name,
        )
        raise DialogueOrchestratorError(
            f"clarification timeout: {slot_name}"
        ) from None
    finally:
        # Load-bearing safety invariant (T-04-12): keepalive MUST be cancelled
        # and state restored regardless of success or timeout.
        if keepalive_timer is not None:
            keepalive_timer.stop()
        for task in (user_task, listener_task, keepalive_task):
            if task is not None and not task.done():
                task.cancel()
        for task, name in (
            (user_task, "user clarification"),
            (listener_task, "merchant audio listener"),
            (keepalive_task, "keepalive"),
        ):
            if task is None:
                continue
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                if isinstance(exc, MerchantImpatienceError):
                    pass
                else:
                    log.warning("[clarification] %s task ended with exception: %s", name, exc)
        state.merchant_held = False
        # Release the transport hold if (and only if) we actually paused it,
        # so a pause failure mode doesn't lead to an unmatched resume.
        if transport_paused and merchant_resume_fn is not None:
            try:
                await merchant_resume_fn()
            except Exception as exc:
                log.warning(
                    "[clarification] merchant resume failed: %s", exc,
                )
        state.transition(
            TaskPhase.EXECUTION_ACTIVE,
            reason="resumed after clarification",
            evidence={"slot": slot_name},
        )


__all__ = [
    "ClarificationTimedOut",
    "MerchantImpatienceError",
    "request_clarification",
]
