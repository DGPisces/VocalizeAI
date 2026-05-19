"""dialogue.preflight tests — outer loop + dial-now phrase override.

Plan 2026-05-04 preflight refactor: ``run_preflight`` now takes a
``UserChannel`` (text or mic-backed) plus an injected ``drive_turn``
async callback that runs one LLM round-trip and mutates TaskState
via tool dispatch side effects. The fakes below mimic that surface
without standing up real services.

v1 Core Engine refactor (2026-05-04): tests use ``TaskState`` with
hand-crafted ``slots_schema`` instead of ``BookingState``. Handlers
now mutate ``state.slots["key"]`` dictionaries instead of named fields.

The preflight outer loop drives the user channel until either:

1. ``state.phase`` is no longer ``COLLECTING`` — i.e., the LLM (via the
   drive_turn callback's tool dispatch) called ``transition_to_calling``
   (→ READY_TO_DIAL) or ``finalize_task`` (→ COMPLETED / FAILED).
   ``assess_readiness_to_dial`` alone does NOT exit — the LLM is
   expected to keep asking M/L slots per preflight_collector spec
   before transitioning;
2. ``detect_dial_now`` matches the latest user text (D-11 voice
   override) — short-circuit BEFORE invoking drive_turn;
3. ``max_turns`` is exceeded → ``DialogueOrchestratorError``;
4. The user channel runs out of input (``EOFError``) → also
   ``DialogueOrchestratorError`` ("user channel exhausted").
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

import pytest

from vocalize.dialogue.preflight import (
    detect_dial_now,
    run_preflight,
)
from vocalize.dialogue.state import (
    DialogueOrchestratorError,
    ReadinessVerdict,
    SlotDef,
    TaskPhase,
    TaskState,
)

# pytest-asyncio is configured with ``asyncio_mode = "auto"`` in pyproject.toml.


_DIAL_NOW_PHRASES = [
    "立刻拨号",
    "现在打吧",
    "马上打",
    "dial now",
    "call now",
    "skip ahead",
]


# ---------------------------------------------------------------------------
# Shared test helper: minimal TaskState with hand-crafted slots_schema
# ---------------------------------------------------------------------------


def _make_test_state(
    session_id: str = "test-session",
    *,
    task_category: str = "restaurant reservation",
    readiness_criteria_text: str = "All H-level slots collected and valid format",
    merchant_lang: str | None = None,
    user_lang: str | None = "zh",
) -> TaskState:
    """Create a minimal TaskState pre-transitioned to COLLECTING for preflight tests.

    The slot schema mirrors the classic 7-slot restaurant booking layout:
    5 H-level (merchant_lang, restaurant, date, time, headcount) +
    1 M (phone) + 1 L (special_requirements). This keeps existing test
    semantics intact while exercising the dynamic schema path.
    """
    state = TaskState(
        session_id=session_id,
        task_category=task_category,
        user_lang=user_lang,
        merchant_lang=merchant_lang,
        slots_schema=[
            SlotDef(
                name="merchant_lang",
                description_zh="商家讲什么语言",
                description_en="What language does the merchant speak",
                criticality="H",
                expected_type="string",
            ),
            SlotDef(
                name="restaurant",
                description_zh="餐厅名称",
                description_en="Restaurant name",
                criticality="H",
                expected_type="string",
            ),
            SlotDef(
                name="date",
                description_zh="用餐日期 (YYYY-MM-DD)",
                description_en="Reservation date (YYYY-MM-DD)",
                criticality="H",
                expected_type="date",
            ),
            SlotDef(
                name="time",
                description_zh="用餐时间 (HH:MM)",
                description_en="Reservation time (HH:MM)",
                criticality="H",
                expected_type="string",
            ),
            SlotDef(
                name="headcount",
                description_zh="用餐人数",
                description_en="Number of diners",
                criticality="H",
                expected_type="number",
            ),
            SlotDef(
                name="phone",
                description_zh="联系电话",
                description_en="Contact phone number",
                criticality="M",
                expected_type="phone",
            ),
            SlotDef(
                name="special_requirements",
                description_zh="特殊要求（过敏、座位偏好等）",
                description_en="Special requirements (allergies, seating, etc.)",
                criticality="L",
                expected_type="string",
            ),
        ],
        readiness_criteria_text=readiness_criteria_text,
    )
    # Transition from DRAFT through TASK_PLANNING to COLLECTING
    # (bypass transition() for test setup simplicity — set phase directly)
    state.phase = TaskPhase.COLLECTING
    return state


# ---------------------------------------------------------------------------
# Lightweight fakes for the post-refactor run_preflight surface.
# ---------------------------------------------------------------------------


class _FakeUserChannel:
    """Scripts user utterances + records AI replies for assertion.

    queued_inputs: tuples of (text, lang) returned by successive
        receive_text() calls. Pop EOFError when exhausted, simulating
        the demo running out of stdin/audio.
    spoken: list of (text, lang) recorded by speak_text() so tests can
        assert what AI said back to the user during preflight.
    """

    def __init__(self, queued_inputs: list[tuple[str, str]]) -> None:
        self._queued = list(queued_inputs)
        self.spoken: list[tuple[str, str]] = []
        self.events: list[dict[str, Any]] = []

    async def receive_text(self) -> tuple[str, str]:
        if not self._queued:
            raise EOFError("FakeUserChannel: queued_inputs exhausted")
        return self._queued.pop(0)

    async def speak_text(self, text: str, *, lang: str) -> None:
        self.spoken.append((text, lang))

    async def request_clarification(self, *_a, **_kw):  # pragma: no cover
        raise NotImplementedError("preflight tests do not exercise clarification")

    async def push_event(self, event: dict[str, Any]) -> None:
        self.events.append(event)


@dataclass
class _DriveTurnRecorder:
    """Records (user_text, lang) calls and runs scripted state mutations.

    handlers: one callable per scripted turn. Each mutates TaskState
    the way the real LLM tool dispatch (assess_readiness_to_dial,
    collect_user_intent) would.
    """

    state: TaskState
    handlers: list[Callable[[TaskState, str, str], None]]
    handled: list[tuple[str, str]] = field(default_factory=list)
    handler_idx: int = 0

    async def __call__(self, user_text: str, language: str) -> None:
        self.handled.append((user_text, language))
        if self.handler_idx < len(self.handlers):
            handler = self.handlers[self.handler_idx]
            self.handler_idx += 1
            handler(self.state, user_text, language)


# ---------------------------------------------------------------------------
# Path A: all 7 slots filled (5H + 1M + 1L). Path B: H-level only + skip
# phone + skip special_req.
#
# B-6 / ROADMAP-#1 / D-09: Path B is the deliberate "user declines M/L
# slots" path. The LLM asks ONCE for each; user declines; the loop must
# NOT re-ask. transition_to_calling fires immediately after.
# ---------------------------------------------------------------------------


def _make_path_a_handlers() -> list[Callable[[TaskState, str, str], None]]:
    """6 collect_user_intent calls (5H + merchant_lang already set by test state)
    + 1 M phone + 1 L special + 1 assess_readiness_to_dial pass + transition."""

    def t0(state: TaskState, text: str, lang: str) -> None:
        # merchant_lang set in test state fixture — skip; fill restaurant
        state.slots["restaurant"] = "海底捞"

    def t1(state: TaskState, text: str, lang: str) -> None:
        state.slots["date"] = "2026-05-04"

    def t2(state: TaskState, text: str, lang: str) -> None:
        state.slots["time"] = "19:00"

    def t3(state: TaskState, text: str, lang: str) -> None:
        state.slots["headcount"] = 4

    def t4(state: TaskState, text: str, lang: str) -> None:
        state.slots["phone"] = "13800000000"

    def t5(state: TaskState, text: str, lang: str) -> None:
        state.slots["special_requirements"] = "no allergies"

    def t6(state: TaskState, text: str, lang: str) -> None:
        # Mimics LLM: assess_readiness_to_dial passes AND
        # transition_to_calling fires.
        state.readiness = ReadinessVerdict(
            missing_critical=[], confidence=0.9, override=False, decided_at=1.0
        )
        state.transition(
            TaskPhase.READY_TO_DIAL, reason="LLM tool transition_to_calling"
        )

    return [t0, t1, t2, t3, t4, t5, t6]


def _make_path_b_handlers() -> list[Callable[[TaskState, str, str], None]]:
    """4 H-level slots (merchant_lang pre-set) + ONE-SHOT phone declined +
    ONE-SHOT special_req declined + readiness pass."""

    def t0(state: TaskState, text: str, lang: str) -> None:
        state.slots["restaurant"] = "海底捞"

    def t1(state: TaskState, text: str, lang: str) -> None:
        state.slots["date"] = "2026-05-04"

    def t2(state: TaskState, text: str, lang: str) -> None:
        state.slots["time"] = "19:00"

    def t3(state: TaskState, text: str, lang: str) -> None:
        state.slots["headcount"] = 4

    def t4(state: TaskState, text: str, lang: str) -> None:
        pass  # phone declined — slot stays unset

    def t5(state: TaskState, text: str, lang: str) -> None:
        pass  # special_requirements declined — slot stays unset

    def t6(state: TaskState, text: str, lang: str) -> None:
        state.readiness = ReadinessVerdict(
            missing_critical=[], confidence=0.85, override=False, decided_at=1.0
        )
        state.transition(
            TaskPhase.READY_TO_DIAL, reason="LLM tool transition_to_calling"
        )

    return [t0, t1, t2, t3, t4, t5, t6]


# ---------------------------------------------------------------------------
# Preflight loop — REQ-dialogue-orchestrator criterion 1 (B-6 dual paths)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path_id, handler_factory, expect_phone_set, expect_special_set",
    [
        ("full_7_slots", _make_path_a_handlers, True, True),
        ("critical_only_skip", _make_path_b_handlers, False, False),
    ],
)
async def test_preflight_minimal_input(
    path_id: str,
    handler_factory: Callable[[], list[Callable[..., None]]],
    expect_phone_set: bool,
    expect_special_set: bool,
) -> None:
    """B-6 dual-path acceptance:

    - Path A ('full_7_slots'): all 5H + 1M + 1L slots filled → READY_TO_DIAL.
    - Path B ('critical_only_skip'): only 5H filled, M/L declined exactly
      once each → READY_TO_DIAL with phone / special_requirements still unset.
    """
    handlers = handler_factory()
    inputs = [
        ("我想订海底捞", "zh"),
        ("明天5月4号", "zh"),
        ("晚上7点", "zh"),
        ("4个人", "zh"),
        ("不用电话", "zh"),
        ("没什么特殊要求", "zh"),
        ("好的就这样", "zh"),
    ]
    state = _make_test_state(merchant_lang="zh")
    user_channel = _FakeUserChannel(queued_inputs=inputs)
    drive = _DriveTurnRecorder(state=state, handlers=handlers)

    verdict = await run_preflight(
        user_channel, state, drive_turn=drive, max_turns=20,
    )

    assert verdict.passed is True
    assert state.readiness is not None and state.readiness.passed is True
    assert state.slots.get("restaurant") == "海底捞"
    assert state.slots.get("date") == "2026-05-04"
    assert state.slots.get("time") == "19:00"
    assert state.slots.get("headcount") == 4
    if expect_phone_set:
        assert state.slots.get("phone") == "13800000000"
    else:
        assert state.slots.get("phone") is None
    if expect_special_set:
        assert state.slots.get("special_requirements") == "no allergies"
    else:
        assert state.slots.get("special_requirements") is None
    assert drive.handler_idx == 7, f"Path {path_id}: handler_idx drift"


async def test_preflight_max_turns_raises() -> None:
    """Feed 21 user turns where readiness is never set; assert
    DialogueOrchestratorError('preflight max_turns exceeded') is raised
    (preflight loop is bounded — RESEARCH "Failure Modes")."""
    state = _make_test_state()
    inputs = [(f"turn {i}", "zh") for i in range(1, 22)]
    user_channel = _FakeUserChannel(queued_inputs=inputs)
    # Empty handlers list → no handler ever runs → state.readiness stays None.
    drive = _DriveTurnRecorder(state=state, handlers=[])

    with pytest.raises(
        DialogueOrchestratorError, match="preflight max_turns exceeded"
    ):
        await run_preflight(
            user_channel, state, drive_turn=drive, max_turns=20,
        )


async def test_preflight_user_channel_exhausted_raises() -> None:
    """When the user channel raises EOFError before readiness passes,
    preflight surfaces it as DialogueOrchestratorError ("user channel
    exhausted") so the caller / orchestrator can transition to FAILED.
    Replaces the pre-refactor 'STT stream ended without readiness' path.
    """
    state = _make_test_state()
    user_channel = _FakeUserChannel(queued_inputs=[])  # immediately EOF
    drive = _DriveTurnRecorder(state=state, handlers=[])

    with pytest.raises(
        DialogueOrchestratorError, match="user channel exhausted"
    ):
        await run_preflight(
            user_channel, state, drive_turn=drive, max_turns=20,
        )


async def test_preflight_empty_text_skipped() -> None:
    """If a UserChannel impl ever returns whitespace-only text (a real
    TextUserChannel would have raised EOFError, but belt-and-braces),
    preflight must skip it without invoking drive_turn."""
    state = _make_test_state()

    def passing(state: TaskState, text: str, lang: str) -> None:
        state.readiness = ReadinessVerdict(
            missing_critical=[], confidence=0.9, decided_at=1.0
        )
        state.transition(
            TaskPhase.READY_TO_DIAL, reason="LLM tool transition_to_calling"
        )

    inputs = [("   ", "zh"), ("订位", "zh")]
    user_channel = _FakeUserChannel(queued_inputs=inputs)
    drive = _DriveTurnRecorder(state=state, handlers=[passing])

    verdict = await run_preflight(
        user_channel, state, drive_turn=drive, max_turns=20,
    )
    assert verdict.passed is True
    assert len(drive.handled) == 1
    assert drive.handled[0][0] == "订位"


async def test_preflight_finalize_task_raises_not_passes() -> None:
    """Pin (Codex P1 2026-05-04): when LLM calls finalize_task during
    preflight (the rare 'user abandoned off the dial path' branch per
    preflight_collector spec), state.phase transitions to FAILED (the only legal
    terminal from COLLECTING per LEGAL_TASK_TRANSITIONS — COMPLETED requires
    going through EXECUTION_ACTIVE). preflight must surface this as
    DialogueOrchestratorError so the orchestrator's failure-handling
    path runs — NOT synthesize a passing readiness and let the
    orchestrator continue into the merchant loop on a terminal state.
    """
    state = _make_test_state()

    def finalize(state: TaskState, text: str, lang: str) -> None:
        # Mimics dispatch of finalize_task(success=False) — direct
        # phase push to FAILED, no readiness mutation.
        state.transition(TaskPhase.FAILED, reason="LLM tool finalize_task")

    user_channel = _FakeUserChannel(queued_inputs=[("我不想订了", "zh")])
    drive = _DriveTurnRecorder(state=state, handlers=[finalize])

    with pytest.raises(
        DialogueOrchestratorError,
        match="preflight terminated via finalize_task",
    ):
        await run_preflight(
            user_channel, state, drive_turn=drive, max_turns=20,
        )

    # state.readiness must NOT have been forged into a passing verdict.
    assert state.readiness is None
    assert state.phase == TaskPhase.FAILED


async def test_preflight_readiness_alone_does_not_exit() -> None:
    """Pin: assess_readiness_to_dial setting state.readiness.passed is
    NOT enough to exit preflight — the LLM must also call
    transition_to_calling (which pushes phase to READY_TO_DIAL). This matches
    the preflight_collector prompt spec: after H-level slots are filled +
    readiness passes, the LLM should still ask M/L slots once each before
    transitioning. Premature exit (the bug observed in manual demo
    verification 2026-05-04 18:58:24) caused the demo to enter the merchant
    loop while the user was still answering phone.
    """
    state = _make_test_state()
    inputs = [
        ("turn 1: H slots filled", "zh"),  # readiness passes here
        ("13800138000", "zh"),              # phone (M-level)
        ("no allergies", "zh"),             # special_requirements (L-level)
        ("yes call them", "zh"),            # transition_to_calling fires
    ]

    def turn1_readiness_passes(state: TaskState, text: str, lang: str) -> None:
        # Mimics LLM filling 5 H-level slots + assess_readiness_to_dial.
        # phase stays COLLECTING — preflight must NOT exit yet.
        state.slots["restaurant"] = "海底捞"
        state.slots["date"] = "2026-05-04"
        state.slots["time"] = "19:00"
        state.slots["headcount"] = 4
        state.readiness = ReadinessVerdict(
            missing_critical=[], confidence=0.95, decided_at=1.0
        )

    def turn2_phone(state: TaskState, text: str, lang: str) -> None:
        state.slots["phone"] = "13800138000"

    def turn3_special(state: TaskState, text: str, lang: str) -> None:
        state.slots["special_requirements"] = "no allergies"

    def turn4_transition(state: TaskState, text: str, lang: str) -> None:
        state.transition(
            TaskPhase.READY_TO_DIAL, reason="LLM tool transition_to_calling"
        )

    user_channel = _FakeUserChannel(queued_inputs=inputs)
    drive = _DriveTurnRecorder(
        state=state,
        handlers=[
            turn1_readiness_passes,
            turn2_phone,
            turn3_special,
            turn4_transition,
        ],
    )

    verdict = await run_preflight(
        user_channel, state, drive_turn=drive, max_turns=20,
    )

    assert verdict.passed is True
    assert state.phase == TaskPhase.READY_TO_DIAL
    assert state.slots.get("phone") == "13800138000"
    assert state.slots.get("special_requirements") == "no allergies"
    # All 4 turns invoked — readiness passing on turn 1 did NOT short-circuit.
    assert len(drive.handled) == 4


# ---------------------------------------------------------------------------
# Dial-now phrase short-circuit — D-11
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("phrase", _DIAL_NOW_PHRASES)
async def test_dial_now_phrases(phrase: str) -> None:
    """Each of the 6 D-11 phrases triggers state.readiness.override=True
    BEFORE drive_turn fires, and transitions COLLECTING → READY_TO_DIAL."""
    state = _make_test_state()
    user_channel = _FakeUserChannel(queued_inputs=[(phrase, "zh")])
    drive = _DriveTurnRecorder(state=state, handlers=[])

    verdict = await run_preflight(
        user_channel, state, drive_turn=drive, max_turns=20,
    )

    assert verdict.passed is True
    assert verdict.override is True
    assert state.readiness is not None
    assert state.readiness.override is True
    assert state.phase == TaskPhase.READY_TO_DIAL
    # drive_turn must NOT have been invoked — short-circuit fires first.
    assert drive.handled == []
    # Audit log records the transition with the phrase as evidence.
    assert state.audit_log, "missing audit entry for dial-now transition"
    last = state.audit_log[-1]
    assert last.from_phase == TaskPhase.COLLECTING
    assert last.to_phase == TaskPhase.READY_TO_DIAL
    assert "dial-now" in last.reason
    assert last.evidence.get("phrase") == phrase


def test_dial_now_phrases_normalize_case_and_whitespace() -> None:
    """'  Dial   Now  ' and 'DIAL NOW!' both normalize to override=True.

    Tested via the standalone matcher (purer — no async loop overhead).
    """
    assert detect_dial_now("  Dial   Now  ") is True
    assert detect_dial_now("DIAL NOW!") is True
    assert detect_dial_now("DiAl NoW") is True


def test_dial_now_window_anchored() -> None:
    """recent_window_chars=80 anchors to END of transcript; a 'dial now'
    buried in chars 0-9 of a long transcript must NOT trigger override.
    """
    haystack = "dial now " + ("x" * 200)
    assert detect_dial_now(haystack, recent_window_chars=80) is False


# ---------------------------------------------------------------------------
# detect_dial_now standalone unit tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("phrase", _DIAL_NOW_PHRASES)
def test_detect_dial_now_each_phrase(phrase: str) -> None:
    """Each of the 6 D-11 phrases is detected verbatim."""
    assert detect_dial_now(phrase) is True


@pytest.mark.parametrize(
    "negative",
    [
        "I want to order food",
        "you can dial later",
        "请稍后再打",
        "skip the appetizer",
        "",
        "   ",
    ],
)
def test_detect_dial_now_negative_cases(negative: str) -> None:
    """Negative cases must NOT trigger override."""
    assert detect_dial_now(negative) is False


# Prompt rendering is owned by ``orchestrator._render_prompt`` (single
# source of truth); behavior is verified by snapshot tests in
# ``tests/test_prompt_rendering.py``.
