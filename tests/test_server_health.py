"""/health endpoint tests."""
from __future__ import annotations

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from vocalize.server.health import (
    make_default_speech_provider_probe,
    register_health_routes,
)


def _app(probe) -> FastAPI:
    app = FastAPI()
    register_health_routes(app, provider_probe=probe)
    return app


async def _request(app: FastAPI) -> dict:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.get("/health")
        assert resp.status_code == 200
        return resp.json()


async def test_health_reports_ok_and_speech_provider_reachable() -> None:
    async def probe() -> bool:
        return True

    body = await _request(_app(probe))
    assert body == {"ok": True, "speech_provider_reachable": True}


async def test_health_reports_speech_provider_unreachable() -> None:
    async def probe() -> bool:
        return False

    body = await _request(_app(probe))
    assert body == {"ok": True, "speech_provider_reachable": False}


async def test_health_swallows_probe_exception() -> None:
    """A flaky probe (DNS error, timeout) MUST not 500 the health endpoint —
    operations relies on /health being always up.
    """
    async def probe() -> bool:
        raise RuntimeError("DNS down")

    body = await _request(_app(probe))
    assert body == {"ok": True, "speech_provider_reachable": False}


async def test_default_provider_probe_uses_app_config_env(monkeypatch) -> None:
    """The default probe must use the same provider URLs as real clients."""
    opened: list[tuple[str, int]] = []

    class _Writer:
        def close(self) -> None:
            pass

        async def wait_closed(self) -> None:
            pass

    async def fake_open_connection(host: str, port: int):
        opened.append((host, port))
        return object(), _Writer()

    monkeypatch.setenv("VOCALIZE_STT_PROVIDER_URL", "http://100.64.0.8:18080")
    monkeypatch.setenv("VOCALIZE_TTS_PROVIDER_URL", "http://100.64.0.8:18081")
    monkeypatch.setattr("asyncio.open_connection", fake_open_connection)

    reachable = await make_default_speech_provider_probe()()

    assert reachable is True
    assert opened == [("100.64.0.8", 18080), ("100.64.0.8", 18081)]


async def test_default_provider_probe_reports_false_for_invalid_url(monkeypatch) -> None:
    monkeypatch.setenv("VOCALIZE_STT_PROVIDER_URL", "not-a-url")
    monkeypatch.setenv("VOCALIZE_TTS_PROVIDER_URL", "http://127.0.0.1:18081")

    reachable = await make_default_speech_provider_probe()()

    assert reachable is False


async def test_default_provider_probe_short_circuits_on_first_failed_port(
    monkeypatch,
) -> None:
    opened: list[tuple[str, int]] = []

    async def fake_open_connection(host: str, port: int):
        opened.append((host, port))
        raise OSError("first service down")

    monkeypatch.setenv("VOCALIZE_STT_PROVIDER_URL", "http://100.64.0.8:18080")
    monkeypatch.setenv("VOCALIZE_TTS_PROVIDER_URL", "http://100.64.0.8:18081")
    monkeypatch.setattr("asyncio.open_connection", fake_open_connection)

    reachable = await make_default_speech_provider_probe()()

    assert reachable is False
    assert opened == [("100.64.0.8", 18080)]
