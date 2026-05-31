"""HTTP + WebSocket server package — see .planning/ history (Phase 1 plans).

``create_app()`` is the single composition root that ``main.py`` uses. Tests
that need a fresh app per case can call it directly with a custom
``runner_factory``.
"""
from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from prometheus_fastapi_instrumentator import Instrumentator

from vocalize.server.health import make_default_speech_provider_probe, register_health_routes
from vocalize.server.metrics import install_error_counter, refresh_runtime_gauges
from vocalize.server.runner import DialogueOrchestratorRunner
from vocalize.server.sessions import register_session_routes
from vocalize.server.state import SessionRegistry
from vocalize.server.ws import register_ws_routes


def _default_user_pipeline_factory(transport):
    """Build a production VoicePipeline for one WS session.

    ``VoicePipeline`` requires a ``system_prompt`` (it stores the prompt
    in its own messages list). Inside ``DialogueOrchestrator`` the
    pipeline's messages list is bypassed — the orchestrator owns
    per-channel ``messages`` lists with prompts rendered from the L2/L3
    templates — so the value here is effectively a placeholder. We pass
    an empty string rather than a misleading sentence so it's obvious
    nothing semantic depends on it.
    """
    from vocalize.config import get_config
    from vocalize.llm.openai_compat import OpenAICompatClient
    from vocalize.pipeline import VoicePipeline
    from vocalize.providers import ProviderSTTClient, ProviderTTSClient

    config = get_config()
    return VoicePipeline(
        transport=transport,
        system_prompt="",
        stt=ProviderSTTClient.from_app_config(config),
        llm=OpenAICompatClient.from_app_config(config),
        tts=ProviderTTSClient.from_app_config(config),
    )


_DEFAULT_PROD_ORIGINS: list[str] = []
_DEFAULT_DEV_ORIGINS = ["http://localhost:3000", "http://127.0.0.1:3000"]
_RESERVED_FRONTEND_FALLBACK_SEGMENTS = {
    "api",
    "ws",
    "health",
    "metrics",
    "docs",
    "redoc",
    "openapi.json",
}


def _frontend_dist_dir() -> Path:
    raw = os.getenv("VOCALIZE_FRONTEND_DIST")
    if raw:
        return Path(raw).expanduser()
    from vocalize.runtime_paths import bundled_frontend_dist

    bundled = bundled_frontend_dist()
    if bundled is not None:
        return bundled
    return Path(__file__).resolve().parents[3] / "frontend" / "dist"


def register_frontend_routes(app: FastAPI) -> None:
    """Serve the built Vite console when ``frontend/dist`` is present."""
    dist_dir = _frontend_dist_dir()
    index_file = dist_dir / "index.html"
    if not index_file.is_file():
        return

    assets_dir = dist_dir / "assets"
    if assets_dir.is_dir():
        app.mount(
            "/assets",
            StaticFiles(directory=assets_dir),
            name="frontend-assets",
        )

    @app.get("/", include_in_schema=False)
    async def _frontend_index() -> FileResponse:
        return FileResponse(index_file)

    @app.get("/{path:path}", include_in_schema=False)
    async def _frontend_spa(path: str) -> FileResponse:
        first_segment = path.split("/", 1)[0]
        if first_segment in _RESERVED_FRONTEND_FALLBACK_SEGMENTS:
            raise HTTPException(status_code=404)

        requested_file = dist_dir / path
        if requested_file.is_file():
            return FileResponse(requested_file)
        return FileResponse(index_file)


def create_app() -> FastAPI:
    """Build the production FastAPI app.

    Env vars:
        VOCALIZE_HOST / VOCALIZE_PORT — where uvicorn binds (handled by main()).
        VOCALIZE_CORS_ORIGINS — comma-separated allowed origins; defaults to
            dev origins when VOCALIZE_HOST is 127.0.0.1/localhost, else
            empty (operator MUST set this env var in non-localhost mode — see D-10).
        VOCALIZE_WS_BASE_URL — REQUIRED when VOCALIZE_HOST is not localhost.
            The public WS prefix echoed back from POST /api/sessions.
            Raises RuntimeError at startup when absent in non-localhost mode
            (closes Host-header spoofing vector D-11 — see CONCERNS.md).
            In localhost-dev mode the WS URL is derived from the request base_url.
        VOCALIZE_STT_PROVIDER_URL / VOCALIZE_TTS_PROVIDER_URL — Provider API
            endpoints for speech recognition and speech synthesis.
    """
    @asynccontextmanager
    async def _lifespan(app: FastAPI):
        yield
        process = getattr(app.state, "speech_provider_process", None)
        if process is not None:
            process.terminate()

    app = FastAPI(title="VocalizeAI", version="0.1.0", lifespan=_lifespan)

    # --- Prometheus metrics (/metrics endpoint) ---
    # Mount BEFORE CORS middleware so the instrumentator middleware sees all
    # requests; /metrics and /health are excluded from the histogram to keep
    # scrape latency out of p99 (T-04b-02).
    install_error_counter()
    Instrumentator(
        excluded_handlers=["/metrics", "/health"],
        should_group_status_codes=True,
    ).instrument(app).expose(app, endpoint="/metrics")

    # --- Env-conditional CORS (D-10) ---
    host = os.getenv("VOCALIZE_HOST", "0.0.0.0")
    is_localhost = host in {"127.0.0.1", "localhost"}
    default_origins = _DEFAULT_DEV_ORIGINS if is_localhost else _DEFAULT_PROD_ORIGINS
    cors_origins_raw = os.getenv("VOCALIZE_CORS_ORIGINS")
    if cors_origins_raw:
        cors_origins = [o.strip() for o in cors_origins_raw.split(",") if o.strip()]
        if "*" in cors_origins:
            raise RuntimeError(
                "VOCALIZE_CORS_ORIGINS must not contain '*'; "
                "use explicit origin URLs (D-10). Got: %r" % cors_origins_raw
            )
    else:
        cors_origins = default_origins
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins,
        allow_credentials=False,
        allow_methods=["GET", "POST", "DELETE"],  # D-10: explicit list; no wildcards
        allow_headers=["Content-Type"],  # explicit; no wildcards
    )

    registry = SessionRegistry()
    app.state.registry = registry

    @app.middleware("http")
    async def _refresh_runtime_gauges_on_metrics_scrape(
        request: Request, call_next: object
    ) -> Response:
        """Refresh process-level gauges just before a Prometheus scrape.

        Only fires on /metrics requests to keep overhead negligible on all
        other paths (RESEARCH §Pattern 4).
        """
        if request.url.path == "/metrics":
            refresh_runtime_gauges(app.state.registry)
        return await call_next(request)  # type: ignore[operator]

    # --- VOCALIZE_WS_BASE_URL enforcement (D-11) ---
    # Raises at startup so uvicorn never binds in a misconfigured state,
    # closing the Host-header spoofing vector described in CONCERNS.md.
    ws_base = os.getenv("VOCALIZE_WS_BASE_URL")
    if not is_localhost and not ws_base:
        raise RuntimeError(
            "VOCALIZE_WS_BASE_URL is required when VOCALIZE_HOST is not localhost "
            "(closes Host-header spoofing vector — see CONCERNS.md). "
            "Example: wss://api.example.com"
        )

    from vocalize.config import get_config
    from vocalize.provider_runtime import ensure_speech_provider_started

    config = get_config()
    speech_provider_process = ensure_speech_provider_started(config)
    app.state.speech_provider_process = speech_provider_process

    register_session_routes(app, registry=registry)
    register_health_routes(app, provider_probe=make_default_speech_provider_probe())
    register_ws_routes(
        app,
        registry=registry,
        runner_factory=lambda session: DialogueOrchestratorRunner(
            session=session,
            user_pipeline_factory=_default_user_pipeline_factory,
            merchant_pipeline_factory=_default_user_pipeline_factory,
        ),
    )
    register_frontend_routes(app)
    return app


__all__ = ["create_app", "register_frontend_routes"]
