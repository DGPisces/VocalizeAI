"""dialogue.language — Language routing: detect_user_lang / is_cross_lingual.

Pure functions; stateless, no I/O. The orchestrator persists results
in ``TaskState.user_lang`` / ``TaskState.merchant_lang``.

Design notes:
- ``Lang = Literal["zh", "en"]`` — bilingual only; extend when adding languages.
- ``detect_user_lang``: normalizes STT language codes ("zh"/"zh-CN"/"en"/"en-US"/
  other/None) to ``Lang``; unknown → ``default``.
- ``detect_lang_from_text``: heuristic for the v1 entry point where the
  user provides a free-text task description before any STT signal —
  any Chinese character → "zh"; otherwise "en".
- ``is_cross_lingual``: triggers ``prompts/relay_*.md`` cross-lingual relay path.
- ``pick_merchant_lang`` removed in v1 refactor; merchant_lang is now collected
  explicitly as a preflight slot instead of a heuristic.
"""
from __future__ import annotations

import re
from typing import Literal

Lang = Literal["zh", "en"]

# CJK Unified Ideographs (U+4E00..U+9FFF) — covers everyday Mandarin
# characters. Compiled once at module import.
_CJK_RE = re.compile(r"[一-鿿]")


def detect_lang_from_text(text: str | None, default: Lang = "en") -> Lang:
    """Best-effort language detection for a free-text task description.

    Used at the v1 entry point: the user types a task description before
    any STT lang code exists, and Layer 1 needs to pick the right
    task_planner prompt. Any CJK character → ``"zh"``; otherwise
    ``default`` (typically ``"en"``).
    """
    if text and _CJK_RE.search(text):
        return "zh"
    return default


def detect_user_lang(transcript_lang: str | None, default: Lang = "zh") -> Lang:
    """Map STT language code → orchestrator's normalized ``Lang``.

    Matching rules (startswith, case-sensitive — STT services return lowercase):
    - Starts with "zh" → ``"zh"`` (covers "zh", "zh-CN", "zh-TW", etc.)
    - Starts with "en" → ``"en"`` (covers "en", "en-US", "en-GB", etc.)
    - Other / None → ``default``
    """
    if transcript_lang and transcript_lang.startswith("en"):
        return "en"
    if transcript_lang and transcript_lang.startswith("zh"):
        return "zh"
    return default


def is_cross_lingual(user_lang: Lang, merchant_lang: Lang) -> bool:
    """D-15 cross-lingual relay trigger: user language ≠ merchant language.

    True → orchestrator enables ``prompts/relay_zh.md`` / ``prompts/relay_en.md``
    one-shot LLM translation path; False → both sides share a language, echo directly.
    """
    return user_lang != merchant_lang


__all__ = [
    "Lang",
    "detect_lang_from_text",
    "detect_user_lang",
    "is_cross_lingual",
]
