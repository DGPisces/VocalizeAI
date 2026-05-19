"""dialogue.user_channel protocol + impl tests.

Covers Plan 2026-05-04 preflight refactor additions: receive_text + speak_text
methods that preflight uses to do user-side text/voice I/O without depending
on a VoicePipeline.
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest

from vocalize.dialogue.state import TaskPhase
from vocalize.dialogue.user_channel import (
    LocalMicUserChannel,
    TextUserChannel,
    UserChannel,
    WebSocketUserChannel,
)


def test_user_channel_protocol_has_receive_text_and_speak_text() -> None:
    """Protocol surface check — both methods MUST be on UserChannel.

    Locked here because preflight (post-refactor) calls these by name; if a
    future contributor accidentally drops them from the Protocol, runtime
    isinstance(..., UserChannel) checks would still pass and bugs would only
    surface on actual demo runs. This test fails at import-time mismatch.
    """
    assert hasattr(UserChannel, "receive_text"), \
        "UserChannel.receive_text missing — preflight refactor depends on it"
    assert hasattr(UserChannel, "speak_text"), \
        "UserChannel.speak_text missing — preflight refactor depends on it"


def test_text_user_channel_satisfies_protocol_runtime_check() -> None:
    """TextUserChannel must remain a runtime UserChannel after additions."""
    assert isinstance(TextUserChannel(), UserChannel)


# ---------------------------------------------------------------------------
# TextUserChannel.receive_text + speak_text — stdin/stdout impls
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_text_user_channel_receive_text_reads_stdin_one_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """receive_text must consume one stdin line and return (text, lang).

    Default lang is "zh" because the preflight zh prompt is the historical
    default (PROJECT.md L13 Chinese-first product). English-speaking users
    select via UI / future detect_user_lang heuristic; not preflight's job.
    """
    monkeypatch.setattr("builtins.input", lambda *_a, **_kw: "我想订海底捞")
    channel = TextUserChannel()

    text, lang = await channel.receive_text()

    assert text == "我想订海底捞"
    assert lang == "zh"


@pytest.mark.asyncio
async def test_text_user_channel_receive_text_strips_whitespace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Trailing newline + leading spaces both must be stripped — match
    TextUserChannel.request_clarification semantics so preflight and
    clarification behave identically on edge whitespace."""
    monkeypatch.setattr("builtins.input", lambda *_a, **_kw: "  hello world  \n")
    channel = TextUserChannel()

    text, _lang = await channel.receive_text()

    assert text == "hello world"


@pytest.mark.asyncio
async def test_text_user_channel_receive_text_empty_returns_empty_string(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Codex P2 (2026-05-04): an empty / whitespace-only stdin line must
    NOT raise EOFError — that would propagate to run_preflight as
    DialogueOrchestratorError ('user channel exhausted') and abort the
    whole session on a stray Enter press. Instead return ("", lang) and
    let preflight's belt-and-braces ``if not user_text: continue`` skip
    it and re-prompt."""
    monkeypatch.setattr("builtins.input", lambda *_a, **_kw: "   ")
    channel = TextUserChannel()

    text, lang = await channel.receive_text()

    assert text == ""
    assert lang == "zh"


@pytest.mark.asyncio
async def test_text_user_channel_receive_text_real_eof_propagates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When stdin is genuinely closed (Ctrl-D), the underlying input()
    raises EOFError; receive_text propagates it unchanged. preflight
    then maps it to DialogueOrchestratorError ('user channel exhausted')
    — the legitimate "channel really gone" path."""
    def _raise_eof(*_a, **_kw):
        raise EOFError("simulated Ctrl-D")
    monkeypatch.setattr("builtins.input", _raise_eof)
    channel = TextUserChannel()

    with pytest.raises(EOFError):
        await channel.receive_text()


@pytest.mark.asyncio
async def test_text_user_channel_receive_text_prompt_goes_to_stdout(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The '[用户 → AI] ' prompt MUST be printed to STDOUT, not delivered
    via input(prompt). Mac libedit/readline writes input()'s prompt arg
    to stderr, which means operators who redirect stderr (e.g. ``2> log``)
    would see no prompt at all and the program would appear hung. This
    test pins the explicit-stdout-print behavior so we never regress.
    """
    monkeypatch.setattr("builtins.input", lambda *_a, **_kw: "hi")
    channel = TextUserChannel()

    await channel.receive_text()

    captured = capsys.readouterr()
    assert "[用户 → AI]" in captured.out, (
        "input prompt did not reach stdout — likely regressed to "
        "input(prompt), which Mac libedit writes to stderr"
    )


@pytest.mark.asyncio
async def test_text_user_channel_speak_text_prints_with_prefix(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """speak_text writes one line with the [AI → 用户] prefix that
    request_clarification already uses — operator visually distinguishes
    AI lines from log noise."""
    channel = TextUserChannel()

    await channel.speak_text("好的，我来帮您预订", lang="zh")

    captured = capsys.readouterr()
    assert "[AI → 用户] 好的，我来帮您预订" in captured.out


@pytest.mark.asyncio
async def test_text_user_channel_speak_text_lang_param_does_not_change_output(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """lang is bookkeeping only for text impl (no TTS to pick voice for);
    output is the literal text. Lock this so future contributors do not
    add inadvertent prefix variation."""
    channel = TextUserChannel()

    await channel.speak_text("Hello there", lang="en")

    captured = capsys.readouterr()
    assert captured.out.strip() == "[AI → 用户] Hello there"


# ---------------------------------------------------------------------------
# LocalMicUserChannel.receive_text + speak_text — mic+STT and TTS+speaker
# ---------------------------------------------------------------------------


from vocalize.stt.base import Transcript  # noqa: E402
from vocalize.tts.base import TextChunk  # noqa: E402


class _MicChanFakeTransport:
    sample_rate = 16000
    channels = 1
    encoding = "pcm_s16le"

    def __init__(self) -> None:
        self.recorded_audio: list[bytes] = []

    async def input_stream(self) -> AsyncIterator[bytes]:
        yield b"\x00" * 320
        await asyncio.sleep(0)

    async def output_stream(self, audio: AsyncIterator[bytes]) -> None:
        async for chunk in audio:
            self.recorded_audio.append(chunk)

    async def close(self) -> None:  # pragma: no cover
        pass


class _MicChanFakeSTT:
    def __init__(self, transcripts: list[Transcript]) -> None:
        self._transcripts = transcripts

    def stream_transcribe(
        self, audio_chunks: AsyncIterator[bytes]
    ) -> AsyncIterator[Transcript]:
        return self._iter()

    async def _iter(self) -> AsyncIterator[Transcript]:
        for t in self._transcripts:
            yield t
            await asyncio.sleep(0)


class _MicChanFakeTTS:
    def __init__(self) -> None:
        self.synthesized: list[tuple[str, str]] = []

    def stream_synthesize(
        self, text_chunks: AsyncIterator[TextChunk]
    ) -> AsyncIterator[bytes]:
        return self._iter(text_chunks)

    async def _iter(
        self, text_chunks: AsyncIterator[TextChunk]
    ) -> AsyncIterator[bytes]:
        async for chunk in text_chunks:
            self.synthesized.append((chunk.text, chunk.language))
            yield b"AUDIO:" + chunk.text.encode("utf-8")


def _final(text: str, lang: str | None = "zh") -> Transcript:
    return Transcript(
        text=text, is_final=True, confidence=0.9,
        start_time=0.0, end_time=1.0, utterance_id=1, language=lang,
    )


@pytest.mark.asyncio
async def test_local_mic_user_channel_receive_text_returns_first_final() -> None:
    """receive_text drives STT until first non-empty final Transcript."""
    transport = _MicChanFakeTransport()
    stt = _MicChanFakeSTT([
        _final("", lang="zh"),
        _final("我想订海底捞", lang="zh"),
        _final("ignored second", lang="zh"),
    ])
    tts = _MicChanFakeTTS()
    channel = LocalMicUserChannel(transport, stt, tts)

    text, lang = await channel.receive_text()

    assert text == "我想订海底捞"
    assert lang == "zh"


@pytest.mark.asyncio
async def test_local_mic_user_channel_receive_text_strips_whitespace() -> None:
    transport = _MicChanFakeTransport()
    stt = _MicChanFakeSTT([_final("   hello   ", lang="en")])
    tts = _MicChanFakeTTS()
    channel = LocalMicUserChannel(transport, stt, tts)

    text, lang = await channel.receive_text()

    assert text == "hello"
    assert lang == "en"


@pytest.mark.asyncio
async def test_local_mic_user_channel_receive_text_lang_default_zh_when_none() -> None:
    """STT may yield language=None on uncertain detection — preflight
    must always get a concrete 'zh' or 'en'. Default to 'zh' (Chinese-first
    product policy)."""
    transport = _MicChanFakeTransport()
    stt = _MicChanFakeSTT([_final("hello", lang=None)])
    tts = _MicChanFakeTTS()
    channel = LocalMicUserChannel(transport, stt, tts)

    text, lang = await channel.receive_text()

    assert text == "hello"
    assert lang == "zh"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "stt_lang, expected",
    [
        ("en", "en"),
        ("en-US", "en"),  # Codex P2 — locale tag must normalize to "en"
        ("en-GB", "en"),
        ("zh", "zh"),
        ("zh-CN", "zh"),
        ("zh-TW", "zh"),
        ("ja", "zh"),     # unsupported → default "zh"
        (None, "zh"),     # uncertain detection → default "zh"
    ],
)
async def test_local_mic_user_channel_receive_text_normalizes_locale_codes(
    stt_lang: str | None, expected: str,
) -> None:
    """Codex P2 (2026-05-04): STT services often return locale-tagged
    codes ('en-US', 'zh-CN'); pre-fix the exact-match check defaulted
    every locale tag to 'zh', giving English-speaking users Chinese
    prompt behavior. Verify normalization via the shared
    detect_user_lang helper handles startswith('en')/('zh') correctly.
    """
    transport = _MicChanFakeTransport()
    stt = _MicChanFakeSTT([_final("hello", lang=stt_lang)])
    tts = _MicChanFakeTTS()
    channel = LocalMicUserChannel(transport, stt, tts)

    _text, lang = await channel.receive_text()

    assert lang == expected


@pytest.mark.asyncio
async def test_local_mic_user_channel_receive_text_no_final_raises_eof() -> None:
    """STT iterator ends with no final transcript — surface EOFError so
    preflight outer loop can react (matches TextUserChannel behavior)."""
    transport = _MicChanFakeTransport()
    stt = _MicChanFakeSTT([])
    tts = _MicChanFakeTTS()
    channel = LocalMicUserChannel(transport, stt, tts)

    with pytest.raises(EOFError):
        await channel.receive_text()


@pytest.mark.asyncio
async def test_local_mic_user_channel_speak_text_runs_tts_to_transport() -> None:
    """speak_text packages text into one final TextChunk and pumps TTS
    audio through transport.output_stream."""
    transport = _MicChanFakeTransport()
    stt = _MicChanFakeSTT([])
    tts = _MicChanFakeTTS()
    channel = LocalMicUserChannel(transport, stt, tts)

    await channel.speak_text("好的", lang="zh")

    assert tts.synthesized == [("好的", "zh")]
    assert transport.recorded_audio == [b"AUDIO:\xe5\xa5\xbd\xe7\x9a\x84"]


# -- Task 8: WebSocketUserChannel tests ---------------------------------------


def _make_channel(
    *,
    text_inputs: list[tuple[str, str | None]] | None = None,
    ack_inputs: list[str] | None = None,
) -> tuple[WebSocketUserChannel, list[dict[str, Any]]]:
    text_q: asyncio.Queue = asyncio.Queue()
    ack_q: asyncio.Queue = asyncio.Queue()
    sent: list[dict[str, Any]] = []

    async def send_json(frame: dict[str, Any]) -> None:
        sent.append(frame)

    for ti in text_inputs or []:
        text_q.put_nowait(ti)
    for av in ack_inputs or []:
        ack_q.put_nowait(av)

    channel = WebSocketUserChannel(
        send_json=send_json,
        text_input_queue=text_q,
        ack_clarification_queue=ack_q,
        transport=None,
        stt=None,
        tts=None,
    )
    return channel, sent


def _assert_ai_transcript_frame(
    frame: dict[str, Any],
    *,
    text: str,
    lang: str,
) -> None:
    assert frame["type"] == "transcript_update"
    assert frame["role"] == "ai_to_user"
    assert frame["text"] == text
    assert frame["lang"] == lang
    assert frame["is_final"] is True
    assert frame["subtype"] == "original"
    assert frame["parent_id"] is None
    assert frame["segment_id"] is None
    assert frame["id"] and len(str(frame["id"])) >= 8
    assert frame["created_at"] and "T" in str(frame["created_at"])


def test_websocket_channel_implements_user_channel_protocol() -> None:
    channel, _ = _make_channel()
    assert isinstance(channel, UserChannel)


async def test_receive_text_returns_next_queued_input() -> None:
    channel, _ = _make_channel(text_inputs=[("帮我订海底捞", "zh")])
    text, lang = await channel.receive_text()
    assert text == "帮我订海底捞"
    assert lang == "zh"


async def test_receive_text_defaults_lang_when_hint_missing() -> None:
    channel, _ = _make_channel(text_inputs=[("hello", None)])
    text, lang = await channel.receive_text()
    assert text == "hello"
    assert lang == "en"


async def test_receive_text_strips_whitespace() -> None:
    channel, _ = _make_channel(text_inputs=[("   你好  ", "zh")])
    text, _ = await channel.receive_text()
    assert text == "你好"


@pytest.mark.asyncio
async def test_user_takeover_text_routes_to_takeover_queue() -> None:
    text_q: asyncio.Queue = asyncio.Queue()
    ack_q: asyncio.Queue = asyncio.Queue()
    hint_q: asyncio.Queue = asyncio.Queue()
    takeover_q: asyncio.Queue = asyncio.Queue()
    text_q.put_nowait(("yes please", "en", "user_takeover"))
    sent: list[dict[str, Any]] = []

    async def send_json(frame: dict[str, Any]) -> None:
        sent.append(frame)

    channel = WebSocketUserChannel(
        send_json=send_json,
        text_input_queue=text_q,
        ack_clarification_queue=ack_q,
        merchant_hint_queue=hint_q,
        user_takeover_queue=takeover_q,
        get_phase=lambda: TaskPhase.EXECUTION_ACTIVE,
    )

    out = await channel.dispatch_one_input()

    assert out is None
    text, lang, passthrough_id = takeover_q.get_nowait()
    assert (text, lang) == ("yes please", "en")
    assert isinstance(passthrough_id, str) and passthrough_id
    assert hint_q.empty()


@pytest.mark.asyncio
async def test_default_text_in_call_phase_routes_to_hint_queue_and_emits_supplement() -> None:
    text_q: asyncio.Queue = asyncio.Queue()
    ack_q: asyncio.Queue = asyncio.Queue()
    hint_q: asyncio.Queue = asyncio.Queue()
    takeover_q: asyncio.Queue = asyncio.Queue()
    sent: list[dict[str, Any]] = []

    async def send_json(frame: dict[str, Any]) -> None:
        sent.append(frame)

    text_q.put_nowait(("they have a private room", "en", "default"))

    channel = WebSocketUserChannel(
        send_json=send_json,
        text_input_queue=text_q,
        ack_clarification_queue=ack_q,
        merchant_hint_queue=hint_q,
        user_takeover_queue=takeover_q,
        get_phase=lambda: TaskPhase.EXECUTION_ACTIVE,
    )

    out = await channel.dispatch_one_input()

    assert out is None
    assert hint_q.get_nowait() == ("they have a private room", "en")
    assert takeover_q.empty()
    assert any(
        frame["type"] == "transcript_update"
        and frame["role"] == "user_supplement"
        and frame["subtype"] == "user_supplement"
        and frame["text"] == "they have a private room"
        for frame in sent
    )


@pytest.mark.asyncio
async def test_default_text_during_clarification_routes_to_hint_queue() -> None:
    text_q: asyncio.Queue = asyncio.Queue()
    ack_q: asyncio.Queue = asyncio.Queue()
    hint_q: asyncio.Queue = asyncio.Queue()
    takeover_q: asyncio.Queue = asyncio.Queue()
    sent: list[dict[str, Any]] = []

    async def send_json(frame: dict[str, Any]) -> None:
        sent.append(frame)

    text_q.put_nowait(("actually ask for a booth", "en", "default"))

    channel = WebSocketUserChannel(
        send_json=send_json,
        text_input_queue=text_q,
        ack_clarification_queue=ack_q,
        merchant_hint_queue=hint_q,
        user_takeover_queue=takeover_q,
        get_phase=lambda: TaskPhase.AWAIT_USER_CLARIFICATION,
    )

    out = await channel.dispatch_one_input()

    assert out is None
    assert hint_q.get_nowait() == ("actually ask for a booth", "en")
    assert ack_q.empty()
    assert any(
        frame["type"] == "transcript_update"
        and frame["role"] == "user_supplement"
        and frame["text"] == "actually ask for a booth"
        for frame in sent
    )


@pytest.mark.asyncio
async def test_takeover_text_emits_takeover_passthrough_transcript() -> None:
    text_q: asyncio.Queue = asyncio.Queue()
    ack_q: asyncio.Queue = asyncio.Queue()
    hint_q: asyncio.Queue = asyncio.Queue()
    takeover_q: asyncio.Queue = asyncio.Queue()
    sent: list[dict[str, Any]] = []

    async def send_json(frame: dict[str, Any]) -> None:
        sent.append(frame)

    text_q.put_nowait(("yes please", "en", "user_takeover"))

    channel = WebSocketUserChannel(
        send_json=send_json,
        text_input_queue=text_q,
        ack_clarification_queue=ack_q,
        merchant_hint_queue=hint_q,
        user_takeover_queue=takeover_q,
        get_phase=lambda: TaskPhase.EXECUTION_ACTIVE,
    )

    await channel.dispatch_one_input()

    text, lang, passthrough_id = takeover_q.get_nowait()
    assert (text, lang) == ("yes please", "en")
    assert isinstance(passthrough_id, str) and passthrough_id
    assert any(
        frame["type"] == "transcript_update"
        and frame["role"] == "user_takeover_passthrough"
        and frame["subtype"] == "user_takeover_passthrough"
        for frame in sent
    )


async def test_speak_text_emits_transcript_update_frame() -> None:
    channel, sent = _make_channel()
    await channel.speak_text("好的，我先记一下", lang="zh")
    assert len(sent) == 1
    _assert_ai_transcript_frame(sent[0], text="好的，我先记一下", lang="zh")


@pytest.mark.asyncio
async def test_speak_text_emits_full_b3a_transcript_shape() -> None:
    sent: list[dict] = []

    async def send_json(frame):
        sent.append(frame)

    channel = WebSocketUserChannel(
        send_json=send_json,
        text_input_queue=asyncio.Queue(),
        ack_clarification_queue=asyncio.Queue(),
    )
    await channel.speak_text("hi", lang="en")

    assert len(sent) == 1
    _assert_ai_transcript_frame(sent[0], text="hi", lang="en")


async def test_push_event_state_diff_emits_state_update() -> None:
    channel, sent = _make_channel()
    await channel.push_event({"event": "state_update", "diff": {"phase": "collecting"}})
    assert sent == [{"type": "state_update", "diff": {"phase": "collecting"}}]


async def test_push_event_readiness_emits_readiness_change() -> None:
    channel, sent = _make_channel()
    await channel.push_event({
        "event": "readiness_change",
        "passed": True,
        "missing_critical": [],
        "confidence": 0.9,
    })
    assert sent == [{
        "type": "readiness_change",
        "passed": True,
        "missing_critical": [],
        "confidence": 0.9,
    }]


async def test_push_event_phase_change_emits_phase_change_frame() -> None:
    channel, sent = _make_channel()
    await channel.push_event({
        "event": "phase_change",
        "previous": "execution_active",
        "current": "post_call_review",
    })
    assert sent == [{
        "type": "phase_change",
        "previous": "execution_active",
        "current": "post_call_review",
    }]


async def test_push_event_uncertain_assumption_added_emits_frame() -> None:
    channel, sent = _make_channel()
    payload = {"id": "a-1", "slot": "x", "assumed_value": 1}
    await channel.push_event({
        "event": "uncertain_assumption_added",
        "assumption": payload,
    })
    assert sent == [{
        "type": "uncertain_assumption_added",
        "assumption": payload,
    }]


async def test_push_event_pending_callback_added_emits_frame() -> None:
    channel, sent = _make_channel()
    payload = {"id": "cb-1", "assumption_id": "a-1", "status": "queued"}
    await channel.push_event({
        "event": "pending_callback_added",
        "callback": payload,
    })
    assert sent == [{
        "type": "pending_callback_added",
        "callback": payload,
    }]


async def test_push_event_escalation_warning_emits_frame() -> None:
    channel, sent = _make_channel()
    await channel.push_event({
        "event": "escalation_warning",
        "reason": "merchant_impatience",
        "holds_used": 2,
        "message_zh": "商家催了三次，先挂电话",
        "message_en": "Merchant interrupted 3 times; ending call",
    })
    assert sent == [{
        "type": "escalation_warning",
        "reason": "merchant_impatience",
        "holds_used": 2,
        "message_zh": "商家催了三次，先挂电话",
        "message_en": "Merchant interrupted 3 times; ending call",
    }]


async def test_push_event_transcript_update_emits_transcript_frame() -> None:
    channel, sent = _make_channel()
    await channel.push_event({
        "event": "transcript_update",
        "id": "t-1",
        "role": "merchant_to_ai",
        "text": "Hello",
        "lang": "en",
        "is_final": True,
        "subtype": "original",
        "parent_id": None,
        "segment_id": None,
        "created_at": "2026-05-07T00:00:00+00:00",
    })

    assert sent == [{
        "type": "transcript_update",
        "id": "t-1",
        "role": "merchant_to_ai",
        "text": "Hello",
        "lang": "en",
        "is_final": True,
        "subtype": "original",
        "parent_id": None,
        "segment_id": None,
        "created_at": "2026-05-07T00:00:00+00:00",
    }]


async def test_push_event_unknown_event_falls_back_to_state_update() -> None:
    """Lifecycle events the channel doesn't have a dedicated frame for
    (e.g. ``task_planning_started``, ``transition``, ``completed``,
    ``failed``) MUST round-trip as a generic ``state_update`` so the
    frontend can still render lifecycle chrome. Without this fallback
    most orchestrator emissions would be silently dropped.
    """
    channel, sent = _make_channel()
    await channel.push_event({"event": "task_planning_started", "task_text": "x"})
    assert sent == [{
        "type": "state_update",
        "diff": {"event": "task_planning_started", "task_text": "x"},
    }]


# -- Task 9: request_clarification tests --------------------------------------


async def test_request_clarification_emits_frame_and_awaits_ack() -> None:
    channel, sent = _make_channel(ack_inputs=["晚上7点"])
    reply = await channel.request_clarification(
        prompt="请问您想约几点？",
        lang="zh",
        timeout_s=5.0,
        field="reservation_time",
    )
    assert reply.answer == "晚上7点"
    assert reply.user_lang == "zh"
    assert sent == [{
        "type": "clarification_request",
        "field": "reservation_time",
        "question": "请问您想约几点？",
        "lang": "zh",
        "timeout_s": 5.0,
    }]


async def test_request_clarification_strips_ack_whitespace() -> None:
    channel, _ = _make_channel(ack_inputs=["   ok  "])
    reply = await channel.request_clarification(
        prompt="?",
        lang="en",
        timeout_s=5.0,
    )
    assert reply.answer == "ok"


async def test_request_clarification_times_out() -> None:
    channel, _ = _make_channel()  # ack_q is empty
    with pytest.raises(asyncio.TimeoutError):
        await channel.request_clarification(
            prompt="?",
            lang="zh",
            timeout_s=0.05,
        )


# ---------------------------------------------------------------------------
# WebSocketUserChannel B2 audio-backed preflight
# ---------------------------------------------------------------------------


class _WebChanTransport(_MicChanFakeTransport):
    def __init__(self) -> None:
        super().__init__()
        self.outbound_role: str | None = None

    def set_outbound_role(self, role: str) -> None:
        self.outbound_role = role


class _CancelAwareQueue(asyncio.Queue):
    def __init__(self) -> None:
        super().__init__()
        self.cancelled_get = False

    async def get(self):
        try:
            return await super().get()
        except asyncio.CancelledError:
            self.cancelled_get = True
            raise


class _BlockingWebChanSTT:
    def __init__(self, transcript: Transcript) -> None:
        self.transcript = transcript
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.closed = False
        self.saw_audio = False

    def stream_transcribe(
        self,
        audio_chunks: AsyncIterator[bytes],
    ) -> AsyncIterator[Transcript]:
        return self._iter(audio_chunks)

    async def _iter(self, audio_chunks: AsyncIterator[bytes]) -> AsyncIterator[Transcript]:
        try:
            self.started.set()
            async for block in audio_chunks:
                if block:
                    self.saw_audio = True
                break
            await self.release.wait()
            yield self.transcript
        finally:
            self.closed = True


class _FailingWebChanSTT:
    def __init__(self, exc: Exception) -> None:
        self.exc = exc
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.closed = False

    def stream_transcribe(
        self,
        audio_chunks: AsyncIterator[bytes],
    ) -> AsyncIterator[Transcript]:
        return self._iter(audio_chunks)

    async def _iter(self, audio_chunks: AsyncIterator[bytes]) -> AsyncIterator[Transcript]:
        try:
            self.started.set()
            async for _block in audio_chunks:
                break
            await self.release.wait()
            raise self.exc
            yield _final("unreachable")
        finally:
            self.closed = True


class _TransportAwareWebChanSTT:
    def __init__(self, transcript: Transcript) -> None:
        self.transcript = transcript
        self.transport = None
        self.started = asyncio.Event()
        self.closed = False

    def stream_transcribe(
        self,
        audio_chunks: AsyncIterator[bytes],
        *,
        transport=None,
    ) -> AsyncIterator[Transcript]:
        self.transport = transport
        return self._iter(audio_chunks)

    async def _iter(self, audio_chunks: AsyncIterator[bytes]) -> AsyncIterator[Transcript]:
        try:
            self.started.set()
            async for _block in audio_chunks:
                break
            yield self.transcript
        finally:
            self.closed = True


@pytest.mark.asyncio
async def test_receive_text_in_collecting_uses_text_queue_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    text_q: asyncio.Queue = asyncio.Queue()
    ack_q: asyncio.Queue = asyncio.Queue()
    text_q.put_nowait(("hello", "en"))

    audio_called = False

    class _ExplodingTransport:
        def input_stream(self):
            nonlocal audio_called
            audio_called = True
            raise AssertionError("audio path must not be called in COLLECTING")

    async def _noop_send(_f):
        pass

    channel = WebSocketUserChannel(
        send_json=_noop_send,
        text_input_queue=text_q,
        ack_clarification_queue=ack_q,
        transport=_ExplodingTransport(),
        stt=object(),
        tts=object(),
        get_phase=lambda: TaskPhase.COLLECTING,
    )

    text, lang = await channel.receive_text()

    assert (text, lang) == ("hello", "en")
    assert audio_called is False


@pytest.mark.asyncio
async def test_websocket_user_channel_receive_text_delayed_audio_text_first() -> None:
    sent: list[dict[str, object]] = []
    text_q = asyncio.Queue()
    ack_q: asyncio.Queue = asyncio.Queue()
    transport = _WebChanTransport()
    stt = _BlockingWebChanSTT(_final("ignored audio", lang="zh"))
    tts = _MicChanFakeTTS()
    channel = WebSocketUserChannel(
        send_json=lambda frame: _record_frame(sent, frame),
        text_input_queue=text_q,
        ack_clarification_queue=ack_q,
        transport=transport,
        stt=stt,
        tts=tts,
        get_phase=lambda: TaskPhase.EXECUTION_ACTIVE,
    )

    task = asyncio.create_task(channel.receive_text())
    await asyncio.wait_for(stt.started.wait(), timeout=1.0)
    await text_q.put(("typed answer", "en"))

    text, lang = await asyncio.wait_for(task, timeout=1.0)

    assert (text, lang) == ("typed answer", "en")
    assert stt.closed is True


@pytest.mark.asyncio
async def test_websocket_user_channel_receive_text_delayed_text_audio_first() -> None:
    sent: list[dict[str, object]] = []
    text_q = _CancelAwareQueue()
    ack_q: asyncio.Queue = asyncio.Queue()
    transport = _WebChanTransport()
    stt = _BlockingWebChanSTT(_final("我想订今晚七点", lang="zh"))
    tts = _MicChanFakeTTS()
    channel = WebSocketUserChannel(
        send_json=lambda frame: _record_frame(sent, frame),
        text_input_queue=text_q,
        ack_clarification_queue=ack_q,
        transport=transport,
        stt=stt,
        tts=tts,
        get_phase=lambda: TaskPhase.EXECUTION_ACTIVE,
    )

    task = asyncio.create_task(channel.receive_text())
    await asyncio.wait_for(stt.started.wait(), timeout=1.0)
    stt.release.set()

    text, lang = await asyncio.wait_for(task, timeout=1.0)

    assert (text, lang) == ("我想订今晚七点", "zh")
    assert stt.saw_audio is True
    assert text_q.cancelled_get is True


@pytest.mark.asyncio
async def test_websocket_user_channel_audio_stt_error_waits_for_typed_text() -> None:
    sent: list[dict[str, object]] = []
    text_q = asyncio.Queue()
    ack_q: asyncio.Queue = asyncio.Queue()
    transport = _WebChanTransport()
    stt = _FailingWebChanSTT(RuntimeError("stt backend down"))
    tts = _MicChanFakeTTS()
    channel = WebSocketUserChannel(
        send_json=lambda frame: _record_frame(sent, frame),
        text_input_queue=text_q,
        ack_clarification_queue=ack_q,
        transport=transport,
        stt=stt,
        tts=tts,
        get_phase=lambda: TaskPhase.EXECUTION_ACTIVE,
    )

    task = asyncio.create_task(channel.receive_text())
    await asyncio.wait_for(stt.started.wait(), timeout=1.0)
    stt.release.set()
    for _ in range(5):
        await asyncio.sleep(0)
        if stt.closed:
            break
    assert stt.closed is True
    done, _pending = await asyncio.wait({task}, timeout=0.05)
    assert task not in done
    await text_q.put(("typed fallback", "en"))

    text, lang = await asyncio.wait_for(task, timeout=1.0)

    assert (text, lang) == ("typed fallback", "en")


@pytest.mark.asyncio
async def test_websocket_user_channel_receive_text_normalizes_audio_locale_codes() -> None:
    sent: list[dict[str, object]] = []
    text_q = _CancelAwareQueue()
    ack_q: asyncio.Queue = asyncio.Queue()
    transport = _WebChanTransport()
    stt = _BlockingWebChanSTT(_final("book a table tonight", lang="en-US"))
    tts = _MicChanFakeTTS()
    channel = WebSocketUserChannel(
        send_json=lambda frame: _record_frame(sent, frame),
        text_input_queue=text_q,
        ack_clarification_queue=ack_q,
        transport=transport,
        stt=stt,
        tts=tts,
        get_phase=lambda: TaskPhase.EXECUTION_ACTIVE,
    )

    task = asyncio.create_task(channel.receive_text())
    await asyncio.wait_for(stt.started.wait(), timeout=1.0)
    stt.release.set()

    text, lang = await asyncio.wait_for(task, timeout=1.0)

    assert (text, lang) == ("book a table tonight", "en")
    assert text_q.cancelled_get is True


@pytest.mark.asyncio
async def test_websocket_user_channel_receive_text_tie_prefers_typed_text() -> None:
    sent: list[dict[str, object]] = []
    text_q = asyncio.Queue()
    ack_q: asyncio.Queue = asyncio.Queue()
    transport = _WebChanTransport()
    stt = _BlockingWebChanSTT(_final("audio tie", lang="zh"))
    tts = _MicChanFakeTTS()
    channel = WebSocketUserChannel(
        send_json=lambda frame: _record_frame(sent, frame),
        text_input_queue=text_q,
        ack_clarification_queue=ack_q,
        transport=transport,
        stt=stt,
        tts=tts,
        get_phase=lambda: TaskPhase.EXECUTION_ACTIVE,
    )

    task = asyncio.create_task(channel.receive_text())
    await asyncio.wait_for(stt.started.wait(), timeout=1.0)
    await text_q.put(("typed tie", "en"))
    stt.release.set()

    text, lang = await asyncio.wait_for(task, timeout=1.0)

    assert (text, lang) == ("typed tie", "en")


@pytest.mark.asyncio
async def test_websocket_user_channel_receive_text_audio_eof_waits_for_text() -> None:
    sent: list[dict[str, object]] = []
    text_q = asyncio.Queue()
    ack_q: asyncio.Queue = asyncio.Queue()
    transport = _WebChanTransport()
    stt = _MicChanFakeSTT([])
    tts = _MicChanFakeTTS()
    channel = WebSocketUserChannel(
        send_json=lambda frame: _record_frame(sent, frame),
        text_input_queue=text_q,
        ack_clarification_queue=ack_q,
        transport=transport,
        stt=stt,
        tts=tts,
        get_phase=lambda: TaskPhase.EXECUTION_ACTIVE,
    )

    task = asyncio.create_task(channel.receive_text())
    await asyncio.sleep(0)
    await text_q.put(("typed after eof", "en"))

    text, lang = await asyncio.wait_for(task, timeout=1.0)

    assert (text, lang) == ("typed after eof", "en")


@pytest.mark.asyncio
async def test_websocket_user_channel_passes_transport_to_audio_stt() -> None:
    sent: list[dict[str, object]] = []
    text_q: asyncio.Queue = asyncio.Queue()
    ack_q: asyncio.Queue = asyncio.Queue()
    transport = _WebChanTransport()
    stt = _TransportAwareWebChanSTT(_final("你好", lang="zh"))
    tts = _MicChanFakeTTS()
    channel = WebSocketUserChannel(
        send_json=lambda frame: _record_frame(sent, frame),
        text_input_queue=text_q,
        ack_clarification_queue=ack_q,
        transport=transport,
        stt=stt,
        tts=tts,
        get_phase=lambda: TaskPhase.EXECUTION_ACTIVE,
    )

    task = asyncio.create_task(channel.receive_text())
    await asyncio.wait_for(stt.started.wait(), timeout=1.0)

    text, lang = await asyncio.wait_for(task, timeout=1.0)

    assert (text, lang) == ("你好", "zh")
    assert stt.transport is transport


@pytest.mark.asyncio
async def test_speak_text_in_collecting_skips_audio() -> None:
    sent: list[dict] = []

    async def send_json(frame):
        sent.append(frame)

    transport_calls: list[str] = []

    class _RecordingTransport:
        def set_outbound_role(self, role): transport_calls.append(f"set_role:{role}")
        async def output_stream(self, audio): transport_calls.append("output_stream")

    class _DummyTTS:
        def stream_synthesize(self, chunks): return chunks

    channel = WebSocketUserChannel(
        send_json=send_json,
        text_input_queue=asyncio.Queue(),
        ack_clarification_queue=asyncio.Queue(),
        transport=_RecordingTransport(),
        stt=object(),
        tts=_DummyTTS(),
        get_phase=lambda: TaskPhase.COLLECTING,
    )
    await channel.speak_text("hi", lang="en")
    assert len(sent) == 1
    f = sent[0]
    assert f["type"] == "transcript_update"
    assert f["role"] == "ai_to_user"
    assert f["text"] == "hi"
    assert f["lang"] == "en"
    assert f["is_final"] is True
    assert f["subtype"] == "original"
    assert f["parent_id"] is None
    assert f["segment_id"] is None
    assert f["id"]
    assert "T" in f["created_at"]
    assert transport_calls == []


@pytest.mark.asyncio
async def test_websocket_user_channel_speak_text_emits_transcript_and_audio() -> None:
    sent: list[dict[str, object]] = []
    text_q: asyncio.Queue = asyncio.Queue()
    ack_q: asyncio.Queue = asyncio.Queue()
    transport = _WebChanTransport()
    stt = _MicChanFakeSTT([])
    tts = _MicChanFakeTTS()
    channel = WebSocketUserChannel(
        send_json=lambda frame: _record_frame(sent, frame),
        text_input_queue=text_q,
        ack_clarification_queue=ack_q,
        transport=transport,
        stt=stt,
        tts=tts,
        get_phase=lambda: TaskPhase.EXECUTION_ACTIVE,
    )

    await channel.speak_text("好的，我来确认信息。", lang="zh")

    assert len(sent) == 1
    _assert_ai_transcript_frame(sent[0], text="好的，我来确认信息。", lang="zh")
    assert transport.outbound_role == "ai_to_user"
    assert transport.recorded_audio == [b"AUDIO:" + "好的，我来确认信息。".encode("utf-8")]
    assert tts.synthesized == [("好的，我来确认信息。", "zh")]


async def _record_frame(
    sent: list[dict[str, object]],
    frame: dict[str, object],
) -> None:
    sent.append(frame)
