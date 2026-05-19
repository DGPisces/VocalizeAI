"""Server composition tests."""
from __future__ import annotations

from fastapi.testclient import TestClient

from vocalize.config import reset_config
from vocalize.llm.openai_compat import OpenAICompatClient
from vocalize.server import _default_user_pipeline_factory
from vocalize.server import create_app
from vocalize.stt.sensevoice import SenseVoiceClient
from vocalize.stt.sensevoice import SenseVoiceError
from vocalize.tts.cosyvoice import CosyVoiceClient


class _FakeTransport:
    sample_rate = 16_000
    channels = 1
    encoding = "pcm_s16le"


def test_default_pipeline_factory_builds_clients_from_app_config(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://example.test/v1")
    monkeypatch.setenv("OPENAI_MODEL", "test-model")
    monkeypatch.setenv("GPU_HOST", "127.0.0.1")
    monkeypatch.setenv("SENSEVOICE_WS_PORT", "18000")
    monkeypatch.setenv("COSYVOICE_WS_PORT", "18001")
    monkeypatch.setenv("DEFAULT_LANGUAGE", "zh")
    reset_config()

    pipeline = _default_user_pipeline_factory(_FakeTransport())

    assert isinstance(pipeline._stt, SenseVoiceClient)
    assert pipeline._stt.host == "127.0.0.1"
    assert pipeline._stt.port == 18000
    assert pipeline._stt.language_hint == "zh"
    assert isinstance(pipeline._llm, OpenAICompatClient)
    assert isinstance(pipeline._tts, CosyVoiceClient)
    assert pipeline._tts.host == "127.0.0.1"
    assert pipeline._tts.port == 18001


def test_sensevoice_from_app_config_requires_gpu_host(monkeypatch) -> None:
    from vocalize.config import Config

    monkeypatch.delenv("GPU_HOST", raising=False)
    reset_config()

    try:
        SenseVoiceClient.from_app_config(Config.from_env())
    except SenseVoiceError as exc:
        assert "GPU_HOST" in str(exc)
    else:  # pragma: no cover - assertion clarity
        raise AssertionError("SenseVoiceClient.from_app_config accepted missing GPU_HOST")


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
