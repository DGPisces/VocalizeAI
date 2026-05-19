"""Frame schema tests — pydantic model validation for spec §4.3 frames.

The wire format is a tagged union: every control frame carries a discriminator
field ``type`` that picks the model. Pydantic 2's ``Field(discriminator=...)``
on the union does the dispatch on parse.

Audio frames are pure binary (no JSON envelope) and are tested in test_server_frames.py
as well, in a separate group below.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from vocalize.server.frames import (
    AckClarificationFrame,
    AudioChunkOutboundFrame,
    CancelCallbackFrame,
    ClarificationRequestFrame,
    ConfirmAssumptionFrame,
    ErrorFrame,
    EscalationWarningFrame,
    HangupFrame,
    InboundAudioChunk,
    MerchantTextInjectFrame,
    ModeAckFrame,
    ModeChangeFrame,
    OnDemandTranslateFrame,
    PendingCallbackAddedFrame,
    PhaseChangeFrame,
    ReadinessChangeFrame,
    RestoreCallbackFrame,
    SetAutoTranslateFrame,
    SetDevicesFrame,
    StateUpdateFrame,
    TextInputFrame,
    TranscriptUpdateFrame,
    TriggerCallbackFrame,
    UncertainAssumptionAddedFrame,
    build_transcript_update,
    decode_inbound_audio_chunk,
    encode_outbound_audio_chunk,
    parse_client_frame,
    serialize_server_frame,
)


def test_text_input_frame_parses() -> None:
    raw = json.dumps({"type": "text_input", "text": "你好", "lang_hint": "zh"})
    frame = parse_client_frame(raw)
    assert isinstance(frame, TextInputFrame)
    assert frame.text == "你好"
    assert frame.lang_hint == "zh"


def test_text_input_lang_hint_optional() -> None:
    raw = json.dumps({"type": "text_input", "text": "hi"})
    frame = parse_client_frame(raw)
    assert isinstance(frame, TextInputFrame)
    assert frame.lang_hint is None


def test_text_input_default_mode_is_default() -> None:
    f = parse_client_frame(json.dumps({"type": "text_input", "text": "hi"}))
    assert isinstance(f, TextInputFrame)
    assert f.mode == "default"


def test_text_input_user_takeover_parses() -> None:
    f = parse_client_frame(
        json.dumps({"type": "text_input", "text": "yes", "mode": "user_takeover"})
    )
    assert f.mode == "user_takeover"


def test_text_input_invalid_mode_rejected() -> None:
    with pytest.raises(ValidationError):
        parse_client_frame(
            json.dumps({"type": "text_input", "text": "x", "mode": "bogus"})
        )


def test_mode_change_frame_validates_enum() -> None:
    raw = json.dumps({"type": "mode_change", "mode": "preflight"})
    frame = parse_client_frame(raw)
    assert isinstance(frame, ModeChangeFrame)
    assert frame.mode == "preflight"


def test_mode_change_rejects_unknown_mode() -> None:
    raw = json.dumps({"type": "mode_change", "mode": "execution_active"})
    with pytest.raises(ValidationError):
        parse_client_frame(raw)


def test_ack_clarification_carries_slot_value() -> None:
    raw = json.dumps({"type": "ack_clarification", "slot_value": "晚上7点"})
    frame = parse_client_frame(raw)
    assert isinstance(frame, AckClarificationFrame)
    assert frame.slot_value == "晚上7点"


def test_hangup_frame_no_payload() -> None:
    raw = json.dumps({"type": "hangup"})
    frame = parse_client_frame(raw)
    assert isinstance(frame, HangupFrame)


def test_set_devices_frame_carries_ids() -> None:
    raw = json.dumps({
        "type": "set_devices",
        "input_id": "default",
        "output_id": "speaker-2",
        "aec": False,
    })
    frame = parse_client_frame(raw)
    assert isinstance(frame, SetDevicesFrame)
    assert frame.input_id == "default"
    assert frame.output_id == "speaker-2"
    assert frame.aec is False


def test_set_devices_frame_defaults_aec_enabled() -> None:
    raw = json.dumps({
        "type": "set_devices",
        "input_id": "default",
        "output_id": "speaker-2",
    })
    frame = parse_client_frame(raw)
    assert isinstance(frame, SetDevicesFrame)
    assert frame.aec is True


def test_trigger_callback_frame_parses() -> None:
    f = parse_client_frame(
        json.dumps({"type": "trigger_callback", "callback_id": "cb-1"})
    )
    assert isinstance(f, TriggerCallbackFrame)
    assert f.callback_id == "cb-1"


def test_cancel_callback_frame_parses() -> None:
    f = parse_client_frame(
        json.dumps({"type": "cancel_callback", "callback_id": "cb-1"})
    )
    assert isinstance(f, CancelCallbackFrame)
    assert f.callback_id == "cb-1"


def test_restore_callback_frame_in_client_union() -> None:
    f = parse_client_frame(
        json.dumps({"type": "restore_callback", "callback_id": "cb-1"})
    )
    assert isinstance(f, RestoreCallbackFrame)
    assert f.callback_id == "cb-1"


def test_confirm_assumption_correct_parses() -> None:
    f = parse_client_frame(
        json.dumps({
            "type": "confirm_assumption",
            "assumption_id": "a-1",
            "choice": "correct",
            "correction": None,
        })
    )
    assert isinstance(f, ConfirmAssumptionFrame)
    assert f.assumption_id == "a-1"
    assert f.choice == "correct"
    assert f.note is None


def test_confirm_assumption_wrong_with_correction_and_note_parses() -> None:
    f = parse_client_frame(
        json.dumps({
            "type": "confirm_assumption",
            "assumption_id": "a-1",
            "choice": "wrong",
            "correction": "6",
            "note": "actually six adults",
        })
    )
    assert f.correction == "6"
    assert f.note == "actually six adults"


def test_confirm_assumption_invalid_choice_rejected() -> None:
    with pytest.raises(ValidationError):
        parse_client_frame(
            json.dumps({
                "type": "confirm_assumption",
                "assumption_id": "a-1",
                "choice": "maybe",
                "correction": None,
            })
        )


def test_confirm_assumption_missing_id_rejected() -> None:
    with pytest.raises(ValidationError):
        parse_client_frame(
            json.dumps({
                "type": "confirm_assumption",
                "choice": "correct",
                "correction": None,
            })
        )


def test_set_auto_translate_parses() -> None:
    f = parse_client_frame(json.dumps({"type": "set_auto_translate", "value": False}))
    assert isinstance(f, SetAutoTranslateFrame)
    assert f.value is False


def test_on_demand_translate_parses() -> None:
    f = parse_client_frame(json.dumps(
        {"type": "on_demand_translate", "transcript_id": "t-42"}
    ))
    assert isinstance(f, OnDemandTranslateFrame)
    assert f.transcript_id == "t-42"


def test_merchant_text_inject_parses() -> None:
    f = parse_client_frame(json.dumps({
        "type": "merchant_text_inject",
        "text": "您好，请问几位？",
        "scenario_id": "handover-readiness",
        "seed": "merchant-direct",
        "lang_hint": "zh",
    }))
    assert isinstance(f, MerchantTextInjectFrame)
    assert f.text == "您好，请问几位？"
    assert f.scenario_id == "handover-readiness"
    assert f.seed == "merchant-direct"
    assert f.lang_hint == "zh"


def test_merchant_text_inject_requires_text() -> None:
    with pytest.raises(ValidationError):
        parse_client_frame(json.dumps({
            "type": "merchant_text_inject",
            "scenario_id": "handover-readiness",
            "seed": "merchant-direct",
            "lang_hint": "zh",
        }))


def test_merchant_text_inject_rejects_invalid_lang_hint() -> None:
    with pytest.raises(ValidationError):
        parse_client_frame(json.dumps({
            "type": "merchant_text_inject",
            "text": "Hello",
            "scenario_id": "handover-readiness",
            "seed": "merchant-direct",
            "lang_hint": "fr",
        }))


def test_merchant_text_inject_rejects_extra_fields() -> None:
    with pytest.raises(ValidationError):
        parse_client_frame(json.dumps({
            "type": "merchant_text_inject",
            "text": "Hello",
            "scenario_id": "handover-readiness",
            "seed": "merchant-direct",
            "extra": True,
        }))


def test_merchant_text_inject_requires_evidence_ids() -> None:
    with pytest.raises(ValidationError):
        parse_client_frame(json.dumps({
            "type": "merchant_text_inject",
            "text": "Hello",
        }))


def test_audio_chunk_in_text_frame_is_protocol_error() -> None:
    """The actual ``audio_chunk`` payload is binary, not JSON.
    ``parse_client_frame`` MUST refuse a JSON envelope claiming
    ``type=audio_chunk`` because the discriminated union excludes it.
    """
    raw = json.dumps({"type": "audio_chunk"})
    with pytest.raises(ValidationError):
        parse_client_frame(raw)


def test_unknown_type_raises() -> None:
    raw = json.dumps({"type": "magic", "foo": "bar"})
    with pytest.raises(ValidationError):
        parse_client_frame(raw)


def test_malformed_json_raises_value_error() -> None:
    with pytest.raises(json.JSONDecodeError):
        parse_client_frame("{not json")


# -- Task 2: Server→client frame tests ---------------------------------------


def test_transcript_update_serializes() -> None:
    frame = TranscriptUpdateFrame(
        id="t-0",
        role="user",
        text="你好",
        lang="zh",
        is_final=True,
        created_at=datetime(2026, 5, 7, 12, 0, tzinfo=timezone.utc),
    )
    raw = serialize_server_frame(frame)
    parsed = json.loads(raw)
    assert parsed == {
        "type": "transcript_update",
        "id": "t-0",
        "role": "user",
        "text": "你好",
        "lang": "zh",
        "is_final": True,
        "subtype": "original",
        "parent_id": None,
        "segment_id": None,
        "created_at": parsed["created_at"],
    }
    assert parsed["created_at"].startswith("2026-05-07")


def test_transcript_update_minimal_serialises() -> None:
    f = TranscriptUpdateFrame(
        id="t-1",
        role="ai_to_user",
        text="hi",
        lang="en",
        is_final=True,
        created_at=datetime(2026, 5, 7, 12, 0, tzinfo=timezone.utc),
    )
    payload = json.loads(serialize_server_frame(f))
    assert payload["id"] == "t-1"
    assert payload["subtype"] == "original"
    assert payload["parent_id"] is None
    assert payload["segment_id"] is None
    assert payload["role"] == "ai_to_user"
    assert payload["created_at"].startswith("2026-05-07")


def test_transcript_update_translation_carries_parent() -> None:
    f = TranscriptUpdateFrame(
        id="t-2",
        role="ai_to_merchant",
        text="hello",
        lang="en",
        is_final=True,
        subtype="translation",
        parent_id="t-1",
        created_at=datetime.now(timezone.utc),
    )
    payload = json.loads(serialize_server_frame(f))
    assert payload["subtype"] == "translation"
    assert payload["parent_id"] == "t-1"


def test_build_transcript_update_assigns_id_and_created_at() -> None:
    f = build_transcript_update(role="ai_to_user", text="hi", lang="en", is_final=True)
    assert f.id
    assert f.role == "ai_to_user"
    assert f.text == "hi"
    assert f.subtype == "original"
    assert f.created_at is not None


def test_build_transcript_update_translation_links_parent() -> None:
    f = build_transcript_update(
        role="ai_to_merchant",
        text="hello",
        lang="en",
        is_final=True,
        subtype="translation",
        parent_id="t-orig",
    )
    assert f.parent_id == "t-orig"
    assert f.subtype == "translation"


def test_transcript_update_user_supplement_role_allowed() -> None:
    f = TranscriptUpdateFrame(
        id="t-3",
        role="user_supplement",
        text="actually 6 people",
        lang="zh",
        is_final=True,
        created_at=datetime.now(timezone.utc),
    )
    assert f.role == "user_supplement"


def test_state_update_carries_diff() -> None:
    frame = StateUpdateFrame(diff={"phase": "collecting", "slots": {"date": "2026-05-06"}})
    raw = serialize_server_frame(frame)
    parsed = json.loads(raw)
    assert parsed["type"] == "state_update"
    assert parsed["diff"] == {"phase": "collecting", "slots": {"date": "2026-05-06"}}


def test_readiness_change_fields() -> None:
    frame = ReadinessChangeFrame(
        passed=False,
        missing_critical=["party_size"],
        confidence=0.42,
    )
    parsed = json.loads(serialize_server_frame(frame))
    assert parsed == {
        "type": "readiness_change",
        "passed": False,
        "missing_critical": ["party_size"],
        "confidence": 0.42,
    }


def test_clarification_request_fields() -> None:
    frame = ClarificationRequestFrame(
        field="party_size",
        question="一共几位客人？",
        lang="zh",
        timeout_s=30.0,
    )
    parsed = json.loads(serialize_server_frame(frame))
    assert parsed == {
        "type": "clarification_request",
        "field": "party_size",
        "question": "一共几位客人？",
        "lang": "zh",
        "timeout_s": 30.0,
    }


def test_mode_ack_fields() -> None:
    frame = ModeAckFrame(mode="call_listening")
    parsed = json.loads(serialize_server_frame(frame))
    assert parsed == {"type": "mode_ack", "mode": "call_listening"}


def test_error_frame_fields() -> None:
    frame = ErrorFrame(
        code=2001,
        message_zh="服务器内部错误",
        message_en="Internal server error",
    )
    parsed = json.loads(serialize_server_frame(frame))
    assert parsed == {
        "type": "error",
        "code": 2001,
        "message_zh": "服务器内部错误",
        "message_en": "Internal server error",
    }


def test_phase_change_frame_serialises() -> None:
    f = PhaseChangeFrame(previous="execution_active", current="post_call_review")
    payload = json.loads(serialize_server_frame(f))
    assert payload == {
        "type": "phase_change",
        "previous": "execution_active",
        "current": "post_call_review",
    }


def test_uncertain_assumption_added_carries_full_payload() -> None:
    f = UncertainAssumptionAddedFrame(
        assumption={
            "id": "a-1",
            "slot": "party_size",
            "question": "how many?",
            "assumed_value": 4,
            "source": "user_timeout",
            "created_at": "2026-05-07T12:00:00+00:00",
            "status": "pending_review",
            "correction": None,
            "note": None,
            "callback_id": None,
        }
    )
    payload = json.loads(serialize_server_frame(f))
    assert payload["type"] == "uncertain_assumption_added"
    assert payload["assumption"]["slot"] == "party_size"


def test_pending_callback_added_serialises() -> None:
    f = PendingCallbackAddedFrame(
        callback={
            "id": "cb-1",
            "assumption_id": "a-1",
            "correction": "6",
            "note": None,
            "status": "queued",
            "created_at": "2026-05-07T12:00:00+00:00",
            "started_at": None,
            "completed_at": None,
            "transcript_segment_id": None,
        }
    )
    payload = json.loads(serialize_server_frame(f))
    assert payload["type"] == "pending_callback_added"
    assert payload["callback"]["correction"] == "6"


def test_escalation_warning_serialises() -> None:
    f = EscalationWarningFrame(
        reason="merchant_impatience",
        holds_used=2,
        message_zh="商家催了三次，先挂电话",
        message_en="Merchant interrupted 3 times; ending call",
    )
    payload = json.loads(serialize_server_frame(f))
    assert payload["type"] == "escalation_warning"
    assert payload["reason"] == "merchant_impatience"
    assert payload["holds_used"] == 2


def test_audio_chunk_outbound_marker_frame_does_not_serialize_json() -> None:
    """The outbound binary audio frame must NOT be sent through
    ``serialize_server_frame``. The marker exists for typing only; passing
    it should raise.
    """
    frame = AudioChunkOutboundFrame(role="ai_to_user")
    with pytest.raises(TypeError):
        serialize_server_frame(frame)


def test_transcript_update_role_validates_enum() -> None:
    """Roles in transcript_update are restricted to ``user`` / ``merchant`` /
    ``ai_to_user`` / ``ai_to_merchant`` (the AI side carries an addressee
    marker so the frontend can lay out the bilingual two-column transcript).
    """
    with pytest.raises(ValidationError):
        TranscriptUpdateFrame(
            id="t-invalid",
            role="other",  # type: ignore[arg-type]
            text="x",
            lang="zh",
            is_final=True,
            created_at=datetime.now(timezone.utc),
        )


# -- Task 3: Binary audio frame tests ----------------------------------------


def test_encode_outbound_audio_user_role() -> None:
    payload = b"\x01\x02\x03\x04"
    raw = encode_outbound_audio_chunk(role="ai_to_user", pcm=payload)
    assert raw[:1] == b"U"
    assert raw[1:] == payload


def test_encode_outbound_audio_merchant_role() -> None:
    payload = b"\xaa\xbb"
    raw = encode_outbound_audio_chunk(role="ai_to_merchant", pcm=payload)
    assert raw[:1] == b"M"
    assert raw[1:] == payload


def test_encode_outbound_audio_rejects_unknown_role() -> None:
    with pytest.raises(ValueError):
        encode_outbound_audio_chunk(role="ai_to_other", pcm=b"")  # type: ignore[arg-type]


def test_decode_inbound_audio_chunk_returns_pcm() -> None:
    chunk = decode_inbound_audio_chunk(b"\x10\x20\x30\x40")
    assert isinstance(chunk, InboundAudioChunk)
    assert chunk.pcm == b"\x10\x20\x30\x40"


def test_decode_inbound_audio_rejects_empty() -> None:
    with pytest.raises(ValueError):
        decode_inbound_audio_chunk(b"")
