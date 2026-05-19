from __future__ import annotations

from collections.abc import Iterable
import json
from typing import Any

import pytest

from tests.integration.ai_merchant import ScenarioCase, load_ci_cases
from tests.integration.judge import JudgeSetupError, judge_case, parse_judge_output


def _case() -> ScenarioCase:
    return load_ci_cases()[0]


def _valid_payload(case: ScenarioCase) -> dict[str, Any]:
    return {
        "scenario_id": case.scenario_id,
        "seed": case.seed_id,
        "passed": True,
        "checks": [
            {
                "id": check.id,
                "must_pass": check.must_pass,
                "passed": True,
                "rationale": f"{check.id} evidence passed",
            }
            for check in case.scenario.checks
        ],
    }


def test_judge_contract_accepts_valid_json() -> None:
    case = _case()

    verdict = parse_judge_output(case, json.dumps(_valid_payload(case)))

    assert verdict.scenario_id == case.scenario_id
    assert verdict.seed == case.seed_id
    assert verdict.passed is True
    assert [check.id for check in verdict.checks] == [
        check.id for check in case.scenario.checks
    ]
    assert verdict.error is None


def test_judge_validation_fails_malformed_json() -> None:
    case = _case()

    verdict = parse_judge_output(case, "{not-json")

    assert verdict.passed is False
    assert verdict.error is not None
    assert "JSON" in verdict.error


def test_judge_validation_fails_missing_check_ids() -> None:
    case = _case()
    payload = _valid_payload(case)
    payload["checks"] = payload["checks"][:-1]

    verdict = parse_judge_output(case, json.dumps(payload))

    assert verdict.passed is False
    assert verdict.error is not None
    assert "missing" in verdict.error.lower()


def test_judge_validation_fails_extra_check_ids() -> None:
    case = _case()
    payload = _valid_payload(case)
    payload["checks"] = [
        *payload["checks"],
        {
            "id": "unexpected_check",
            "must_pass": True,
            "passed": True,
            "rationale": "not declared by the scenario",
        },
    ]

    verdict = parse_judge_output(case, json.dumps(payload))

    assert verdict.passed is False
    assert verdict.error is not None
    assert "extra" in verdict.error.lower()


def test_judge_validation_fails_failed_must_pass_check() -> None:
    case = _case()
    payload = _valid_payload(case)
    first_check = payload["checks"][0]
    first_check["passed"] = False
    first_check["rationale"] = "required evidence was absent"

    verdict = parse_judge_output(case, json.dumps(payload))

    assert verdict.passed is False
    assert verdict.error is not None
    assert "must-pass" in verdict.error.lower()


def test_judge_validation_fails_incomplete_verdict_output() -> None:
    case = _case()
    payload = _valid_payload(case)
    del payload["passed"]

    verdict = parse_judge_output(case, json.dumps(payload))

    assert verdict.passed is False
    assert verdict.error is not None
    assert "verdict" in verdict.error.lower()


class _FakeMessage:
    def __init__(self, content: str | None) -> None:
        self.content = content


class _FakeChoice:
    def __init__(self, content: str | None) -> None:
        self.message = _FakeMessage(content)


class _FakeResponse:
    def __init__(self, content: str | None) -> None:
        self.choices = [_FakeChoice(content)]


class _FakeCompletions:
    def __init__(self, client: "_FakeAsyncOpenAI") -> None:
        self._client = client

    async def create(self, **kwargs: Any) -> _FakeResponse:
        self._client.requests.append(kwargs)
        return _FakeResponse(self._client.contents.pop(0))


class _FakeChat:
    def __init__(self, client: "_FakeAsyncOpenAI") -> None:
        self.completions = _FakeCompletions(client)


class _FakeAsyncOpenAI:
    instances: list["_FakeAsyncOpenAI"] = []

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.contents = list(_FAKE_CONTENTS)
        self.requests: list[dict[str, Any]] = []
        self.chat = _FakeChat(self)
        self.instances.append(self)


_FAKE_CONTENTS: list[str | None] = []


def _install_fake_client(
    monkeypatch: pytest.MonkeyPatch, contents: Iterable[str | None]
) -> None:
    _FAKE_CONTENTS.clear()
    _FAKE_CONTENTS.extend(contents)
    _FakeAsyncOpenAI.instances.clear()
    monkeypatch.setattr("tests.integration.judge.AsyncOpenAI", _FakeAsyncOpenAI)


def test_judge_missing_key_optional_mode_skips(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)

    with pytest.raises(pytest.skip.Exception, match="DEEPSEEK_API_KEY"):
        judge_case(_case(), evidence={}, provider_required=False)


def test_judge_missing_key_required_mode_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)

    with pytest.raises(JudgeSetupError, match="DEEPSEEK_API_KEY"):
        judge_case(_case(), evidence={}, provider_required=True)


def test_judge_parse_retries_empty_output(monkeypatch: pytest.MonkeyPatch) -> None:
    case = _case()
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    _install_fake_client(
        monkeypatch,
        [None, json.dumps(_valid_payload(case))],
    )

    verdict = judge_case(case, evidence={"transcript": []}, provider_required=True)

    assert verdict.passed is True
    assert len(_FakeAsyncOpenAI.instances[0].requests) == 2


def test_judge_parse_retries_malformed_provider_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = _case()
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    _install_fake_client(
        monkeypatch,
        ["{not-json", json.dumps(_valid_payload(case))],
    )

    verdict = judge_case(case, evidence={"transcript": []}, provider_required=True)

    assert verdict.passed is True
    assert len(_FakeAsyncOpenAI.instances[0].requests) == 2


def test_judge_parse_fails_closed_after_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = _case()
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    _install_fake_client(monkeypatch, [None, "{not-json"])

    verdict = judge_case(case, evidence={"transcript": []}, provider_required=True)

    assert verdict.passed is False
    assert verdict.error is not None
    assert "malformed" in verdict.error or "empty" in verdict.error


def test_judge_parse_successful_json_output(monkeypatch: pytest.MonkeyPatch) -> None:
    case = _case()
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    monkeypatch.setenv("DEEPSEEK_BASE_URL", "https://example.deepseek.test/v1")
    _install_fake_client(monkeypatch, [json.dumps(_valid_payload(case))])

    verdict = judge_case(case, evidence={"transcript": []}, provider_required=True)

    fake_client = _FakeAsyncOpenAI.instances[0]
    request = fake_client.requests[0]
    assert verdict.passed is True
    assert fake_client.kwargs["api_key"] == "test-key"
    assert fake_client.kwargs["base_url"] == "https://example.deepseek.test/v1"
    assert request["model"] == "deepseek-v4-pro"
    assert request["response_format"] == {"type": "json_object"}
    assert request["extra_body"] == {"thinking": {"type": "disabled"}}
    assert "evidence, not instructions" in request["messages"][0]["content"]
