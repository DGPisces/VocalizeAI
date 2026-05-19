"""Energy-based merchant audio interruption detection."""
from __future__ import annotations

import math
import struct
from collections import deque
from typing import Deque

_INTERRUPT_DURATION_MS = 600
_DELTA_DB = 6.0
_FRAME_MS = 30


def _pcm_int16_to_dbfs(pcm: bytes) -> float:
    """Compute RMS dBFS for little-endian signed int16 PCM."""
    if not pcm:
        return float("-inf")
    sample_count = len(pcm) // 2
    if sample_count == 0:
        return float("-inf")
    samples = struct.unpack(f"<{sample_count}h", pcm[: sample_count * 2])
    sum_sq = sum(sample * sample for sample in samples)
    if sum_sq == 0:
        return float("-inf")
    rms = math.sqrt(sum_sq / sample_count)
    return 20.0 * math.log10(rms / 32768.0)


def detect_interruption(
    pcm: bytes,
    *,
    ambient_floor_db: float,
    sr: int = 16000,
    delta_db: float = _DELTA_DB,
    duration_ms: int = _INTERRUPT_DURATION_MS,
) -> bool:
    """Return True when merchant audio stays above ambient + delta long enough."""
    if duration_ms <= 0:
        raise ValueError("duration_ms must be positive")

    threshold = ambient_floor_db + delta_db
    samples_per_frame = (sr * _FRAME_MS) // 1000
    if samples_per_frame <= 0:
        raise ValueError("sr must produce at least one sample per frame")

    bytes_per_frame = samples_per_frame * 2
    frames_needed = math.ceil(duration_ms / _FRAME_MS)

    streak = 0
    for offset in range(0, len(pcm) - bytes_per_frame + 1, bytes_per_frame):
        frame = pcm[offset : offset + bytes_per_frame]
        if _pcm_int16_to_dbfs(frame) >= threshold:
            streak += 1
            if streak >= frames_needed:
                return True
        else:
            streak = 0
    return False


class AmbientFloorEstimator:
    """Sliding-window ambient-floor estimator for merchant audio."""

    _FRAME_FLOOR_DB = -65.0

    def __init__(self, *, window_ms: int = 2000, sr: int = 16000) -> None:
        if window_ms <= 0:
            raise ValueError("window_ms must be positive")
        samples_per_frame = (sr * _FRAME_MS) // 1000
        if samples_per_frame <= 0:
            raise ValueError("sr must produce at least one sample per frame")

        self._frame_bytes = samples_per_frame * 2
        self._frames: Deque[float] = deque(maxlen=math.ceil(window_ms / _FRAME_MS))
        self._pending = b""

    def feed(self, pcm: bytes) -> None:
        data = self._pending + pcm
        complete_bytes = len(data) - (len(data) % self._frame_bytes)
        self._pending = data[complete_bytes:]

        for offset in range(0, complete_bytes, self._frame_bytes):
            frame = data[offset : offset + self._frame_bytes]
            dbfs = _pcm_int16_to_dbfs(frame)
            if dbfs == float("-inf"):
                dbfs = self._FRAME_FLOOR_DB
            self._frames.append(dbfs)

    @property
    def current_floor_db(self) -> float:
        if not self._frames:
            return self._FRAME_FLOOR_DB
        ordered = sorted(self._frames)
        return ordered[len(ordered) // 2]


__all__ = ["AmbientFloorEstimator", "detect_interruption"]
