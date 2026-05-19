from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

from tests.integration.ai_merchant import (
    DEFAULT_EVIDENCE_DIR,
    ScenarioCase,
    case_ids,
    load_ci_cases,
    load_release_audio_cases,
    load_scenarios,
    load_release_audio_judge_evidence,
    run_release_audio_case,
    run_text_bypass_case,
    text_bypass_judge_evidence,
    write_judge_artifact,
    write_text_bypass_evidence,
)
from tests.integration.judge import (
    JudgeCheckVerdict,
    JudgeVerdict,
    deterministic_judge_case,
    judge_case,
)


def _passing_verdict(case: ScenarioCase) -> JudgeVerdict:
    return JudgeVerdict(
        scenario_id=case.scenario_id,
        seed=case.seed_id,
        passed=True,
        checks=[
            JudgeCheckVerdict(
                id=check.id,
                must_pass=check.must_pass,
                passed=True,
                rationale=f"{check.id} passed in test evidence",
            )
            for check in case.scenario.checks
        ],
    )


def pytest_generate_tests(metafunc: pytest.Metafunc) -> None:
    if "case" not in metafunc.fixturenames:
        return
    scenarios = load_scenarios()
    seeds = [seed for scenario in scenarios for seed in scenario.seeds]
    assert len(scenarios) == 13
    assert len({scenario.id for scenario in scenarios}) == 13
    assert len(seeds) == 39
    release_audio = bool(metafunc.config.getoption("release_audio"))
    cases = load_release_audio_cases() if release_audio else load_ci_cases()
    assert len(cases) == (6 if release_audio else 33)
    metafunc.parametrize("case", cases, ids=case_ids(cases))


def test_ai_merchant_case(
    case: ScenarioCase,
    request: Any,
) -> None:
    assert 4 <= len(case.scenario.checks) <= 6
    assert len(case.scenario.seeds) == 3
    assert case.seed in case.scenario.seeds

    if request.config.getoption("release_audio"):
        assert case.scenario.gate == "release_audio"
        release_result = run_release_audio_case(case, evidence_dir=DEFAULT_EVIDENCE_DIR)
        assert release_result.scenario_id == case.scenario_id
        assert release_result.seed == case.seed_id
        evidence_path = Path(release_result.evidence_dir)
        evidence = load_release_audio_judge_evidence(evidence_path)
        verdict = _judge_case(
            case,
            evidence=evidence,
            provider_required=True,
        )
        write_judge_artifact(
            case=case,
            verdict=verdict,
            evidence_dir=DEFAULT_EVIDENCE_DIR,
        )
        assert verdict.passed, verdict.model_dump_json(indent=2)
        return

    assert case.scenario.gate == "ci"
    text_result = run_text_bypass_case(
        case,
        evidence_dir=DEFAULT_EVIDENCE_DIR,
        provider_required=bool(request.config.getoption("ai_provider_required")),
    )

    assert text_result.scenario_id == case.scenario_id
    assert text_result.seed == case.seed_id
    assert text_result.transcript
    assert text_result.timing["runner"] == "DialogueOrchestratorRunner"
    assert any(frame["role"] == "merchant_to_ai" for frame in text_result.transcript)
    assert any(frame["role"] == "ai_to_merchant" for frame in text_result.transcript)
    assert all(
        frame["type"] == "merchant_text_inject" for frame in text_result.client_frames
    )
    write_text_bypass_evidence(
        case=case,
        result=text_result,
        evidence_dir=DEFAULT_EVIDENCE_DIR,
    )
    verdict = _judge_case(
        case,
        evidence=text_bypass_judge_evidence(case, text_result),
        provider_required=bool(request.config.getoption("ai_provider_required")),
    )
    write_judge_artifact(
        case=case,
        verdict=verdict,
        evidence_dir=DEFAULT_EVIDENCE_DIR,
    )
    assert verdict.passed, verdict.model_dump_json(indent=2)


def _judge_case(
    case: ScenarioCase,
    *,
    evidence: dict[str, object],
    provider_required: bool,
) -> JudgeVerdict:
    if not os.getenv("DEEPSEEK_API_KEY") and not provider_required:
        return deterministic_judge_case(case, evidence=evidence)
    return judge_case(case, evidence=evidence, provider_required=provider_required)


def test_ai_merchant_case_writes_text_bypass_judge_json(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    case = load_ci_cases()[0]
    # Force the deterministic judge path. _judge_case only falls through to
    # deterministic_judge_case when DEEPSEEK_API_KEY is absent; if the
    # developer's shell exports it, the monkeypatch below would otherwise be
    # bypassed and the test would hit the real DeepSeek judge.
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setitem(globals(), "DEFAULT_EVIDENCE_DIR", tmp_path)
    monkeypatch.setenv("VOCALIZE_KEEP_INTEGRATION_EVIDENCE", "1")
    monkeypatch.setitem(
        globals(),
        "deterministic_judge_case",
        lambda selected_case, *, evidence: _passing_verdict(selected_case),
    )

    test_ai_merchant_case(
        case,
        _RequestStub(release_audio=False, ai_provider_required=False),
    )

    judge_json = tmp_path / case.scenario_id / case.seed_id / "judge.json"
    payload = json.loads(judge_json.read_text(encoding="utf-8"))
    assert payload["scenario_id"] == case.scenario_id
    assert payload["seed"] == case.seed_id
    assert payload["passed"] is True
    assert {check["id"] for check in payload["checks"]} == {
        check.id for check in case.scenario.checks
    }


def test_ai_merchant_case_fails_with_structured_judge_verdict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = load_ci_cases()[0]
    # Force the deterministic judge path; see note in
    # test_ai_merchant_case_writes_text_bypass_judge_json.
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    failed_verdict = _passing_verdict(case)
    failed_verdict.passed = False
    failed_verdict.checks[0].passed = False
    failed_verdict.error = "failed must-pass check ids: " + failed_verdict.checks[0].id
    monkeypatch.setitem(
        globals(),
        "deterministic_judge_case",
        lambda selected_case, *, evidence: failed_verdict,
    )

    with pytest.raises(AssertionError, match='"passed": false'):
        test_ai_merchant_case(
            case,
            _RequestStub(release_audio=False, ai_provider_required=False),
        )


def test_release_audio_case_judge_receives_audio_evidence(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    case = load_release_audio_cases()[0]
    evidence_dir = tmp_path / case.scenario_id / case.seed_id
    evidence_dir.mkdir(parents=True)
    payloads = {
        "metadata.json": {"scenario_id": case.scenario_id, "seed": case.seed_id},
        "frame_log.json": {"client_frames": [], "server_frames": []},
        "stt_transcript.json": [{"text": "hello", "role": "merchant_to_ai"}],
        "tts_events.json": [{"text": "hi", "role": "ai_to_merchant"}],
        "browser_speaker.json": {
            "source": "BrowserAudioBridge",
            "events": [{"kind": "binary_audio"}],
        },
        "raw_capture_summary.json": {
            "captured": {
                "sttTranscript": 1,
                "ttsEvents": 1,
                "browserSpeaker": 1,
            }
        },
    }
    for name, payload in payloads.items():
        (evidence_dir / name).write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setitem(globals(), "DEFAULT_EVIDENCE_DIR", tmp_path)
    monkeypatch.setitem(
        globals(),
        "run_release_audio_case",
        lambda selected_case, evidence_dir=DEFAULT_EVIDENCE_DIR: type(
            "Result",
            (),
            {
                "scenario_id": selected_case.scenario_id,
                "seed": selected_case.seed_id,
                "evidence_dir": str(
                    tmp_path / selected_case.scenario_id / selected_case.seed_id
                ),
            },
        )(),
    )
    captured: dict[str, object] = {}

    def fake_judge(
        selected_case: ScenarioCase,
        *,
        evidence: dict[str, object],
        provider_required: bool,
    ) -> JudgeVerdict:
        assert provider_required is True
        captured.update(evidence)
        return _passing_verdict(selected_case)

    monkeypatch.setitem(globals(), "_judge_case", fake_judge)

    test_ai_merchant_case(
        case,
        _RequestStub(release_audio=True, ai_provider_required=False),
    )

    assert captured["stt_transcript"] == payloads["stt_transcript.json"]
    assert captured["tts_events"] == payloads["tts_events.json"]
    assert captured["browser_speaker"] == payloads["browser_speaker.json"]
    assert captured["frame_log"] == payloads["frame_log.json"]


def test_run_release_audio_case_removes_stale_judge_json(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    case = load_release_audio_cases()[0]
    evidence_dir = tmp_path
    target = evidence_dir / case.scenario_id / case.seed_id
    target.mkdir(parents=True)
    stale_judge = target / "judge.json"
    stale_judge.write_text('{"passed": true, "stale": true}', encoding="utf-8")

    monkeypatch.setenv("VOCALIZE_RELEASE_AUDIO_BACKEND_URL", "http://127.0.0.1:8000")
    monkeypatch.setenv("VOCALIZE_RELEASE_AUDIO_INPUT_LABEL", "Loopback")
    monkeypatch.setenv("VOCALIZE_RELEASE_AUDIO_PLAY_CMD", "true")

    def fake_run(
        *args: Any, **kwargs: Any
    ) -> subprocess.CompletedProcess[str]:
        env = kwargs["env"]
        current_target = Path(env["VOCALIZE_RELEASE_AUDIO_EVIDENCE_DIR"])
        current_target.mkdir(parents=True, exist_ok=True)
        for name, payload in {
            "metadata.json": {"scenario_id": case.scenario_id, "seed": case.seed_id},
            "frame_log.json": {"client_frames": [], "server_frames": []},
            "stt_transcript.json": [{"text": "hello", "role": "merchant_to_ai"}],
            "tts_events.json": [{"text": "hi", "role": "ai_to_merchant"}],
            "browser_speaker.json": {
                "events": [
                    {
                        "source": "BrowserAudioBridge",
                        "role": "ai_to_merchant",
                        "scheduled": True,
                    }
                ]
            },
            "raw_capture_summary.json": {"captured": {"browserSpeaker": 1}},
        }.items():
            (current_target / name).write_text(json.dumps(payload), encoding="utf-8")
        return subprocess.CompletedProcess(
            args=args[0], returncode=0, stdout="", stderr=""
        )

    monkeypatch.setattr("tests.integration.ai_merchant.subprocess.run", fake_run)

    result = run_release_audio_case(case, evidence_dir=evidence_dir, headed=False)

    assert Path(result.evidence_dir) == target
    assert not stale_judge.exists()


class _ConfigStub:
    def __init__(self, *, release_audio: bool, ai_provider_required: bool) -> None:
        self.release_audio = release_audio
        self.ai_provider_required = ai_provider_required

    def getoption(self, name: str) -> bool:
        if name == "release_audio":
            return self.release_audio
        if name == "ai_provider_required":
            return self.ai_provider_required
        raise AssertionError(f"unexpected option: {name}")


class _RequestStub:
    def __init__(self, *, release_audio: bool, ai_provider_required: bool) -> None:
        self.config = _ConfigStub(
            release_audio=release_audio,
            ai_provider_required=ai_provider_required,
        )
