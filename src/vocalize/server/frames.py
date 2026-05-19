"""WebSocket frame schemas for the v1 spec §4.3 protocol.

Two encodings:

- **Control frames** are JSON-encoded text WS messages. Every control frame
  has a ``type`` discriminator field, and ``parse_client_frame`` /
  ``parse_server_frame`` dispatch on it.
- **Audio frames** are raw binary WS messages. Inbound (client→server) is raw
  PCM int16 LE 16 kHz mono. Outbound (server→client) prefixes a single ASCII
  role byte (``b'U'`` = ai-to-user, ``b'M'`` = ai-to-merchant) before the same
  PCM payload at 24 kHz (matching the CosyVoice2 default sample rate).

Server→client frames + binary helpers are added in Tasks 2–3.
"""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Annotated, Literal, Union

from pydantic import BaseModel, Field, TypeAdapter

# ---------------------------------------------------------------------------
# Shared enums
# ---------------------------------------------------------------------------

ConversationLang = Literal["zh", "en"]

TextInputMode = Literal["default", "user_takeover"]

Mode = Literal[
    "preflight",
    "call_listening",
    "call_speaking",
    "user_takeover",
    "ended",
]

# ---------------------------------------------------------------------------
# Client → server frames
# ---------------------------------------------------------------------------


class _ClientFrameBase(BaseModel):
    """Common base for all client→server control frames.

    Sub-classes set ``type`` to a fixed literal so the discriminated-union
    dispatch in ``parse_client_frame`` resolves cleanly.
    """

    model_config = {"extra": "forbid"}


class TextInputFrame(_ClientFrameBase):
    type: Literal["text_input"]
    text: str
    lang_hint: ConversationLang | None = None
    mode: TextInputMode = "default"


class ModeChangeFrame(_ClientFrameBase):
    type: Literal["mode_change"]
    mode: Mode


class AckClarificationFrame(_ClientFrameBase):
    type: Literal["ack_clarification"]
    slot_value: str


class HangupFrame(_ClientFrameBase):
    type: Literal["hangup"]


class SetDevicesFrame(_ClientFrameBase):
    type: Literal["set_devices"]
    input_id: str
    output_id: str
    aec: bool = True


class TriggerCallbackFrame(_ClientFrameBase):
    type: Literal["trigger_callback"]
    callback_id: str


class CancelCallbackFrame(_ClientFrameBase):
    type: Literal["cancel_callback"]
    callback_id: str


class RestoreCallbackFrame(_ClientFrameBase):
    type: Literal["restore_callback"]
    callback_id: str


class ConfirmAssumptionFrame(_ClientFrameBase):
    type: Literal["confirm_assumption"]
    assumption_id: str
    choice: Literal["correct", "wrong"]
    correction: str | None = None
    note: str | None = None


class SetAutoTranslateFrame(_ClientFrameBase):
    type: Literal["set_auto_translate"]
    value: bool


class OnDemandTranslateFrame(_ClientFrameBase):
    """One-shot per-row Layer 5 call (spec §3.5 ``[译]`` button).

    Used when ``auto_translate_merchant=False`` and
    ``user_lang != merchant_lang``. Backend replies with a
    ``transcript_update(subtype=translation, parent_id=transcript_id)``
    carrying the translated text.
    """

    type: Literal["on_demand_translate"]
    transcript_id: str


class MerchantTextInjectFrame(_ClientFrameBase):
    """Test-only merchant text input for deterministic WS-path scenarios."""

    type: Literal["merchant_text_inject"]
    text: str
    scenario_id: str
    seed: str
    lang_hint: ConversationLang | None = None


ClientFrame = Annotated[
    Union[
        TextInputFrame,
        ModeChangeFrame,
        AckClarificationFrame,
        HangupFrame,
        SetDevicesFrame,
        TriggerCallbackFrame,
        CancelCallbackFrame,
        RestoreCallbackFrame,
        ConfirmAssumptionFrame,
        SetAutoTranslateFrame,
        OnDemandTranslateFrame,
        MerchantTextInjectFrame,
    ],
    Field(discriminator="type"),
]

_client_adapter: TypeAdapter[ClientFrame] = TypeAdapter(ClientFrame)


def parse_client_frame(payload: str) -> ClientFrame:
    """Parse a JSON text WS frame into the matching pydantic model.

    Raises:
        json.JSONDecodeError: payload is not valid JSON.
        pydantic.ValidationError: payload is JSON but does not match any
            client→server schema (unknown ``type``, missing fields, wrong
            mode, etc).
    """
    obj = json.loads(payload)
    return _client_adapter.validate_python(obj)


# ---------------------------------------------------------------------------
# Server → client frames
# ---------------------------------------------------------------------------

TranscriptRole = Literal[
    "user",
    "merchant",
    "ai_to_user",
    "ai_to_merchant",
    "merchant_to_ai",
    "user_supplement",
    "user_takeover_passthrough",
    "system",
]

TranscriptSubtype = Literal[
    "original",
    "translation",
    "user_supplement",
    "user_takeover_passthrough",
    "callback_segment",
    "filler",
    "keepalive",
]

AudioOutboundRole = Literal["ai_to_user", "ai_to_merchant"]


class _ServerFrameBase(BaseModel):
    model_config = {"extra": "forbid"}


class AudioChunkOutboundFrame(_ServerFrameBase):
    """Marker — outbound audio rides binary WS frames, not JSON. Calling
    ``serialize_server_frame`` on this class raises TypeError so an accidental
    misroute is caught at the call site.
    """

    type: Literal["audio_chunk"] = "audio_chunk"
    role: AudioOutboundRole


class TranscriptUpdateFrame(_ServerFrameBase):
    type: Literal["transcript_update"] = "transcript_update"
    id: str
    role: TranscriptRole
    text: str
    lang: ConversationLang | None = None
    is_final: bool
    subtype: TranscriptSubtype = "original"
    parent_id: str | None = None
    segment_id: str | None = None
    created_at: datetime


def build_transcript_update(
    *,
    role: TranscriptRole,
    text: str,
    lang: ConversationLang | None,
    is_final: bool,
    subtype: TranscriptSubtype = "original",
    parent_id: str | None = None,
    segment_id: str | None = None,
) -> TranscriptUpdateFrame:
    """Construct a TranscriptUpdateFrame with a fresh uuid id and UTC timestamp."""
    return TranscriptUpdateFrame(
        id=uuid.uuid4().hex,
        role=role,
        text=text,
        lang=lang,
        is_final=is_final,
        subtype=subtype,
        parent_id=parent_id,
        segment_id=segment_id,
        created_at=datetime.now(timezone.utc),
    )


class StateUpdateFrame(_ServerFrameBase):
    type: Literal["state_update"] = "state_update"
    diff: dict


class ReadinessChangeFrame(_ServerFrameBase):
    type: Literal["readiness_change"] = "readiness_change"
    passed: bool
    missing_critical: list[str]
    confidence: float


class ClarificationRequestFrame(_ServerFrameBase):
    type: Literal["clarification_request"] = "clarification_request"
    field: str
    question: str
    lang: ConversationLang
    timeout_s: float


class ModeAckFrame(_ServerFrameBase):
    type: Literal["mode_ack"] = "mode_ack"
    mode: Mode


class ErrorFrame(_ServerFrameBase):
    type: Literal["error"] = "error"
    code: int
    message_zh: str
    message_en: str


class PhaseChangeFrame(_ServerFrameBase):
    type: Literal["phase_change"] = "phase_change"
    previous: str  # TaskPhase.value
    current: str  # TaskPhase.value


class CallSegmentAddedFrame(_ServerFrameBase):
    type: Literal["call_segment_added"] = "call_segment_added"
    segment: dict


class SegmentInterruptedFrame(_ServerFrameBase):
    type: Literal["segment_interrupted"] = "segment_interrupted"
    segment_id: str
    reason: Literal["ws_close", "user_hangup", "merchant_impatience"]


class UncertainAssumptionAddedFrame(_ServerFrameBase):
    type: Literal["uncertain_assumption_added"] = "uncertain_assumption_added"
    assumption: dict


class PendingCallbackAddedFrame(_ServerFrameBase):
    type: Literal["pending_callback_added"] = "pending_callback_added"
    callback: dict


class EscalationWarningFrame(_ServerFrameBase):
    type: Literal["escalation_warning"] = "escalation_warning"
    reason: Literal["merchant_impatience"]
    holds_used: int
    message_zh: str
    message_en: str


_JSON_SERIALIZABLE_SERVER_FRAMES = (
    TranscriptUpdateFrame,
    StateUpdateFrame,
    ReadinessChangeFrame,
    ClarificationRequestFrame,
    ModeAckFrame,
    ErrorFrame,
    PhaseChangeFrame,
    CallSegmentAddedFrame,
    SegmentInterruptedFrame,
    UncertainAssumptionAddedFrame,
    PendingCallbackAddedFrame,
    EscalationWarningFrame,
)


def serialize_server_frame(
    frame: TranscriptUpdateFrame
    | StateUpdateFrame
    | ReadinessChangeFrame
    | ClarificationRequestFrame
    | ModeAckFrame
    | ErrorFrame
    | PhaseChangeFrame
    | CallSegmentAddedFrame
    | SegmentInterruptedFrame
    | UncertainAssumptionAddedFrame
    | PendingCallbackAddedFrame
    | EscalationWarningFrame,
) -> str:
    """Render a server→client control frame as a JSON string.

    Raises:
        TypeError: the input is ``AudioChunkOutboundFrame`` (binary path) or
            anything else not in the JSON-serializable set above. This
            prevents the WS layer from accidentally sending audio markers as
            text frames.
    """
    if not isinstance(frame, _JSON_SERIALIZABLE_SERVER_FRAMES):
        raise TypeError(
            f"frame {type(frame).__name__} is not JSON-serializable; "
            "audio frames must be sent via send_outbound_audio()"
        )
    return frame.model_dump_json()


# ---------------------------------------------------------------------------
# Binary audio frames
# ---------------------------------------------------------------------------


_ROLE_BYTE_USER: bytes = b"U"
_ROLE_BYTE_MERCHANT: bytes = b"M"


@dataclass(frozen=True)
class InboundAudioChunk:
    """Decoded payload of a client→server binary audio WS frame.

    There is no role tag on inbound audio — the only inbound source is the
    user laptop's microphone. Sample format: PCM int16 LE 16 kHz mono.
    """

    pcm: bytes


@dataclass(frozen=True)
class OutboundAudioChunk:
    """Decoded payload of a server→client binary audio WS frame.

    The role tag is stored alongside so callers can route to the correct UI
    visualiser. Sample format: PCM int16 LE 24 kHz mono (CosyVoice2 default).
    """

    role: AudioOutboundRole
    pcm: bytes


def encode_outbound_audio_chunk(role: AudioOutboundRole, pcm: bytes) -> bytes:
    """Prepend the single-byte role tag to ``pcm``.

    Raises:
        ValueError: ``role`` is not one of ``ai_to_user`` / ``ai_to_merchant``.
    """
    if role == "ai_to_user":
        return _ROLE_BYTE_USER + pcm
    if role == "ai_to_merchant":
        return _ROLE_BYTE_MERCHANT + pcm
    raise ValueError(f"unknown outbound audio role: {role!r}")


def decode_inbound_audio_chunk(payload: bytes) -> InboundAudioChunk:
    """Wrap a raw inbound binary frame in an ``InboundAudioChunk``.

    Raises:
        ValueError: payload is empty (an empty WS binary frame is a protocol
            error; the client should send EOF semantics via ``hangup`` text
            frame, not zero-length audio).
    """
    if len(payload) == 0:
        raise ValueError("empty audio_chunk payload")
    return InboundAudioChunk(pcm=payload)


__all__ = [
    "AckClarificationFrame",
    "AudioChunkOutboundFrame",
    "AudioOutboundRole",
    "CallSegmentAddedFrame",
    "ClarificationRequestFrame",
    "ClientFrame",
    "ConfirmAssumptionFrame",
    "ConversationLang",
    "EscalationWarningFrame",
    "ErrorFrame",
    "HangupFrame",
    "InboundAudioChunk",
    "Mode",
    "ModeAckFrame",
    "ModeChangeFrame",
    "OnDemandTranslateFrame",
    "OutboundAudioChunk",
    "PendingCallbackAddedFrame",
    "PhaseChangeFrame",
    "ReadinessChangeFrame",
    "RestoreCallbackFrame",
    "SegmentInterruptedFrame",
    "SetAutoTranslateFrame",
    "SetDevicesFrame",
    "StateUpdateFrame",
    "TextInputFrame",
    "TextInputMode",
    "TranscriptRole",
    "TranscriptSubtype",
    "TranscriptUpdateFrame",
    "TriggerCallbackFrame",
    "UncertainAssumptionAddedFrame",
    "build_transcript_update",
    "decode_inbound_audio_chunk",
    "encode_outbound_audio_chunk",
    "parse_client_frame",
    "serialize_server_frame",
]
