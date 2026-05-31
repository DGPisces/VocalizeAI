"""Shared service error types."""


class VoiceServiceError(RuntimeError):
    """Base class for STT/TTS provider failures.

    VoicePipeline catches this type to end or abandon the current audio path
    without depending on a concrete provider implementation.
    """
