"""Server composition tests."""
from __future__ import annotations

from fastapi.testclient import TestClient

from vocalize.config import reset_config
from vocalize.llm.openai_compat import OpenAICompatClient
from vocalize.providers import ProviderSTTClient, ProviderTTSClient
from vocalize.providers.speech import SpeechProviderError
from vocalize.server import _default_user_pipeline_factory
from vocalize.server import create_app


class _FakeTransport:
    sample_rate = 16_000
    channels = 1
    encoding = "pcm_s16le"


def test_default_pipeline_factory_builds_clients_from_app_config(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://example.test/v1")
    monkeypatch.setenv("OPENAI_MODEL", "test-model")
    monkeypatch.setenv("VOCALIZE_STT_PROVIDER_URL", "http://127.0.0.1:18000")
    monkeypatch.setenv("VOCALIZE_TTS_PROVIDER_URL", "http://127.0.0.1:18001")
    monkeypatch.setenv("DEFAULT_LANGUAGE", "zh")
    reset_config()

    pipeline = _default_user_pipeline_factory(_FakeTransport())

    assert isinstance(pipeline._stt, ProviderSTTClient)
    assert pipeline._stt.base_url == "http://127.0.0.1:18000"
    assert pipeline._stt.ws_url == "ws://127.0.0.1:18000/v1/stt/stream"
    assert pipeline._stt.language_hint == "zh"
    assert isinstance(pipeline._llm, OpenAICompatClient)
    assert isinstance(pipeline._tts, ProviderTTSClient)
    assert pipeline._tts.base_url == "http://127.0.0.1:18001"
    assert pipeline._tts.ws_url == "ws://127.0.0.1:18001/v1/tts/stream"


def test_provider_url_validation_rejects_unknown_scheme() -> None:
    client = ProviderSTTClient(base_url="ftp://127.0.0.1:18000")
    try:
        _ = client.ws_url
    except SpeechProviderError as exc:
        assert "scheme" in str(exc)
    else:  # pragma: no cover - assertion clarity
        raise AssertionError("ProviderSTTClient accepted unsupported URL scheme")


def test_create_app_allows_readme_127_frontend_origin(monkeypatch) -> None:
    monkeypatch.delenv("VOCALIZE_CORS_ORIGINS", raising=False)
    # D-11: startup raises when VOCALIZE_HOST is non-localhost and VOCALIZE_WS_BASE_URL
    # is unset. Set localhost mode so this test checks CORS, not the startup guard.
    monkeypatch.setenv("VOCALIZE_HOST", "127.0.0.1")
    monkeypatch.delenv("VOCALIZE_WS_BASE_URL", raising=False)
    reset_config()

    app = create_app()

    with TestClient(app) as client:
        response = client.options(
            "/api/sessions",
            headers={
                "Origin": "http://127.0.0.1:3000",
                "Access-Control-Request-Method": "POST",
            },
        )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "http://127.0.0.1:3000"


def test_create_app_serves_built_vite_frontend(monkeypatch, tmp_path) -> None:
    dist = tmp_path / "dist"
    assets = dist / "assets"
    assets.mkdir(parents=True)
    (dist / "index.html").write_text("<main>VocalizeAI console</main>", encoding="utf-8")
    (assets / "app.js").write_text("console.log('vocalize')", encoding="utf-8")

    monkeypatch.setenv("VOCALIZE_HOST", "127.0.0.1")
    monkeypatch.setenv("VOCALIZE_FRONTEND_DIST", str(dist))
    monkeypatch.delenv("VOCALIZE_WS_BASE_URL", raising=False)
    reset_config()

    app = create_app()

    with TestClient(app) as client:
        index = client.get("/")
        spa_route = client.get("/zh/live/session-1")
        asset = client.get("/assets/app.js")
        api_miss = client.get("/api/not-found")

    assert index.status_code == 200
    assert "VocalizeAI console" in index.text
    assert spa_route.status_code == 200
    assert "VocalizeAI console" in spa_route.text
    assert asset.status_code == 200
    assert "vocalize" in asset.text
    assert api_miss.status_code == 404
