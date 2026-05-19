"""Layer 5 cross-lingual text relay.

v1.0 RC exposes merchant-to-user translation only. Cross-lingual user
takeover is deferred: takeover text is treated as merchant-language TTS input.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from collections.abc import AsyncIterator
from typing import Literal, Protocol

from vocalize.dialogue.prompts import load_prompt
from vocalize.llm.base import ChatMessage, LLMChunk

log = logging.getLogger(__name__)

__all__ = ["RelayResult", "merchant_text_to_user_lang", "user_to_merchant"]


@dataclass(frozen=True)
class RelayResult:
    """Outcome of a relay call."""

    translated: str | None
    skipped: bool = False
    failed: bool = False


class _RelayLLM(Protocol):
    def stream_chat(
        self,
        *,
        messages: list[ChatMessage],
    ) -> AsyncIterator[LLMChunk]: ...


async def merchant_text_to_user_lang(
    text: str,
    *,
    src: Literal["zh", "en"],
    dst: Literal["zh", "en"],
    llm: _RelayLLM,
) -> RelayResult:
    """Translate merchant text into the user's language without blocking calls."""
    if src == dst:
        return RelayResult(translated=text, skipped=True)

    prompt = load_prompt(
        f"relay_{dst}",
        src_lang=src,
        dst_lang=dst,
    )
    messages = [
        ChatMessage(role="system", content=prompt),
        ChatMessage(role="user", content=text),
    ]
    pieces: list[str] = []
    try:
        async for chunk in llm.stream_chat(messages=messages):
            piece = getattr(chunk, "text", None)
            if piece:
                pieces.append(piece)
    except Exception:
        log.exception("relay LLM failed; continuing without translation")
        return RelayResult(translated=None, failed=True)

    return RelayResult(translated="".join(pieces).strip())


async def user_to_merchant(
    text: str,
    *,
    src: Literal["zh", "en"],
    dst: Literal["zh", "en"],
    llm: _RelayLLM,
) -> RelayResult:
    """Translate user text into merchant language for TTS playback."""
    if src == dst:
        return RelayResult(translated=text, skipped=True)

    prompt = load_prompt(
        f"relay_{dst}",
        src_lang=src,
        dst_lang=dst,
    )
    messages = [
        ChatMessage(role="system", content=prompt),
        ChatMessage(role="user", content=text),
    ]
    pieces: list[str] = []
    try:
        async for chunk in llm.stream_chat(messages=messages):
            piece = getattr(chunk, "text", None)
            if piece:
                pieces.append(piece)
    except Exception:
        log.exception("relay LLM failed; continuing without translation")
        return RelayResult(translated=None, failed=True)

    return RelayResult(translated="".join(pieces).strip())
