"""/health endpoint.

Reports two booleans:

- ``ok``: always True when the server is reachable (the request itself
  proves it).
- ``gpu_reachable``: True if TCP connects to the configured GPU service ports
  succeeded within a short timeout. The default probe is provided by
  ``make_default_gpu_probe()`` reading the same app config as STT/TTS clients;
  tests inject a fake probe to avoid network.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable

from fastapi import FastAPI

from vocalize.config import Config

log = logging.getLogger(__name__)

GpuProbe = Callable[[], Awaitable[bool]]


def make_default_gpu_probe(
    *,
    timeout_s: float = 1.5,
) -> GpuProbe:
    """Return a probe that TCP-connects to configured STT and TTS services.

    ``Config`` is the source of truth: ``GPU_HOST``, ``SENSEVOICE_WS_PORT``,
    and ``COSYVOICE_WS_PORT``. If ``GPU_HOST`` is unset, the probe returns
    False without a network call and treats "no GPU configured" as
    "GPU unreachable" rather than crashing.
    """
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
        # GPU node and eventually exhausts file descriptors / endpoint
        # capacity.
        writer.close()
        try:
            await writer.wait_closed()
        except (OSError, asyncio.TimeoutError):
            pass  # close-side errors are not interesting for a probe
        return True

    async def probe() -> bool:
        cfg = Config.from_env()
        if not cfg.gpu_host:
            return False
        ports = (cfg.sensevoice_ws_port, cfg.cosyvoice_ws_port)
        for port in ports:
            if not await _can_connect(cfg.gpu_host, port):
                return False
        return True

    return probe


def register_health_routes(app: FastAPI, *, gpu_probe: GpuProbe) -> None:
    @app.get("/health")
    async def health() -> dict:
        try:
            reachable = await gpu_probe()
        except Exception:
            log.warning("health: gpu_probe raised; reporting unreachable", exc_info=True)
            reachable = False
        return {"ok": True, "gpu_reachable": reachable}


__all__ = ["GpuProbe", "make_default_gpu_probe", "register_health_routes"]
