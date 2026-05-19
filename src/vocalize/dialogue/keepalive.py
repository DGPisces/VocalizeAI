"""Merchant keepalive timer during user clarification."""
from __future__ import annotations

import asyncio
import logging
import time
from contextlib import suppress
from collections.abc import Awaitable, Callable
from typing import Literal

from vocalize.dialogue.prompts import load_prompt

log = logging.getLogger(__name__)


class KeepaliveTimer:
    """Periodic merchant TTS keepalive with reactive-filler suppression."""

    def __init__(
        self,
        *,
        merchant_speak: Callable[[str], Awaitable[None]],
        lang: Literal["zh", "en"],
        interval_s: float = 12.0,
        suppression_window_s: float = 6.0,
        monotonic: Callable[[], float] = time.monotonic,
        emit_transcript: Callable[[str], Awaitable[None]] | None = None,
    ) -> None:
        self._merchant_speak = merchant_speak
        self._lang = lang
        self._interval_s = interval_s
        self._suppression_window_s = suppression_window_s
        self._monotonic = monotonic
        self._emit_transcript = emit_transcript
        self._last_reactive_filler_at = 0.0
        self._stopped = asyncio.Event()

    def note_reactive_filler(self) -> None:
        self._last_reactive_filler_at = self._monotonic()

    def stop(self) -> None:
        self._stopped.set()

    async def run(self) -> None:
        while not self._stopped.is_set():
            try:
                await asyncio.wait_for(
                    self._stopped.wait(),
                    timeout=self._interval_s,
                )
                return
            except asyncio.TimeoutError:
                pass

            since_reactive = self._monotonic() - self._last_reactive_filler_at
            if since_reactive < self._suppression_window_s:
                continue

            line = load_prompt(f"clarification_keepalive_{self._lang}").strip()
            await self._speak_until_stopped(line)

    async def _speak_until_stopped(self, line: str) -> None:
        if self._emit_transcript is not None:
            await self._emit_transcript(line)
        speech_task: asyncio.Future[None] = asyncio.ensure_future(
            self._merchant_speak(line)
        )
        stop_task: asyncio.Task[None] = asyncio.create_task(self._wait_until_stopped())
        try:
            done, _pending = await asyncio.wait(
                {speech_task, stop_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if stop_task in done and not speech_task.done():
                speech_task.cancel()
                with suppress(asyncio.CancelledError):
                    await speech_task
                return
            if speech_task in done:
                stop_task.cancel()
                with suppress(asyncio.CancelledError):
                    await stop_task
                try:
                    await speech_task
                except Exception:
                    log.debug("merchant keepalive TTS failed", exc_info=True)
        except asyncio.CancelledError:
            speech_task.cancel()
            stop_task.cancel()
            with suppress(asyncio.CancelledError):
                await speech_task
            with suppress(asyncio.CancelledError):
                await stop_task
            raise
        finally:
            if not stop_task.done():
                stop_task.cancel()
                with suppress(asyncio.CancelledError):
                    await stop_task
            if not speech_task.done():
                speech_task.cancel()
                with suppress(asyncio.CancelledError):
                    await speech_task

    async def _wait_until_stopped(self) -> None:
        await self._stopped.wait()


__all__ = ["KeepaliveTimer"]
