"""/health endpoint.

Reports two booleans:

- ``ok``: always True when the server is reachable (the request itself
  proves it).
- ``speech_provider_reachable``: True if TCP connects to the configured STT/TTS
  Provider API endpoints within a short timeout. Tests inject a fake probe to
  avoid network.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable
from urllib.parse import urlparse

from fastapi import FastAPI

from vocalize.config import Config

log = logging.getLogger(__name__)

ProviderProbe = Callable[[], Awaitable[bool]]


def _host_port_from_url(url: str) -> tuple[str, int]:
    parsed = urlparse(url)
    if not parsed.hostname:
        raise ValueError(f"provider URL must include a host: {url!r}")
    if parsed.port is not None:
        return parsed.hostname, parsed.port
    if parsed.scheme in {"https", "wss"}:
        return parsed.hostname, 443
    return parsed.hostname, 80


def make_default_speech_provider_probe(
    *,
    timeout_s: float = 1.5,
) -> ProviderProbe:
    """Return a probe that TCP-connects to configured speech providers."""

    async def _can_connect(host: str, port: int) -> bool:
        try:
            _reader, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port),
                timeout=timeout_s,
            )
        except (OSError, asyncio.TimeoutError):
            return False
        # Close immediately; the probe is liveness only. If we leave the
        # writer open, every /health call leaks a TCP session against the
        # speech provider and eventually exhausts file descriptors / endpoint
        # capacity.
        writer.close()
        try:
            await writer.wait_closed()
        except (OSError, asyncio.TimeoutError):
            pass  # close-side errors are not interesting for a probe
        return True

    async def probe() -> bool:
        cfg = Config.from_env()
        try:
            endpoints = list(
                dict.fromkeys(
                    [
                        _host_port_from_url(cfg.stt_provider_url),
                        _host_port_from_url(cfg.tts_provider_url),
                    ]
                )
            )
        except ValueError:
            return False
        for host, port in endpoints:
            if not await _can_connect(host, port):
                return False
        return True

    return probe


def register_health_routes(app: FastAPI, *, provider_probe: ProviderProbe) -> None:
    @app.get("/health")
    async def health() -> dict:
        try:
            reachable = await provider_probe()
        except Exception:
            log.warning(
                "health: provider_probe raised; reporting unreachable",
                exc_info=True,
            )
            reachable = False
        return {"ok": True, "speech_provider_reachable": reachable}


__all__ = [
    "ProviderProbe",
    "make_default_speech_provider_probe",
    "register_health_routes",
]
