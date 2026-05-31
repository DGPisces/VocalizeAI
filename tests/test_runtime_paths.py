"""Packaged runtime resource path tests."""
from __future__ import annotations

import sys

from vocalize.config import Config
from vocalize.runtime_paths import (
    bundled_config_template,
    bundled_frontend_dist,
    bundled_resource_root,
    bundled_speech_provider,
)
from vocalize.server import _frontend_dist_dir


def test_bundled_runtime_paths_use_pyinstaller_meipass(monkeypatch, tmp_path) -> None:
    runtime_root = tmp_path / "vocalize_runtime"
    frontend = runtime_root / "frontend"
    config = runtime_root / "config"
    provider = runtime_root / "bin" / "vocalize-mac-speech-provider"
    frontend.mkdir(parents=True)
    config.mkdir()
    provider.parent.mkdir()
    (frontend / "index.html").write_text("<main>VocalizeAI</main>", encoding="utf-8")
    (config / ".env.example").write_text("OPENAI_API_KEY=\n", encoding="utf-8")
    provider.write_text("#!/bin/sh\n", encoding="utf-8")

    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)

    assert bundled_resource_root() == runtime_root
    assert bundled_frontend_dist() == frontend
    assert bundled_config_template() == config / ".env.example"
    assert bundled_speech_provider() == provider


def test_server_frontend_dist_prefers_bundled_dist_when_env_absent(
    monkeypatch,
    tmp_path,
) -> None:
    frontend = tmp_path / "vocalize_runtime" / "frontend"
    frontend.mkdir(parents=True)
    (frontend / "index.html").write_text("<main>VocalizeAI</main>", encoding="utf-8")

    monkeypatch.delenv("VOCALIZE_FRONTEND_DIST", raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)

    assert _frontend_dist_dir() == frontend


def test_config_auto_starts_bundled_speech_provider(monkeypatch, tmp_path) -> None:
    provider = tmp_path / "vocalize_runtime" / "bin" / "vocalize-mac-speech-provider"
    provider.parent.mkdir(parents=True)
    provider.write_text("#!/bin/sh\n", encoding="utf-8")

    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
    monkeypatch.setattr("vocalize.config.load_dotenv", lambda *args, **kwargs: None)
    monkeypatch.delenv("VOCALIZE_SPEECH_PROVIDER_AUTO_START", raising=False)
    monkeypatch.delenv("VOCALIZE_SPEECH_PROVIDER_COMMAND", raising=False)

    cfg = Config.from_env()

    assert cfg.speech_provider_auto_start is True
    assert cfg.speech_provider_command == str(provider)


def test_config_env_can_disable_bundled_speech_provider(monkeypatch, tmp_path) -> None:
    provider = tmp_path / "vocalize_runtime" / "bin" / "vocalize-mac-speech-provider"
    provider.parent.mkdir(parents=True)
    provider.write_text("#!/bin/sh\n", encoding="utf-8")

    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
    monkeypatch.setattr("vocalize.config.load_dotenv", lambda *args, **kwargs: None)
    monkeypatch.setenv("VOCALIZE_SPEECH_PROVIDER_AUTO_START", "0")
    monkeypatch.delenv("VOCALIZE_SPEECH_PROVIDER_COMMAND", raising=False)

    cfg = Config.from_env()

    assert cfg.speech_provider_auto_start is False
    assert cfg.speech_provider_command == str(provider)
