from __future__ import annotations

import asyncio
import json
import os
import queue
import shutil
import subprocess
import threading
import time
from collections.abc import AsyncIterator, Callable, Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Literal

import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient
from openai import AsyncOpenAI
from pydantic import BaseModel, Field, ValidationError, model_validator

from vocalize.llm.base import ChatMessage, FinishChunk, LLMChunk, TextDelta, ToolCallDelta, ToolDef
from vocalize.pipeline import VoicePipeline
from vocalize.server.runner import DialogueOrchestratorRunner
from vocalize.server.sessions import register_session_routes
from vocalize.server.state import Session, SessionRegistry
from vocalize.server.ws import register_ws_routes
from vocalize.stt.base import Transcript
from vocalize.transports.base import AudioEncoding, AudioTransport
from vocalize.tts.base import TextChunk

SCENARIO_PATH = Path(__file__).with_name("scenarios.yaml")
DEFAULT_EVIDENCE_DIR = Path(__file__).with_name("evidence")

ScenarioGate = Literal["ci", "release_audio"]
MerchantStyle = Literal["direct", "impatient", "follow_up"]
ConversationLang = Literal["zh", "en"]


class ScenarioCheck(BaseModel):
    id: str = Field(min_length=1)
    description: str = Field(min_length=1)
    must_pass: bool = True


class ScenarioSeed(BaseModel):
    id: str = Field(min_length=1)
    merchant_style: MerchantStyle
    merchant_turns: list[str] = Field(min_length=1)


class Scenario(BaseModel):
    id: str = Field(min_length=1)
    gate: ScenarioGate
    behavior: str = Field(min_length=1)
    task: str = Field(min_length=1)
    user_lang: ConversationLang
    merchant_lang: ConversationLang
    checks: list[ScenarioCheck]
    seeds: list[ScenarioSeed]

    @model_validator(mode="after")
    def _validate_matrix_shape(self) -> "Scenario":
        if len(self.checks) < 4 or len(self.checks) > 6:
            raise ValueError(f"{self.id} must define 4 to 6 checks")
        if len(self.seeds) != 3:
            raise ValueError(f"{self.id} must define exactly 3 seeds")
        check_ids = [check.id for check in self.checks]
        if len(set(check_ids)) != len(check_ids):
            raise ValueError(f"{self.id} contains duplicate check ids")
        seed_ids = [seed.id for seed in self.seeds]
        if len(set(seed_ids)) != len(seed_ids):
            raise ValueError(f"{self.id} contains duplicate seed ids")
        return self


class ScenarioCase(BaseModel):
    scenario: Scenario
    seed: ScenarioSeed

    @property
    def scenario_id(self) -> str:
        return self.scenario.id

    @property
    def seed_id(self) -> str:
        return self.seed.id

    @property
    def case_id(self) -> str:
        return f"{self.scenario_id}::{self.seed_id}"


class TextBypassResult(BaseModel):
    scenario_id: str
    seed: str
    session_id: str
    provider: str
    client_frames: list[dict[str, Any]]
    server_frames: list[dict[str, Any]]
    transcript: list[dict[str, Any]]
    timing: dict[str, Any] = Field(default_factory=dict)


class ReleaseAudioResult(BaseModel):
    scenario_id: str
    seed: str
    evidence_dir: str
    backend_url: str


class ScenarioFile(BaseModel):
    scenarios: list[Scenario]

    @model_validator(mode="after")
    def _validate_unique_scenarios(self) -> "ScenarioFile":
        scenario_ids = [scenario.id for scenario in self.scenarios]
        if len(set(scenario_ids)) != len(scenario_ids):
            raise ValueError("scenario ids must be unique")
        return self


def load_scenarios(path: Path = SCENARIO_PATH) -> list[Scenario]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    try:
        parsed = ScenarioFile.model_validate(raw)
    except ValidationError:
        raise
    return parsed.scenarios


def _cases_for_gate(gate: ScenarioGate) -> list[ScenarioCase]:
    return [
        ScenarioCase(scenario=scenario, seed=seed)
        for scenario in load_scenarios()
        if scenario.gate == gate
        for seed in scenario.seeds
    ]


def load_ci_cases() -> list[ScenarioCase]:
    return _cases_for_gate("ci")


def load_release_audio_cases() -> list[ScenarioCase]:
    return _cases_for_gate("release_audio")


def case_ids(cases: Iterable[ScenarioCase]) -> list[str]:
    return [case.case_id for case in cases]


class IntegrationSetupError(RuntimeError):
    """Raised when an integration test cannot run without explicit setup."""


@contextmanager
def _test_frames_enabled() -> Iterator[None]:
    previous = os.environ.get("VOCALIZE_ENABLE_TEST_FRAMES")
    os.environ["VOCALIZE_ENABLE_TEST_FRAMES"] = "1"
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("VOCALIZE_ENABLE_TEST_FRAMES", None)
        else:
            os.environ["VOCALIZE_ENABLE_TEST_FRAMES"] = previous


async def _generate_deepseek_turns(case: ScenarioCase) -> list[str]:
    api_key = os.getenv("DEEPSEEK_API_KEY")
    if not api_key:
        raise IntegrationSetupError(
            "DEEPSEEK_API_KEY is required when --ai-provider-required is set"
        )

    client = AsyncOpenAI(
        api_key=api_key,
        base_url=os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
        max_retries=0,
    )
    checks = [
        {"id": check.id, "description": check.description}
        for check in case.scenario.checks
    ]
    prompt = {
        "scenario_id": case.scenario_id,
        "seed": case.seed_id,
        "behavior": case.scenario.behavior,
        "task": case.scenario.task,
        "merchant_style": case.seed.merchant_style,
        "merchant_lang": case.scenario.merchant_lang,
        "checks": checks,
        "seed_turn_examples": case.seed.merchant_turns,
    }
    response = await client.chat.completions.create(
        model="deepseek-v4-flash",
        messages=[
            {
                "role": "system",
                "content": (
                    "You role-play the merchant side of a VocalizeAI phone "
                    'scenario. Return JSON only: {"turns":[...]} with '
                    "2-4 short merchant utterances in merchant_lang."
                ),
            },
            {"role": "user", "content": json.dumps(prompt, ensure_ascii=False)},
        ],
        response_format={"type": "json_object"},
    )
    content = response.choices[0].message.content
    if not content:
        raise IntegrationSetupError("DeepSeek returned empty merchant content")
    try:
        payload = json.loads(content)
    except json.JSONDecodeError as exc:
        raise IntegrationSetupError(
            "DeepSeek returned non-JSON merchant content"
        ) from exc
    turns = payload.get("turns")
    if not isinstance(turns, list) or not all(isinstance(turn, str) for turn in turns):
        raise IntegrationSetupError("DeepSeek response did not contain string turns")
    cleaned = [turn.strip() for turn in turns if turn.strip()]
    if not cleaned:
        raise IntegrationSetupError("DeepSeek produced no usable merchant turns")
    return cleaned


def _merchant_turns(
    case: ScenarioCase, *, provider_required: bool
) -> tuple[str, list[str]]:
    if os.getenv("DEEPSEEK_API_KEY"):
        return "deepseek-v4-flash", asyncio.run(_generate_deepseek_turns(case))
    if provider_required:
        raise IntegrationSetupError(
            "DeepSeek merchant provider setup is absent; scripted fallback is disabled"
        )
    return "scripted", list(case.seed.merchant_turns)


class _ScriptedLLM:
    def __init__(self, scripts: list[list[LLMChunk]]) -> None:
        self._scripts = list(scripts)
        self.calls: list[list[ChatMessage]] = []

    async def stream_chat(
        self,
        messages: list[ChatMessage],
        tools: list[ToolDef] | None = None,
    ) -> AsyncIterator[LLMChunk]:
        self.calls.append([ChatMessage(**message.__dict__) for message in messages])
        if not self._scripts:
            raise IntegrationSetupError("scripted LLM exhausted")
        for chunk in self._scripts.pop(0):
            yield chunk


class _ParkedSTT:
    async def stream_transcribe(
        self,
        audio_chunks: AsyncIterator[bytes],
        **_kwargs: Any,
    ) -> AsyncIterator[Transcript]:
        if os.getenv("VOCALIZE_TEST_STT_YIELD_EMPTY") == "1":
            yield Transcript(
                text="",
                is_final=False,
                confidence=0.0,
                start_time=0.0,
                end_time=0.0,
                utterance_id=0,
            )
            return
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            raise


class _NoopTTS:
    output_sample_rate: int = 24000
    output_encoding: AudioEncoding = "pcm_s16le"

    async def stream_synthesize(
        self,
        text_chunks: AsyncIterator[TextChunk],
    ) -> AsyncIterator[bytes]:
        async for _chunk in text_chunks:
            pass
        if os.getenv("VOCALIZE_TEST_TTS_YIELD_EMPTY") == "1":
            yield b""


def _tool_call_chunks(
    idx: int,
    tc_id: str,
    name: str,
    arguments: dict[str, Any],
) -> list[LLMChunk]:
    return [
        ToolCallDelta(
            tool_call_index=idx,
            tool_call_id=tc_id,
            name=name,
            arguments_delta=json.dumps(arguments),
        ),
        FinishChunk(reason="tool_calls"),
    ]


def _text_chunks(text: str) -> list[LLMChunk]:
    return [TextDelta(text=text), FinishChunk(reason="stop")]


def _task_planner_script(case: ScenarioCase) -> list[LLMChunk]:
    return _tool_call_chunks(
        0,
        "call_ai_merchant_task_schema",
        "emit_task_schema",
        {
            "task_category": "ai-merchant-scenario",
            "slots_schema": [
                {
                    "name": "merchant_lang",
                    "description_zh": "商家语言",
                    "description_en": "Merchant language",
                    "criticality": "H",
                    "expected_type": "enum",
                    "enum_values": ["zh", "en"],
                },
                {
                    "name": "task_details",
                    "description_zh": "任务细节",
                    "description_en": "Task details",
                    "criticality": "H",
                    "expected_type": "string",
                },
            ],
            "optional_slots_schema": [],
            "conversation_goals": [case.scenario.task],
            "merchant_etiquette_notes": "Be concise and polite.",
            "readiness_criteria_text": "User explicitly authorizes dialing.",
            "relay_strategy": "Translate exactly when languages differ.",
            "reasoning": "AI-merchant integration scenario schema.",
        },
    )


def _build_runner_scripts(
    case: ScenarioCase,
    merchant_turn_count: int,
) -> list[list[LLMChunk]]:
    return [
        _task_planner_script(case),
        _text_chunks(""),
        *[
            _text_chunks(
                f"Scenario {case.scenario_id} seed {case.seed_id} turn {index + 1} acknowledged."
            )
            for index in range(merchant_turn_count + 2)
        ],
    ]


def _build_harness_app(case: ScenarioCase, expected_turns: int) -> FastAPI:
    app = FastAPI()
    registry = SessionRegistry()
    app.state.registry = registry
    app.state.runners = {}
    scripted_llm = _ScriptedLLM(_build_runner_scripts(case, expected_turns))
    register_session_routes(app, registry=registry)

    def user_pipeline_factory(transport: AudioTransport) -> VoicePipeline:
        return VoicePipeline(
            transport=transport,
            stt=_ParkedSTT(),
            llm=scripted_llm,
            tts=_NoopTTS(),
            system_prompt="",
            default_language=case.scenario.user_lang,
        )

    def merchant_pipeline_factory(transport: AudioTransport) -> VoicePipeline:
        return VoicePipeline(
            transport=transport,
            stt=_ParkedSTT(),
            llm=scripted_llm,
            tts=_NoopTTS(),
            system_prompt="",
            default_language=case.scenario.merchant_lang,
        )

    def runner_factory(session: Session) -> DialogueOrchestratorRunner:
        runner = DialogueOrchestratorRunner(
            session=session,
            user_pipeline_factory=user_pipeline_factory,
            merchant_pipeline_factory=merchant_pipeline_factory,
        )
        app.state.runners[session.session_id] = runner
        return runner

    register_ws_routes(app, registry=registry, runner_factory=runner_factory)
    return app


def _make_bounded_receiver(ws: Any) -> Callable[[float], dict[str, Any] | None]:
    frames: queue.Queue[dict[str, Any] | object] = queue.Queue()
    stop = object()

    def pump() -> None:
        while True:
            try:
                frames.put(ws.receive_json())
            except Exception:
                frames.put(stop)
                return

    threading.Thread(target=pump, daemon=True).start()

    def receive(timeout: float) -> dict[str, Any] | None:
        try:
            item = frames.get(timeout=timeout)
        except queue.Empty:
            return None
        if item is stop:
            return None
        return item  # type: ignore[return-value]

    return receive


def _case_evidence_dir(evidence_dir: Path, case: ScenarioCase) -> Path:
    return evidence_dir / case.scenario_id / case.seed_id


def case_evidence_dir(evidence_dir: Path, case: ScenarioCase) -> Path:
    return _case_evidence_dir(evidence_dir, case)


def _safe_json_write(path: Path, payload: Any) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def _write_text_bypass_evidence(
    *,
    case: ScenarioCase,
    result: TextBypassResult | None,
    evidence_dir: Path,
    error: str | None,
) -> None:
    target = _case_evidence_dir(evidence_dir, case)
    target.mkdir(parents=True, exist_ok=True)
    metadata = {
        "scenario_id": case.scenario_id,
        "seed": case.seed_id,
        "gate": case.scenario.gate,
        "behavior": case.scenario.behavior,
        "user_lang": case.scenario.user_lang,
        "merchant_lang": case.scenario.merchant_lang,
        "provider": result.provider if result is not None else None,
    }
    _safe_json_write(target / "metadata.json", metadata)
    _safe_json_write(
        target / "transcript.json",
        result.transcript if result is not None else [],
    )
    _safe_json_write(
        target / "frame_log.json",
        {
            "scenario_id": case.scenario_id,
            "seed": case.seed_id,
            "client_frames": result.client_frames if result is not None else [],
            "server_frames": result.server_frames if result is not None else [],
        },
    )
    _safe_json_write(
        target / "timing.json",
        result.timing if result is not None else {},
    )
    _safe_json_write(
        target / "judge_ready.json",
        {
            "scenario_id": case.scenario_id,
            "seed": case.seed_id,
            "checks": [check.model_dump(mode="json") for check in case.scenario.checks],
            "transcript": result.transcript if result is not None else [],
            "frame_log": {
                "client_frames": result.client_frames if result is not None else [],
                "server_frames": result.server_frames if result is not None else [],
            },
            "timing": result.timing if result is not None else {},
        },
    )
    if error is not None:
        _safe_json_write(
            target / "error.json",
            {"scenario_id": case.scenario_id, "seed": case.seed_id, "error": error},
        )


def write_text_bypass_evidence(
    *,
    case: ScenarioCase,
    result: TextBypassResult,
    evidence_dir: Path = DEFAULT_EVIDENCE_DIR,
) -> None:
    _write_text_bypass_evidence(
        case=case,
        result=result,
        evidence_dir=evidence_dir,
        error=None,
    )


def text_bypass_judge_evidence(
    case: ScenarioCase, result: TextBypassResult
) -> dict[str, Any]:
    return {
        "scenario_id": case.scenario_id,
        "seed": case.seed_id,
        "gate": case.scenario.gate,
        "behavior": case.scenario.behavior,
        "task": case.scenario.task,
        "checks": [check.model_dump(mode="json") for check in case.scenario.checks],
        "transcript": result.transcript,
        "frame_log": {
            "client_frames": result.client_frames,
            "server_frames": result.server_frames,
        },
        "timing": result.timing,
        "provider": result.provider,
    }


def load_release_audio_judge_evidence(evidence_dir: Path) -> dict[str, Any]:
    def read_json(name: str) -> Any:
        return json.loads((evidence_dir / name).read_text(encoding="utf-8"))

    return {
        "metadata": read_json("metadata.json"),
        "frame_log": read_json("frame_log.json"),
        "stt_transcript": read_json("stt_transcript.json"),
        "tts_events": read_json("tts_events.json"),
        "browser_speaker": read_json("browser_speaker.json"),
        "raw_capture_summary": read_json("raw_capture_summary.json"),
    }


def write_judge_artifact(
    *,
    case: ScenarioCase,
    verdict: BaseModel,
    evidence_dir: Path = DEFAULT_EVIDENCE_DIR,
) -> None:
    target = _case_evidence_dir(evidence_dir, case)
    target.mkdir(parents=True, exist_ok=True)
    _safe_json_write(target / "judge.json", verdict.model_dump(mode="json"))


class _ReleaseAudioSettings(BaseModel):
    backend_url: str
    frontend_url: str = "http://localhost:3000"
    input_label: str
    play_cmd: str


def _release_audio_settings() -> _ReleaseAudioSettings:
    backend_url = (
        os.getenv("VOCALIZE_RELEASE_AUDIO_BACKEND_URL")
        or os.getenv("VITE_VOCALIZE_API_BASE_URL")
    )
    input_label = os.getenv("VOCALIZE_RELEASE_AUDIO_INPUT_LABEL")
    play_cmd = os.getenv("VOCALIZE_RELEASE_AUDIO_PLAY_CMD")
    missing = [
        name
        for name, value in (
            ("VOCALIZE_RELEASE_AUDIO_BACKEND_URL", backend_url),
            ("VOCALIZE_RELEASE_AUDIO_INPUT_LABEL", input_label),
            ("VOCALIZE_RELEASE_AUDIO_PLAY_CMD", play_cmd),
        )
        if not value
    ]
    if missing:
        raise IntegrationSetupError(
            "release-audio setup missing: " + ", ".join(missing)
        )
    return _ReleaseAudioSettings(
        backend_url=backend_url or "",
        frontend_url=os.getenv("VOCALIZE_RELEASE_AUDIO_FRONTEND_URL")
        or "http://localhost:3000",
        input_label=input_label or "",
        play_cmd=play_cmd or "",
    )


def _release_audio_payload(case: ScenarioCase) -> dict[str, Any]:
    return {
        "scenario_id": case.scenario_id,
        "seed": case.seed_id,
        "task": case.scenario.task,
        "behavior": case.scenario.behavior,
        "user_lang": case.scenario.user_lang,
        "merchant_lang": case.scenario.merchant_lang,
        "merchant_turns": list(case.seed.merchant_turns),
        "checks": [check.model_dump(mode="json") for check in case.scenario.checks],
    }


def run_text_bypass_case(
    case: ScenarioCase,
    *,
    evidence_dir: Path = DEFAULT_EVIDENCE_DIR,
    provider_required: bool = False,
) -> TextBypassResult:
    result: TextBypassResult | None = None
    error: str | None = None
    started = time.monotonic()
    try:
        provider, turns = _merchant_turns(case, provider_required=provider_required)
        app = _build_harness_app(case=case, expected_turns=len(turns))
        with _test_frames_enabled(), TestClient(app) as client:
            created = client.post(
                "/api/sessions",
                json={"default_lang": case.scenario.user_lang},
            )
            created.raise_for_status()
            session_id = created.json()["session_id"]
            task_set = client.post(
                f"/api/sessions/{session_id}/task",
                json={"task": case.scenario.task},
            )
            task_set.raise_for_status()
            client_frames: list[dict[str, Any]] = []
            server_frames: list[dict[str, Any]] = []
            with client.websocket_connect(f"/ws/sessions/{session_id}") as ws:
                receive = _make_bounded_receiver(ws)
                dial_now = "dial now" if case.scenario.user_lang == "en" else "现在打吧"
                ws.send_text(
                    json.dumps(
                        {
                            "type": "text_input",
                            "text": dial_now,
                            "lang_hint": case.scenario.user_lang,
                        },
                        ensure_ascii=False,
                    )
                )
                deadline = time.monotonic() + 8.0
                while time.monotonic() < deadline:
                    frame = receive(0.25)
                    if frame is None:
                        continue
                    server_frames.append(frame)
                    if frame.get("type") == "readiness_change" and frame.get("passed"):
                        break
                else:
                    raise IntegrationSetupError(
                        "production runner did not emit readiness_change before text bypass"
                    )

                ws.send_text(
                    json.dumps({"type": "mode_change", "mode": "call_listening"})
                )
                deadline = time.monotonic() + 4.0
                while time.monotonic() < deadline:
                    frame = receive(0.25)
                    if frame is None:
                        continue
                    server_frames.append(frame)
                    if frame == {"type": "mode_ack", "mode": "call_listening"}:
                        break
                else:
                    raise IntegrationSetupError(
                        "production runner did not acknowledge call_listening"
                    )

                for turn in turns:
                    frame = {
                        "type": "merchant_text_inject",
                        "text": turn,
                        "scenario_id": case.scenario_id,
                        "seed": case.seed_id,
                        "lang_hint": case.scenario.merchant_lang,
                    }
                    client_frames.append(frame)
                    ws.send_text(json.dumps(frame, ensure_ascii=False))
                    deadline = time.monotonic() + 4.0
                    observed_merchant = False
                    observed_ai = False
                    while time.monotonic() < deadline:
                        server_frame = receive(0.25)
                        if server_frame is None:
                            continue
                        server_frames.append(server_frame)
                        if (
                            server_frame.get("type") == "transcript_update"
                            and server_frame.get("role") == "merchant_to_ai"
                            and server_frame.get("text") == turn
                        ):
                            observed_merchant = True
                        if (
                            server_frame.get("type") == "transcript_update"
                            and server_frame.get("role") == "ai_to_merchant"
                        ):
                            observed_ai = True
                        if observed_merchant and observed_ai:
                            break
                    if not (observed_merchant and observed_ai):
                        raise IntegrationSetupError(
                            "production runner did not emit merchant and AI transcripts"
                        )
                ws.send_text(json.dumps({"type": "mode_change", "mode": "ended"}))
            runner = app.state.runners[session_id]
            session = app.state.registry.get(session_id)
        result = TextBypassResult(
            scenario_id=case.scenario_id,
            seed=case.seed_id,
            session_id=session_id,
            provider=provider,
            client_frames=client_frames,
            server_frames=server_frames,
            transcript=[
                frame
                for frame in server_frames
                if frame.get("type") == "transcript_update"
            ],
            timing={
                "duration_s": round(time.monotonic() - started, 3),
                "merchant_turn_count": len(turns),
                "runner": runner.__class__.__name__,
                "final_phase": (
                    session.task_state.phase.value
                    if session is not None and session.task_state is not None
                    else None
                ),
            },
        )
        assert sum(
            1
            for frame in result.transcript
            if frame.get("role") == "merchant_to_ai"
        ) == len(turns)
        assert any(frame.get("role") == "ai_to_merchant" for frame in result.transcript)
        assert all(
            frame["scenario_id"] == case.scenario_id for frame in result.client_frames
        )
        assert all(frame["seed"] == case.seed_id for frame in result.client_frames)
        return result
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        if result is not None and (
            os.getenv("VOCALIZE_KEEP_INTEGRATION_EVIDENCE") == "1"
        ):
            _write_text_bypass_evidence(
                case=case,
                result=result,
                evidence_dir=evidence_dir,
                error=None,
            )
        elif error is not None:
            _write_text_bypass_evidence(
                case=case,
                result=result,
                evidence_dir=evidence_dir,
                error=error,
            )


def run_release_audio_case(
    case: ScenarioCase,
    *,
    evidence_dir: Path = DEFAULT_EVIDENCE_DIR,
    headed: bool = True,
) -> ReleaseAudioResult:
    settings = _release_audio_settings()
    target = _case_evidence_dir(evidence_dir, case)
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True)
    env = os.environ.copy()
    env.update(
        {
            "VOCALIZE_RELEASE_AUDIO_CASE_JSON": json.dumps(
                _release_audio_payload(case),
                ensure_ascii=False,
            ),
            "VOCALIZE_RELEASE_AUDIO_EVIDENCE_DIR": str(target),
            "VOCALIZE_RELEASE_AUDIO_BACKEND_URL": settings.backend_url,
            "VOCALIZE_RELEASE_AUDIO_FRONTEND_URL": settings.frontend_url,
            "VOCALIZE_RELEASE_AUDIO_INPUT_LABEL": settings.input_label,
            "VOCALIZE_RELEASE_AUDIO_PLAY_CMD": settings.play_cmd,
        }
    )
    command = [
        "npm",
        "exec",
        "--",
        "playwright",
        "test",
        "-c",
        "playwright.release-audio.config.ts",
        "../tests/integration/release-audio.spec.ts",
        "--project=release-audio",
    ]
    if headed:
        command.append("--headed")
    completed = subprocess.run(
        command,
        cwd=Path("frontend"),
        env=env,
        text=True,
        capture_output=True,
        timeout=300,
        check=False,
    )
    if completed.returncode != 0:
        _safe_json_write(
            target / "error.json",
            {
                "scenario_id": case.scenario_id,
                "seed": case.seed_id,
                "returncode": completed.returncode,
                "stdout": completed.stdout[-4000:],
                "stderr": completed.stderr[-4000:],
            },
        )
        raise IntegrationSetupError(
            "release-audio Playwright probe failed; see evidence error.json"
        )
    required = {
        "metadata.json",
        "frame_log.json",
        "stt_transcript.json",
        "tts_events.json",
        "browser_speaker.json",
        "raw_capture_summary.json",
    }
    missing = [name for name in sorted(required) if not (target / name).is_file()]
    if missing:
        raise IntegrationSetupError(
            "release-audio evidence missing required files: " + ", ".join(missing)
        )
    return ReleaseAudioResult(
        scenario_id=case.scenario_id,
        seed=case.seed_id,
        evidence_dir=str(target),
        backend_url=settings.backend_url,
    )
