"""Deployment readiness checks for local VocalizeAI installs."""
from __future__ import annotations

import asyncio
import json
import os
import platform
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from openai import AsyncOpenAI

from vocalize.config import Config
from vocalize.install_state import INSTALL_MARKER
from vocalize.llm.openai_compat import _thinking_extra_body
from vocalize.provider_runtime import ensure_speech_provider_started


@dataclass(frozen=True)
class DoctorCheck:
    name: str
    ok: bool
    detail: str
    remediation: str | None = None


def run_doctor(
    cfg: Config | None = None,
    *,
    skip_llm_probe: bool = False,
) -> list[DoctorCheck]:
    cfg = cfg or Config.from_env()
    checks: list[DoctorCheck] = [
        _check_macos(),
        _check_install_layout(),
        _check_llm_config(cfg),
    ]
    if not cfg.validate_for_phase("llm"):
        checks.append(_check_llm_probe(cfg, skip=skip_llm_probe))
    checks.append(_check_speech_provider(cfg))
    return checks


def _check_macos() -> DoctorCheck:
    if platform.system() == "Darwin":
        return DoctorCheck("macos", True, platform.platform())
    return DoctorCheck(
        "macos",
        False,
        f"unsupported platform: {platform.system()}",
        "v0.1.0 only supports macOS as the public local runtime",
    )


def _check_llm_config(cfg: Config) -> DoctorCheck:
    missing = cfg.validate_for_phase("llm")
    if not missing:
        return DoctorCheck("llm_config", True, cfg.openai_model)
    return DoctorCheck(
        "llm_config",
        False,
        f"missing: {', '.join(missing)}",
        "set OPENAI_API_KEY, OPENAI_BASE_URL, and OPENAI_MODEL in .env",
    )


def _check_install_layout() -> DoctorCheck:
    raw_root = os.getenv("VOCALIZE_INSTALL_ROOT")
    if not raw_root:
        return DoctorCheck("install_layout", True, "source/dev mode")

    root = Path(raw_root)
    required = [
        root / INSTALL_MARKER,
        root / "vocalize",
        root / "bin",
        root / "app",
        root / "config",
        root / "logs",
        root / "cache",
        root / "VERSION",
    ]
    missing = [str(path.relative_to(root)) for path in required if not path.exists()]
    if missing:
        return DoctorCheck(
            "install_layout",
            False,
            f"missing: {', '.join(missing)}",
            f"repair or reinstall the local VocalizeAI directory: {root}",
        )
    return DoctorCheck("install_layout", True, str(root))


def _check_llm_probe(cfg: Config, *, skip: bool) -> DoctorCheck:
    if skip:
        return DoctorCheck("llm_probe", True, "skipped by --skip-llm-probe")
    try:
        result = asyncio.run(_run_llm_full_agent_probe(cfg))
    except Exception as exc:
        return DoctorCheck(
            "llm_probe",
            False,
            _classify_llm_probe_error(exc),
            "rerun `./vocalize setup` to adjust thinking mode; verify endpoint, API key, model, streaming, tool calling, and JSON mode",
        )
    return DoctorCheck("llm_probe", True, result)


async def _run_llm_full_agent_probe(cfg: Config) -> str:
    client = AsyncOpenAI(
        api_key=cfg.openai_api_key,
        base_url=cfg.openai_base_url,
        timeout=20.0,
        max_retries=0,
    )
    json_kwargs: dict[str, Any] = {
        "model": cfg.openai_model,
        "messages": [
            {
                "role": "user",
                "content": 'Return only this JSON object: {"ok": true}',
            }
        ],
        "response_format": {"type": "json_object"},
        "stream": True,
    }
    extra_body = _thinking_extra_body(cfg.openai_thinking_mode)
    if extra_body is not None:
        json_kwargs["extra_body"] = extra_body
    json_stream = await client.chat.completions.create(**json_kwargs)
    json_text = ""
    async for chunk in json_stream:
        if not chunk.choices:
            continue
        content = chunk.choices[0].delta.content
        if content:
            json_text += content
    parsed = json.loads(json_text)
    if parsed.get("ok") is not True:
        raise RuntimeError("schema adherence probe returned unexpected JSON")

    tool_kwargs: dict[str, Any] = {
        "model": cfg.openai_model,
        "messages": [
            {
                "role": "user",
                "content": "Call the readiness tool with ok=true.",
            }
        ],
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "report_readiness",
                    "description": "Report readiness probe result.",
                    "parameters": {
                        "type": "object",
                        "properties": {"ok": {"type": "boolean"}},
                        "required": ["ok"],
                    },
                },
            }
        ],
        "tool_choice": {
            "type": "function",
            "function": {"name": "report_readiness"},
        },
        "stream": True,
    }
    extra_body = _thinking_extra_body(cfg.openai_thinking_mode)
    if extra_body is not None:
        tool_kwargs["extra_body"] = extra_body
    tool_stream = await client.chat.completions.create(**tool_kwargs)
    saw_tool_call = False
    async for chunk in tool_stream:
        if not chunk.choices:
            continue
        delta = chunk.choices[0].delta
        tool_calls: Any = getattr(delta, "tool_calls", None)
        if tool_calls:
            saw_tool_call = True
    if not saw_tool_call:
        raise RuntimeError("tool calling probe did not emit a tool call")
    return "streaming, tool calling, and JSON mode passed"


def _classify_llm_probe_error(exc: Exception) -> str:
    message = str(exc)
    lowered = message.lower()
    if "401" in message or "unauthorized" in lowered or "authentication" in lowered:
        return f"authentication failed: {message}"
    if "404" in message or "model" in lowered:
        return f"model or endpoint failed: {message}"
    if "response_format" in lowered or "json" in lowered:
        return f"JSON/schema probe failed: {message}"
    if "tool" in lowered:
        return f"tool calling probe failed: {message}"
    if "timeout" in lowered or "connect" in lowered or "network" in lowered:
        return f"network reachability failed: {message}"
    return f"LLM probe failed: {message}"


def _check_speech_provider(cfg: Config) -> DoctorCheck:
    url = _capabilities_url(cfg.stt_provider_url)
    process = None
    try:
        process = ensure_speech_provider_started(cfg)
        body = _read_provider_capabilities(url, timeout_s=cfg.provider_connect_timeout_s)
        speech_status, mic_status, voices = _extract_permission_summary(body)
        if speech_status == "not_determined" or mic_status == "not_determined":
            _request_provider_permissions(
                cfg.stt_provider_url,
                timeout_s=max(cfg.provider_connect_timeout_s, 30.0),
            )
            body = _read_provider_capabilities(
                url,
                timeout_s=cfg.provider_connect_timeout_s,
            )
    except (
        OSError,
        RuntimeError,
        TimeoutError,
        urllib.error.URLError,
        json.JSONDecodeError,
    ) as exc:
        return DoctorCheck(
            "speech_provider",
            False,
            f"unreachable: {url} ({exc})",
            "run the macOS speech provider helper or fix VOCALIZE_STT_PROVIDER_URL",
        )
    finally:
        if process is not None:
            process.terminate()

    speech_status, mic_status, voices = _extract_permission_summary(body)

    problems: list[str] = []
    if speech_status not in {"authorized", ""}:
        problems.append(f"speech permission is {speech_status}")
    if mic_status not in {"authorized", ""}:
        problems.append(f"microphone permission is {mic_status}")
    if voices <= 0:
        problems.append("no TTS voices available")

    if problems:
        return DoctorCheck(
            "speech_provider",
            False,
            "; ".join(problems),
            "grant Speech Recognition permission and install at least one macOS voice",
        )
    return DoctorCheck("speech_provider", True, "provider reachable")


def _capabilities_url(base_url: str) -> str:
    parsed = urlparse(base_url)
    scheme = {"ws": "http", "wss": "https"}.get(parsed.scheme, parsed.scheme)
    return f"{scheme}://{parsed.netloc}/v1/capabilities"


def _permissions_request_url(base_url: str) -> str:
    parsed = urlparse(base_url)
    scheme = {"ws": "http", "wss": "https"}.get(parsed.scheme, parsed.scheme)
    return f"{scheme}://{parsed.netloc}/v1/permissions/request"


def _read_provider_capabilities(url: str, *, timeout_s: float) -> dict[str, Any]:
    with urllib.request.urlopen(url, timeout=timeout_s) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    if not isinstance(body, dict):
        raise json.JSONDecodeError("provider capabilities must be an object", "", 0)
    return body


def _request_provider_permissions(base_url: str, *, timeout_s: float) -> None:
    request = urllib.request.Request(
        _permissions_request_url(base_url),
        data=b"{}",
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=timeout_s) as resp:
        resp.read()


def _extract_permission_summary(body: dict[str, Any]) -> tuple[str, str, int]:
    permissions = body.get("permissions")
    speech_status = ""
    mic_status = ""
    voices = 0
    if isinstance(permissions, dict):
        speech_status = str(
            permissions.get("speech_recognition")
            or permissions.get("speechRecognition")
            or ""
        )
        mic_status = str(permissions.get("microphone") or "")
        try:
            voices = int(
                permissions.get("tts_voices_available")
                or permissions.get("ttsVoicesAvailable")
                or 0
            )
        except (TypeError, ValueError):
            voices = 0
    return speech_status, mic_status, voices
