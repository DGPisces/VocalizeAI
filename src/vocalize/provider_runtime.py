"""Lifecycle helpers for the local speech Provider API process."""
from __future__ import annotations

import os
import shlex
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from vocalize.config import Config


@dataclass
class SpeechProviderProcess:
    process: subprocess.Popen
    capabilities_url: str

    def terminate(self, *, timeout_s: float = 3.0) -> None:
        if self.process.poll() is not None:
            return
        self.process.terminate()
        try:
            self.process.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=timeout_s)


def ensure_speech_provider_started(cfg: Config) -> SpeechProviderProcess | None:
    """Start the configured local speech provider when auto-start is enabled."""
    if not cfg.speech_provider_auto_start:
        return None
    if not cfg.speech_provider_command:
        raise RuntimeError(
            "VOCALIZE_SPEECH_PROVIDER_AUTO_START=1 requires "
            "VOCALIZE_SPEECH_PROVIDER_COMMAND"
        )

    capabilities_url = _capabilities_url(cfg.stt_provider_url)
    if _capabilities_ready(capabilities_url, timeout_s=0.25):
        return None

    env = os.environ.copy()
    parsed = urlparse(cfg.stt_provider_url)
    if parsed.port is not None:
        env.setdefault("VOCALIZE_SPEECH_PROVIDER_PORT", str(parsed.port))

    process = subprocess.Popen(  # noqa: S603 - operator-provided local command.
        _command_args(cfg.speech_provider_command),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    deadline = time.monotonic() + cfg.speech_provider_startup_timeout_s
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(
                "speech provider exited during startup "
                f"(code={process.returncode})"
            )
        if _capabilities_ready(capabilities_url, timeout_s=0.25):
            return SpeechProviderProcess(process=process, capabilities_url=capabilities_url)
        time.sleep(0.1)

    process.terminate()
    raise RuntimeError(
        "speech provider did not become ready at "
        f"{capabilities_url} within {cfg.speech_provider_startup_timeout_s}s"
    )


def _capabilities_url(base_url: str) -> str:
    parsed = urlparse(base_url)
    scheme = {"ws": "http", "wss": "https"}.get(parsed.scheme, parsed.scheme)
    return f"{scheme}://{parsed.netloc}/v1/capabilities"


def _command_args(command: str) -> list[str]:
    if Path(command).exists():
        return [command]
    return shlex.split(command)


def _capabilities_ready(url: str, *, timeout_s: float) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout_s) as response:
            status = int(response.status)
            return 200 <= status < 300
    except (OSError, urllib.error.URLError, TimeoutError):
        return False
