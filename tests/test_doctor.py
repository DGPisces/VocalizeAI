"""Doctor checks."""
from __future__ import annotations

import json

from vocalize.config import Config
from vocalize.doctor import run_doctor


class _Response:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None

    def read(self) -> bytes:
        return json.dumps(
            {
                "providerApiVersion": "1.0",
                "permissions": {
                    "speech_recognition": "authorized",
                    "microphone": "authorized",
                    "tts_voices_available": 5,
                },
            }
        ).encode("utf-8")


def test_doctor_passes_for_macos_llm_and_ready_provider(monkeypatch) -> None:
    opened: list[str] = []

    def fake_urlopen(url: str, timeout: float):
        opened.append(url)
        return _Response()

    monkeypatch.setattr("platform.system", lambda: "Darwin")
    monkeypatch.setattr("platform.platform", lambda: "macOS-26.5-arm64")
    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    checks = run_doctor(
        Config(
            openai_api_key="sk-test",
            openai_model="test-model",
            stt_provider_url="http://127.0.0.1:8766",
        ),
        skip_llm_probe=True,
    )

    assert all(check.ok for check in checks)
    assert opened == ["http://127.0.0.1:8766/v1/capabilities"]
    assert {check.name for check in checks} >= {"install_layout", "llm_probe"}
    assert {check.name: check.detail for check in checks}["llm_probe"] == (
        "skipped by --skip-llm-probe"
    )


def test_doctor_fails_for_missing_llm_and_speech_permission(monkeypatch) -> None:
    class _DeniedResponse(_Response):
        def read(self) -> bytes:
            return json.dumps(
                {
                    "permissions": {
                        "speech_recognition": "denied",
                        "microphone": "denied",
                        "tts_voices_available": 0,
                    }
                }
            ).encode("utf-8")

    monkeypatch.setattr("platform.system", lambda: "Darwin")
    monkeypatch.setattr("platform.platform", lambda: "macOS-26.5-arm64")
    monkeypatch.setattr("urllib.request.urlopen", lambda *_args, **_kwargs: _DeniedResponse())

    checks = run_doctor(Config(openai_api_key=None))
    by_name = {check.name: check for check in checks}

    assert by_name["llm_config"].ok is False
    assert by_name["speech_provider"].ok is False
    assert "speech permission is denied" in by_name["speech_provider"].detail
    assert "microphone permission is denied" in by_name["speech_provider"].detail


def test_doctor_requests_not_determined_speech_permissions(monkeypatch) -> None:
    calls: list[str] = []

    class _NotDeterminedResponse(_Response):
        def read(self) -> bytes:
            return json.dumps(
                {
                    "permissions": {
                        "speech_recognition": "not_determined",
                        "microphone": "not_determined",
                        "tts_voices_available": 5,
                    }
                }
            ).encode("utf-8")

    def fake_urlopen(request, timeout: float):
        url = getattr(request, "full_url", request)
        calls.append(str(url))
        if str(url).endswith("/v1/permissions/request"):
            return _Response()
        return _NotDeterminedResponse() if len(calls) == 1 else _Response()

    monkeypatch.setattr("platform.system", lambda: "Darwin")
    monkeypatch.setattr("platform.platform", lambda: "macOS-26.5-arm64")
    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    checks = run_doctor(
        Config(
            openai_api_key="sk-test",
            openai_model="test-model",
            stt_provider_url="http://127.0.0.1:8766",
        ),
        skip_llm_probe=True,
    )

    assert all(check.ok for check in checks)
    assert calls == [
        "http://127.0.0.1:8766/v1/capabilities",
        "http://127.0.0.1:8766/v1/permissions/request",
        "http://127.0.0.1:8766/v1/capabilities",
    ]


def test_doctor_probe_thinking_extra_body_modes() -> None:
    from vocalize.llm.openai_compat import _thinking_extra_body

    assert _thinking_extra_body("disabled") == {"thinking": {"type": "disabled"}}
    assert _thinking_extra_body("enabled") is None


def test_doctor_validates_packaged_install_layout(monkeypatch, tmp_path) -> None:
    root = tmp_path / "VocalizeAI"
    for path in [
        root / "bin",
        root / "app",
        root / "config",
        root / "logs",
        root / "cache",
    ]:
        path.mkdir(parents=True)
    (root / ".vocalize-install-root").write_text("marker\n", encoding="utf-8")
    (root / "vocalize").write_text("#!/bin/sh\n", encoding="utf-8")
    (root / "VERSION").write_text("0.1.0\n", encoding="utf-8")
    monkeypatch.setenv("VOCALIZE_INSTALL_ROOT", str(root))
    monkeypatch.setattr("platform.system", lambda: "Darwin")
    monkeypatch.setattr("urllib.request.urlopen", lambda *_args, **_kwargs: _Response())

    checks = run_doctor(Config(openai_api_key=None))
    by_name = {check.name: check for check in checks}

    assert by_name["install_layout"].ok is True


def test_doctor_reports_missing_install_layout_files(monkeypatch, tmp_path) -> None:
    root = tmp_path / "VocalizeAI"
    root.mkdir()
    monkeypatch.setenv("VOCALIZE_INSTALL_ROOT", str(root))
    monkeypatch.setattr("platform.system", lambda: "Darwin")
    monkeypatch.setattr("urllib.request.urlopen", lambda *_args, **_kwargs: _Response())

    checks = run_doctor(Config(openai_api_key=None))
    by_name = {check.name: check for check in checks}

    assert by_name["install_layout"].ok is False
    assert ".vocalize-install-root" in by_name["install_layout"].detail
