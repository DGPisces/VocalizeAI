from __future__ import annotations

import struct

import pytest

from vocalize.dialogue.merchant_vad import AmbientFloorEstimator, detect_interruption


def _make_pcm_silence(duration_ms: int, sr: int = 16000) -> bytes:
    n = (sr * duration_ms) // 1000
    return struct.pack(f"<{n}h", *([0] * n))


def _make_pcm_loud(duration_ms: int, sr: int = 16000, amp: int = 10000) -> bytes:
    n = (sr * duration_ms) // 1000
    return struct.pack(f"<{n}h", *((amp if i % 2 else -amp) for i in range(n)))


def test_detect_interruption_returns_false_for_silence() -> None:
    assert detect_interruption(_make_pcm_silence(700), ambient_floor_db=-50.0) is False


def test_detect_interruption_returns_true_when_loud_for_600ms() -> None:
    assert detect_interruption(_make_pcm_loud(700), ambient_floor_db=-50.0) is True


def test_detect_interruption_returns_false_when_loud_under_600ms() -> None:
    assert detect_interruption(_make_pcm_loud(400), ambient_floor_db=-50.0) is False


def test_detect_interruption_uses_ceiling_for_non_multiple_duration() -> None:
    assert (
        detect_interruption(
            _make_pcm_loud(600),
            ambient_floor_db=-50.0,
            duration_ms=601,
        )
        is False
    )


def test_detect_interruption_rejects_invalid_sample_rate() -> None:
    with pytest.raises(ValueError, match="sr must produce"):
        detect_interruption(_make_pcm_loud(30, sr=1), ambient_floor_db=-50.0, sr=1)


def test_detect_interruption_rejects_non_positive_duration() -> None:
    with pytest.raises(ValueError, match="duration_ms must be positive"):
        detect_interruption(_make_pcm_loud(30), ambient_floor_db=-50.0, duration_ms=0)


def test_ambient_floor_estimator_initial_floor_uses_silent_window() -> None:
    est = AmbientFloorEstimator(window_ms=2000)
    silence_2s = _make_pcm_silence(2000)

    est.feed(silence_2s)

    assert est.current_floor_db <= -50.0


def test_ambient_floor_estimator_tracks_low_steady_noise() -> None:
    est = AmbientFloorEstimator(window_ms=2000)
    low_noise_2s = _make_pcm_loud(2000, amp=300)

    est.feed(low_noise_2s)

    assert -45.0 <= est.current_floor_db <= -35.0


def test_ambient_floor_estimator_preserves_partial_frames_across_feeds() -> None:
    one_frame = _make_pcm_loud(30, amp=300)
    split_at = len(one_frame) // 2
    est = AmbientFloorEstimator(window_ms=2000)

    est.feed(one_frame[:split_at])
    assert est.current_floor_db <= -50.0

    est.feed(one_frame[split_at:])
    assert -45.0 <= est.current_floor_db <= -35.0


def test_ambient_floor_estimator_window_drops_old_samples() -> None:
    est = AmbientFloorEstimator(window_ms=500)
    est.feed(_make_pcm_loud(500, amp=10000))
    high_floor = est.current_floor_db

    est.feed(_make_pcm_silence(500))

    assert est.current_floor_db < high_floor
