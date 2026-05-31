"""Speech provider process lifecycle tests."""
from __future__ import annotations

from typing import Any

import pytest

from vocalize.config import Config
from vocalize.provider_runtime import ensure_speech_provider_started


def test_provider_runtime_noops_when_auto_start_disabled() -> None:
    cfg = Config(speech_provider_auto_start=False)

    assert ensure_speech_provider_started(cfg) is None


def test_provider_runtime_requires_command_when_enabled() -> None:
    cfg = Config(speech_provider_auto_start=True, speech_provider_command=None)

    with pytest.raises(RuntimeError, match="VOCALIZE_SPEECH_PROVIDER_COMMAND"):
        ensure_speech_provider_started(cfg)


def test_provider_runtime_starts_command_with_provider_port(monkeypatch) -> None:
    ready_calls = iter([False, True])
    popen_calls: list[dict[str, Any]] = []

    class _Process:
        returncode = None

        def poll(self) -> int | None:
            return None

        def terminate(self) -> None:
            pass

        def wait(self, timeout: float | None = None) -> int:
            return 0

        def kill(self) -> None:
            pass

    def fake_ready(url: str, *, timeout_s: float) -> bool:
        assert url == "http://127.0.0.1:8766/v1/capabilities"
        return next(ready_calls)

    def fake_popen(args, **kwargs):
        popen_calls.append({"args": args, "env": kwargs["env"]})
        return _Process()

    monkeypatch.setattr("vocalize.provider_runtime._capabilities_ready", fake_ready)
    monkeypatch.setattr("subprocess.Popen", fake_popen)

    cfg = Config(
        stt_provider_url="http://127.0.0.1:8766",
        speech_provider_auto_start=True,
        speech_provider_command="/tmp/VocalizeSpeechProvider --flag",
    )

    process = ensure_speech_provider_started(cfg)

    assert process is not None
    assert process.capabilities_url == "http://127.0.0.1:8766/v1/capabilities"
    assert popen_calls[0]["args"] == ["/tmp/VocalizeSpeechProvider", "--flag"]
    assert popen_calls[0]["env"]["VOCALIZE_SPEECH_PROVIDER_PORT"] == "8766"


def test_provider_runtime_preserves_existing_command_path_with_spaces(
    monkeypatch,
    tmp_path,
) -> None:
    ready_calls = iter([False, True])
    command = tmp_path / "Folder With Spaces" / "VocalizeSpeechProvider"
    command.parent.mkdir()
    command.write_text("#!/bin/sh\n")
    popen_args: list[list[str]] = []

    class _Process:
        returncode = None

        def poll(self) -> int | None:
            return None

        def terminate(self) -> None:
            pass

        def wait(self, timeout: float | None = None) -> int:
            return 0

        def kill(self) -> None:
            pass

    monkeypatch.setattr(
        "vocalize.provider_runtime._capabilities_ready",
        lambda *_args, **_kwargs: next(ready_calls),
    )
    monkeypatch.setattr(
        "subprocess.Popen",
        lambda args, **_kwargs: popen_args.append(args) or _Process(),
    )

    process = ensure_speech_provider_started(
        Config(
            stt_provider_url="http://127.0.0.1:8766",
            speech_provider_auto_start=True,
            speech_provider_command=str(command),
        )
    )

    assert process is not None
    assert popen_args == [[str(command)]]
