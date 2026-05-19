"""/health endpoint tests.

The endpoint reports both server liveness and GPU node reachability
(``gpu_reachable``). Reachability is probed by attempting a TCP connect to
the GPU host:port from env. We monkey-patch the probe so tests don't depend
on real network state.
"""
from __future__ import annotations

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from vocalize.server.health import make_default_gpu_probe, register_health_routes


def _app(probe) -> FastAPI:
    app = FastAPI()
    register_health_routes(app, gpu_probe=probe)
    return app


async def _request(app: FastAPI) -> dict:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.get("/health")
        assert resp.status_code == 200
        return resp.json()


async def test_health_reports_ok_and_gpu_reachable() -> None:
    async def probe() -> bool:
        return True

    body = await _request(_app(probe))
    assert body == {"ok": True, "gpu_reachable": True}


async def test_health_reports_gpu_unreachable() -> None:
    async def probe() -> bool:
        return False

    body = await _request(_app(probe))
    assert body == {"ok": True, "gpu_reachable": False}


async def test_health_swallows_probe_exception() -> None:
    """A flaky probe (DNS error, timeout) MUST not 500 the health endpoint —
    operations relies on /health being always up.
    """
    async def probe() -> bool:
        raise RuntimeError("DNS down")

    body = await _request(_app(probe))
    assert body == {"ok": True, "gpu_reachable": False}


async def test_default_gpu_probe_uses_app_config_env(monkeypatch) -> None:
    """The default probe must use the same GPU env namespace as real clients."""
    opened: list[tuple[str, int]] = []

    class _Writer:
        def close(self) -> None:
            pass

        async def wait_closed(self) -> None:
            pass

    async def fake_open_connection(host: str, port: int):
        opened.append((host, port))
        return object(), _Writer()

    monkeypatch.setenv("GPU_HOST", "100.64.0.8")
    monkeypatch.setenv("SENSEVOICE_WS_PORT", "18080")
    monkeypatch.setenv("COSYVOICE_WS_PORT", "18081")
    monkeypatch.delenv("VOCALIZE_GPU_HOST", raising=False)
    monkeypatch.delenv("VOCALIZE_GPU_PORT", raising=False)
    monkeypatch.setattr("asyncio.open_connection", fake_open_connection)

    reachable = await make_default_gpu_probe()()

    assert reachable is True
    assert opened == [("100.64.0.8", 18080), ("100.64.0.8", 18081)]


async def test_default_gpu_probe_reports_false_without_gpu_host(monkeypatch) -> None:
    monkeypatch.delenv("GPU_HOST", raising=False)
    monkeypatch.delenv("VOCALIZE_GPU_HOST", raising=False)

    reachable = await make_default_gpu_probe()()

    assert reachable is False


async def test_default_gpu_probe_short_circuits_on_first_failed_port(
    monkeypatch,
) -> None:
    opened: list[tuple[str, int]] = []

    async def fake_open_connection(host: str, port: int):
        opened.append((host, port))
        raise OSError("first service down")

    monkeypatch.setenv("GPU_HOST", "100.64.0.8")
    monkeypatch.setenv("SENSEVOICE_WS_PORT", "18080")
    monkeypatch.setenv("COSYVOICE_WS_PORT", "18081")
    monkeypatch.setattr("asyncio.open_connection", fake_open_connection)

    reachable = await make_default_gpu_probe()()

    assert reachable is False
    assert opened == [("100.64.0.8", 18080)]
