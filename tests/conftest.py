"""Test fixtures for VocalizeAI.

Auto-applied fixtures here protect tests from local-environment bleed:
- ``_isolate_dotenv``: prevents ``vocalize.config.load_dotenv`` from reading the
  developer's real ``.env`` during tests, which would otherwise leak credentials
  or default values into config-related test cases.

Phase 4 dialogue-orchestrator fixtures (per 04-VALIDATION.md "Wave 0
Requirements" + 04-RESEARCH.md "Wave 0 Gaps"):

- ``fake_user_channel`` — stand-in for ``vocalize.dialogue.user_channel.UserChannel``
  (CONTEXT D-03). Records request/event calls so clarification tests can assert
  pause→ask→resume ordering. Lazy-imports ``ClarificationReply`` so Wave 0
  collection succeeds before Wave 2 implements ``dialogue/user_channel.py``.
- ``recording_audio_transport`` — extends the FakeTransport idiom from
  test_pipeline.py:33-65 with ``pause_outbound`` / ``resume_outbound`` log
  recording (D-04) and an ``output_active`` event for half-duplex AEC tests
  (D-01).
- ``scenario_loader`` — session-scoped passthrough of
  ``tests.dialogue_fixtures.load_scenarios``.
- ``make_scripted_llm`` — module-level helper (NOT a fixture) used by Plan 09
  Task 2's tool round-trip test. Each call to the returned object's
  ``stream_chat`` yields the next scripted chunk list. Lives in conftest so
  downstream test modules can ``from tests.conftest import make_scripted_llm``
  without redefining inline.
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest


@pytest.fixture(autouse=True)
def _isolate_dotenv(monkeypatch: pytest.MonkeyPatch):
    """Stub ``load_dotenv`` and reset the cached ``Config`` singleton each test.

    Two things must be true for tests to be order-independent:

    1. ``vocalize.config.load_dotenv`` is patched to a no-op so the developer's
       real ``.env`` doesn't bleed into tests via ``Config.from_env()``.
    2. ``vocalize.config._config`` (the lazy singleton) is reset before AND
       after each test, so a previously-cached ``Config`` from another test
       doesn't survive the ``monkeypatch.setenv``/``delenv`` calls in this one.

    Tests that want to exercise real ``.env`` loading must override this
    fixture explicitly.
    """
    import vocalize.config

    monkeypatch.setattr(vocalize.config, "load_dotenv", lambda *a, **kw: None)
    vocalize.config.reset_config()
    yield
    vocalize.config.reset_config()


# ---------------------------------------------------------------------------
# Phase 4 — dialogue orchestrator test fixtures
# ---------------------------------------------------------------------------


class _FakeUserChannel:
    """Minimal in-memory ``UserChannel`` Protocol stand-in (CONTEXT D-03 +
    Plan 2026-05-04 preflight refactor).

    Tests pre-populate ``queued_replies`` (clarification) and/or
    ``queued_inputs`` (preflight receive_text) with the answers the user
    "would have given"; each call pops the next one. Replies / events /
    spoken text are recorded for assertion.
    """

    def __init__(self) -> None:
        self.queued_replies: list[str] = []
        self.requests: list[tuple[str, str, float]] = []
        self.events: list[dict[str, Any]] = []
        # Plan 2026-05-04 additions:
        self.queued_inputs: list[tuple[str, str]] = []  # (text, lang) for receive_text
        self.spoken: list[tuple[str, str]] = []         # records speak_text calls

    async def request_clarification(
        self,
        prompt: str,
        lang: str,
        timeout_s: float,
        field: str | None = None,
    ) -> Any:
        self.requests.append((prompt, lang, timeout_s))
        # Lazy import — Wave 0 has no production dialogue/* yet.
        try:
            from vocalize.dialogue.user_channel import ClarificationReply
        except ImportError:
            pytest.skip("awaits Wave 2: vocalize.dialogue.user_channel")

        if not self.queued_replies:
            raise asyncio.TimeoutError(
                "fake_user_channel has no queued reply; tests must pre-populate"
            )
        answer = self.queued_replies.pop(0)
        return ClarificationReply(answer=answer, user_lang=lang, received_at=0.0)

    async def push_event(self, event: dict[str, Any]) -> None:
        self.events.append(event)

    async def receive_text(self) -> tuple[str, str]:
        if not self.queued_inputs:
            raise EOFError(
                "fake_user_channel.queued_inputs exhausted; tests must pre-populate"
            )
        return self.queued_inputs.pop(0)

    async def speak_text(self, text: str, *, lang: str) -> None:
        self.spoken.append((text, lang))


@pytest.fixture
def fake_user_channel() -> _FakeUserChannel:
    """Function-scoped ``UserChannel`` Protocol stand-in.

    Tests assign ``fake_user_channel.queued_replies`` BEFORE invoking
    ``request_clarification`` to script user answers. ``requests`` and
    ``events`` are recorded for assertion.
    """
    return _FakeUserChannel()


class _RecordingAudioTransport:
    """``AudioTransport``-shaped fake that records outbound bytes + pause/resume.

    Extends the FakeTransport pattern from tests/test_pipeline.py:33-65 with
    Phase 4 instrumentation:

    - ``recorded_output``: every chunk written via ``output_stream`` is appended.
    - ``outbound_log``: each ``pause_outbound`` / ``resume_outbound`` call name.
    - ``output_active`` (asyncio.Event): set while the AI is speaking; the
      half-duplex AEC gate consults this on the input side (D-01).
    """

    sample_rate: int = 16000
    channels: int = 1
    encoding: str = "pcm_s16le"

    def __init__(self) -> None:
        self.recorded_output: list[bytes] = []
        self.outbound_log: list[str] = []
        self.closed = False
        self._input_done = asyncio.Event()
        self.output_active = asyncio.Event()

    async def input_stream(self) -> AsyncIterator[bytes]:
        try:
            await self._input_done.wait()
        except asyncio.CancelledError:
            raise
        if False:  # pragma: no cover - keep function an async generator
            yield b""  # type: ignore[unreachable]

    async def output_stream(self, audio: AsyncIterator[bytes]) -> None:
        async for chunk in audio:
            self.recorded_output.append(chunk)

    async def pause_outbound(self) -> None:
        self.outbound_log.append("pause_outbound")

    async def resume_outbound(self) -> None:
        self.outbound_log.append("resume_outbound")

    def set_output_active(self, active: bool) -> None:
        if active:
            self.output_active.set()
        else:
            self.output_active.clear()

    async def close(self) -> None:
        self.closed = True
        self._input_done.set()


@pytest.fixture
def recording_audio_transport() -> _RecordingAudioTransport:
    """Function-scoped recording transport for clarification + half-duplex tests."""
    return _RecordingAudioTransport()


@pytest.fixture(scope="session")
def scenario_loader():
    """Session-scoped passthrough to ``tests.dialogue_fixtures.load_scenarios``."""
    from tests.dialogue_fixtures import load_scenarios

    return load_scenarios


# ---------------------------------------------------------------------------
# make_scripted_llm — multi-call FakeLLM helper for Plan 09 tool round-trip
# ---------------------------------------------------------------------------


def make_scripted_llm(*call_chunks: list[Any]) -> Any:
    """Return a FakeLLM-style object that yields a different chunk list per call.

    Plan 04-09 (tool round-trip) needs to assert that the LLM is invoked TWICE:
    once for the tool call (yields ToolCallDelta + FinishChunk(reason="tool_calls"))
    and once for the post-tool natural-language reply (yields TextDelta +
    FinishChunk(reason="stop")). The existing ``tests/test_pipeline.py::FakeLLM``
    only takes a single script. This helper wraps that idea but advances
    through ``call_chunks`` one entry per ``stream_chat`` invocation.

    Lazy-imports ``LLMChunk``/``TextDelta``/``ToolCallDelta``/``FinishChunk``
    so Wave 0 callers (which only exist as skeletons) don't import-fail when
    the production types haven't yet been touched. The helper itself works
    on TODAY's ``vocalize.llm.base`` — the lazy-import is only to surface a
    clearer skip if the module ever moves.
    """
    try:
        # Imports validated lazily so a future refactor that relocates types
        # is reported as a clean skip rather than a collection-time crash.
        from vocalize.llm.base import (  # noqa: F401
            ChatMessage,
            FinishChunk,
            LLMChunk,
            TextDelta,
            ToolCallDelta,
            ToolDef,
        )
    except ImportError:
        pytest.skip("awaits Wave 2: vocalize.llm.base ToolCall/ChatMessage extension")

    class _ScriptedMultiCallLLM:
        def __init__(self, scripts: list[list[Any]]) -> None:
            self._scripts = scripts
            self._calls: int = 0
            self.calls: list[list[Any]] = []  # records messages per stream_chat

        async def stream_chat(
            self,
            messages: list[Any],
            tools: list[Any] | None = None,
        ) -> AsyncIterator[Any]:
            self.calls.append(list(messages))
            if self._calls >= len(self._scripts):
                raise IndexError("scripted LLM exhausted")
            script = self._scripts[self._calls]
            self._calls += 1
            for chunk in script:
                yield chunk

    return _ScriptedMultiCallLLM(list(call_chunks))


# ---------------------------------------------------------------------------
# Task 20: Shared LLM fixtures for integration tests
# ---------------------------------------------------------------------------


@pytest.fixture
def llm_client():
    """Real LLM client for integration tests. Requires OPENAI_API_KEY env var.

    Returns an OpenAICompatClient configured from app Config. Tests that
    depend on a real LLM should use this fixture — it skips cleanly when
    the API key is not set, allowing CI to run the rest of the suite.
    """
    import os

    from vocalize.config import Config
    from vocalize.llm.openai_compat import LLMServiceError, OpenAICompatClient

    if not os.environ.get("OPENAI_API_KEY"):
        pytest.skip("OPENAI_API_KEY not set")
    try:
        return OpenAICompatClient.from_app_config(Config.from_env())
    except LLMServiceError as exc:
        pytest.skip(f"LLM client unavailable: {exc}")


@pytest.fixture
def judge_client(llm_client):
    """LLM client for LLM-as-judge evaluations.

    Same underlying client as llm_client, with a semantic name for
    judge-quality test contexts.
    """
    return llm_client


# ---------------------------------------------------------------------------
# Plan B1: fake VoicePipeline factory for WS integration tests
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_voice_pipeline_factory():
    """Yields a callable that builds a fresh ``VoicePipeline`` backed by the
    same fake STT / LLM / TTS used in ``tests/test_pipeline.py``.

    Class names match what test_pipeline.py defines (``FakeSTT`` /
    ``FakeLLM`` / ``FakeTTS`` — no leading underscore). VoicePipeline
    requires a ``system_prompt`` argument; we pass an empty string
    because the orchestrator bypasses the pipeline's own messages list.
    """
    from tests.test_pipeline import FakeLLM, FakeSTT, FakeTTS
    from vocalize.pipeline import VoicePipeline

    def build(transport):
        return VoicePipeline(
            transport=transport,
            stt=FakeSTT([]),
            llm=FakeLLM([]),
            tts=FakeTTS([]),
            system_prompt="",
        )

    yield build
