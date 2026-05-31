"""Runtime resource discovery for source and packaged builds."""
from __future__ import annotations

import sys
from pathlib import Path


RUNTIME_RESOURCE_DIRNAME = "vocalize_runtime"
FRONTEND_DIRNAME = "frontend"
CONFIG_DIRNAME = "config"
BIN_DIRNAME = "bin"
MACOS_SPEECH_PROVIDER_NAME = "vocalize-mac-speech-provider"


def bundled_resource_root() -> Path | None:
    """Return the PyInstaller runtime resource root when present."""
    bundle_root = getattr(sys, "_MEIPASS", None)
    if not bundle_root:
        return None
    candidate = Path(bundle_root) / RUNTIME_RESOURCE_DIRNAME
    if candidate.is_dir():
        return candidate
    return None


def bundled_frontend_dist() -> Path | None:
    """Return the bundled Vite ``dist`` directory when available."""
    root = bundled_resource_root()
    if root is None:
        return None
    candidate = root / FRONTEND_DIRNAME
    if (candidate / "index.html").is_file():
        return candidate
    return None


def bundled_config_template() -> Path | None:
    """Return the packaged ``.env.example`` template when available."""
    root = bundled_resource_root()
    if root is None:
        return None
    candidate = root / CONFIG_DIRNAME / ".env.example"
    if candidate.is_file():
        return candidate
    return None


def bundled_speech_provider() -> Path | None:
    """Return the bundled macOS speech provider helper when available."""
    root = bundled_resource_root()
    if root is None:
        return None
    candidate = root / BIN_DIRNAME / MACOS_SPEECH_PROVIDER_NAME
    if candidate.is_file():
        return candidate
    return None
