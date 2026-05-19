"""dialogue.state — task/booking state types, phase machines, and schema checks.

Pure data plus transition rules for the dialogue layer; performs no I/O and
makes no LLM calls.

This module exposes two parallel families of types:

1. Legacy booking-specific types (kept for backwards compatibility but no
   longer the primary type):
   - ``BookingState`` holds 6 fixed booking slots plus phase / audit log /
     pending clarifications / merchant_held / readiness.
   - ``BookingPhase`` and ``LEGAL_TRANSITIONS`` define the 7-phase machine.
   - ``_schema_check()`` implements D-10 stage 1 validation against the
     hardcoded booking schema.

2. v1 universal task types (the new primary model):
   - ``TaskState`` carries a dynamic slot schema produced at runtime by the
     Layer 1 task planner; it is task-category-agnostic.
   - ``TaskPhase`` and ``LEGAL_TASK_TRANSITIONS`` define a generic phase
     machine that adds DRAFT / TASK_PLANNING up front and renames IN_CALL to
     EXECUTION_ACTIVE so v1.x in-person / accessibility flows can reuse it.

Shared semantics:
- ``transition()`` is the only entry point that mutates ``phase``; illegal
  transitions raise ``DialogueOrchestratorError`` and a legal transition
  appends an audit entry.
- ``ReadinessVerdict.passed`` = ``override OR (not missing_critical AND
  confidence >= 0.7)`` — the merged D-10 / D-11 readiness rule. Stage 2
  (LLM self-assessed confidence) is filled by ``assess_readiness_to_dial``
  in ``tools.py``; D-11 is the dial-now override.

Design notes:
- A hand-rolled enum plus a ``LEGAL_TRANSITIONS`` dict is preferred over a
  general state-machine library: a handful of states and edges plus one
  ``transition()`` method does not amortize the abstraction overhead, and a
  plain audit-log append is easier to inspect in pdb than callback hooks.
"""
from __future__ import annotations

import datetime as _dt
import re
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel


class DialogueOrchestratorError(RuntimeError):
    """Orchestration-layer error: illegal phase transition, prompt-load failure,
    invalid tool-dispatch branch, etc."""


class BookingPhase(Enum):
    """The 7 lifecycle phases of the legacy 3-party booking dialogue.

    - ``COLLECTING``: preflight phase; orchestrator collects slots on the user channel.
    - ``READY_TO_DIAL``: preflight readiness passed; awaiting user dial confirmation.
    - ``DIALING``: dialing / connecting the merchant channel.
    - ``IN_CALL``: call in progress; orchestrator relays between the two channels.
    - ``NEEDS_CLARIFICATION``: merchant asked something preflight did not cover;
      orchestrator holds the merchant and routes via ``UserChannel.request_clarification``.
    - ``COMPLETED``: terminal — booking succeeded or user ended the session.
    - ``FAILED``: terminal — dial failed, merchant declined, or orchestrator error.
    """

    COLLECTING = "collecting"
    READY_TO_DIAL = "ready_to_dial"
    DIALING = "dialing"
    IN_CALL = "in_call"
    NEEDS_CLARIFICATION = "needs_clarification"
    COMPLETED = "completed"
    FAILED = "failed"


# Legal-transition table: ``LEGAL_TRANSITIONS[from] = {to1, to2, ...}``.
# Terminal phases (``COMPLETED`` / ``FAILED``) map to empty sets — any transition
# out of a terminal phase is illegal.
# READY_TO_DIAL → COLLECTING is a legal back-edge (user changes mind or edits
# a slot after readiness was reached).
LEGAL_TRANSITIONS: dict[BookingPhase, set[BookingPhase]] = {
    BookingPhase.COLLECTING: {BookingPhase.READY_TO_DIAL, BookingPhase.FAILED},
    BookingPhase.READY_TO_DIAL: {
        BookingPhase.DIALING,
        BookingPhase.COLLECTING,
        BookingPhase.FAILED,
    },
    BookingPhase.DIALING: {BookingPhase.IN_CALL, BookingPhase.FAILED},
    BookingPhase.IN_CALL: {
        BookingPhase.NEEDS_CLARIFICATION,
        BookingPhase.COMPLETED,
        BookingPhase.FAILED,
    },
    BookingPhase.NEEDS_CLARIFICATION: {BookingPhase.IN_CALL, BookingPhase.FAILED},
    BookingPhase.COMPLETED: set(),  # terminal
    BookingPhase.FAILED: set(),  # terminal
}


# Schema regexes for D-10 stage 1. ``date`` is shape-checked by regex then
# validated for calendar correctness via ``datetime.date.fromisoformat``
# (e.g. "2026-13-99" matches the regex but is not a real date). ``time`` is
# shape-locked to two-digit HH:MM, then hour ∈ [0, 23] and minute ∈ [0, 59]
# are checked.
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TIME_RE = re.compile(r"^\d{2}:\d{2}$")


@dataclass
class BookingAuditEntry:
    """Immutable audit record for one phase transition.

    ``timestamp`` uses ``time.monotonic()`` rather than ``time.time()``: the
    audit is about relative ordering, not wall-clock time, and monotonic is
    immune to system-clock adjustments. ``evidence`` is ``dict[str, Any]``
    holding the transition context (readiness verdict, tool return payload,
    raw user utterance, etc.); not frozen — callers are expected to pass a
    semantically immutable dict.
    """

    timestamp: float
    from_phase: BookingPhase
    to_phase: BookingPhase
    reason: str
    evidence: dict[str, Any]


@dataclass
class ClarificationItem:
    """One mid-call clarification record (D-09).

    ``field`` is the slot name being asked about ("phone",
    "special_requirements", or any ad-hoc field). ``answer is None`` means
    the user has not replied yet (the orchestrator is currently holding the
    merchant and waiting for the user). ``ts`` uses ``time.monotonic()`` and
    drives the clarification-timeout check.
    """

    field: str
    question: str
    answer: str | None
    ts: float


@dataclass
class ReadinessVerdict:
    """Preflight readiness verdict (D-10 + D-11).

    - ``missing_critical``: slot names flagged missing by the schema stage;
      an empty list means stage 1 passed.
    - ``confidence``: LLM-self-assessed score in [0, 1] from
      ``assess_readiness_to_dial`` (stage 2).
    - ``override``: D-11 dial-now phrase matched — skip stage 2 and force pass.
    - ``decided_at``: ``time.monotonic()``; used to diagnose ordering of
      readiness decisions.
    - ``passed``: merged rule —
      ``override OR (not missing_critical AND confidence >= 0.7)``.
    """

    missing_critical: list[str]
    confidence: float
    override: bool = False
    decided_at: float = 0.0

    @property
    def passed(self) -> bool:
        return self.override or (not self.missing_critical and self.confidence >= 0.7)


@dataclass
class BookingState:
    """Legacy 3-party booking state — kept for backwards compatibility but no
    longer the primary type. New code should use ``TaskState`` instead.

    Design principle (D-14):
    - This dataclass is the only data path between the user and merchant
      channels. The two channels' ``ChatMessage`` lists are fully isolated;
      neither side may quote the other's transcript. Cross-channel facts
      must flow through these fields.

    Field groups:
    - 6 booking slots (D-09): critical = restaurant / date / time / headcount;
      nice-to-have = phone / special_requirements.
    - Language fields ``user_lang`` / ``merchant_lang`` (D-09); filled by
      ``language.detect_user_lang`` or explicit slot collection.
    - State machine: ``phase`` + ``audit_log``.
    - Mid-call protocol: ``pending_clarifications`` queue + ``merchant_held`` flag.
    - ``readiness``: preflight verdict; read by DialogueOrchestrator at transition time.
    """

    # 6 fixed booking slots
    restaurant_name: str | None = None
    date: str | None = None  # ISO YYYY-MM-DD
    time: str | None = None  # HH:MM
    headcount: int | None = None
    phone: str | None = None
    special_requirements: str | None = None
    # Language (D-09)
    user_lang: str | None = None
    merchant_lang: str | None = None
    # State machine
    phase: BookingPhase = BookingPhase.COLLECTING
    audit_log: list[BookingAuditEntry] = field(default_factory=list)
    # Mid-call protocol
    pending_clarifications: list[ClarificationItem] = field(default_factory=list)
    merchant_held: bool = False
    readiness: ReadinessVerdict | None = None

    def transition(
        self,
        new: BookingPhase,
        *,
        reason: str,
        evidence: dict[str, Any] | None = None,
    ) -> None:
        """Legal: mutate phase and append audit. Illegal: raise DialogueOrchestratorError.

        Key invariant: on a failed transition neither ``self.phase`` nor
        ``self.audit_log`` may be mutated — atomicity. The implementation
        validates first and writes second, which trivially satisfies this.
        """
        if new not in LEGAL_TRANSITIONS[self.phase]:
            raise DialogueOrchestratorError(
                f"illegal transition {self.phase.value} → {new.value}: {reason}"
            )
        self.audit_log.append(
            BookingAuditEntry(
                timestamp=time.monotonic(),
                from_phase=self.phase,
                to_phase=new,
                reason=reason,
                evidence=evidence or {},
            )
        )
        self.phase = new


def _schema_check(state: BookingState) -> list[str]:
    """D-10 stage 1: pure schema validation. Returns the names of missing or
    invalid critical slots, in field order so test assertions stay stable.

    Checks:
    - ``restaurant_name``: None or empty string → "restaurant_name".
    - ``date``: does not match ``^\\d{4}-\\d{2}-\\d{2}$``, or
      ``date.fromisoformat`` raises ValueError (e.g. "2026-13-99") → "date".
    - ``time``: does not match ``^\\d{2}:\\d{2}$``, or hour ∉ [0, 23] /
      minute ∉ [0, 59] → "time".
    - ``headcount``: None / not an int / int < 1 → "headcount".
      (Note: ``bool`` is a subclass of ``int`` and is explicitly rejected.)

    ``phone`` / ``special_requirements`` are nice-to-have (D-09) and are not
    validated here.
    """
    missing: list[str] = []

    if not state.restaurant_name:
        missing.append("restaurant_name")

    date_val = state.date
    if not isinstance(date_val, str) or not _DATE_RE.match(date_val):
        missing.append("date")
    else:
        try:
            _dt.date.fromisoformat(date_val)
        except ValueError:
            missing.append("date")

    time_val = state.time
    if not isinstance(time_val, str) or not _TIME_RE.match(time_val):
        missing.append("time")
    else:
        hh, mm = time_val.split(":")
        h, m = int(hh), int(mm)
        if not (0 <= h <= 23 and 0 <= m <= 59):
            missing.append("time")

    headcount = state.headcount
    if (
        headcount is None
        or isinstance(headcount, bool)
        or not isinstance(headcount, int)
        or headcount < 1
    ):
        missing.append("headcount")

    return missing


# ---------------------------------------------------------------------------
# v1 Core Engine: Layer 1 (slot/phase) types — additive alongside existing
# BookingState / BookingPhase / LEGAL_TRANSITIONS.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SlotDef:
    """One slot in a runtime-generated task schema (Layer 1 output).

    ``criticality``: H = readiness-blocking; M = nice-to-have but worth asking;
    L = optional, only collect if user volunteers. ``expected_type`` constrains
    LLM output and informs validation. ``enum_values`` non-None implies
    ``expected_type == "enum"``.
    """

    name: str
    description_zh: str
    description_en: str
    criticality: Literal["H", "M", "L"]
    expected_type: Literal["string", "number", "date", "phone", "enum"]
    enum_values: tuple[str, ...] | None = None
    validation_hint: str = ""


class TaskPhase(Enum):
    """Generic task lifecycle (replaces ``BookingPhase``).

    Naming is mode-neutral so v1.x can use the same enum for in-person and
    accessibility flows without renaming: ``EXECUTION_ACTIVE`` is what the
    legacy ``IN_CALL`` becomes for non-phone modes.
    """

    DRAFT = "draft"
    TASK_PLANNING = "task_planning"
    COLLECTING = "collecting"
    READY_TO_DIAL = "ready_to_dial"
    EXECUTION_ACTIVE = "execution_active"
    NEEDS_CLARIFICATION = "needs_clarification"
    AWAIT_USER_CLARIFICATION = "await_user_clarification"
    POST_CALL_REVIEW = "post_call_review"
    CALLBACK_ACTIVE = "callback_active"
    COMPLETED = "completed"
    FAILED = "failed"


LEGAL_TASK_TRANSITIONS: dict[TaskPhase, set[TaskPhase]] = {
    TaskPhase.DRAFT: {TaskPhase.TASK_PLANNING, TaskPhase.FAILED},
    TaskPhase.TASK_PLANNING: {TaskPhase.COLLECTING, TaskPhase.FAILED},
    TaskPhase.COLLECTING: {TaskPhase.READY_TO_DIAL, TaskPhase.FAILED},
    TaskPhase.READY_TO_DIAL: {
        TaskPhase.EXECUTION_ACTIVE,
        TaskPhase.COLLECTING,
        TaskPhase.FAILED,
    },
    TaskPhase.EXECUTION_ACTIVE: {
        TaskPhase.NEEDS_CLARIFICATION,
        TaskPhase.AWAIT_USER_CLARIFICATION,
        TaskPhase.POST_CALL_REVIEW,
        TaskPhase.COMPLETED,
        TaskPhase.FAILED,
    },
    TaskPhase.NEEDS_CLARIFICATION: {
        TaskPhase.EXECUTION_ACTIVE,
        TaskPhase.POST_CALL_REVIEW,
        TaskPhase.FAILED,
    },
    TaskPhase.AWAIT_USER_CLARIFICATION: {
        TaskPhase.EXECUTION_ACTIVE,
        TaskPhase.POST_CALL_REVIEW,
        TaskPhase.FAILED,
    },
    TaskPhase.POST_CALL_REVIEW: {
        TaskPhase.CALLBACK_ACTIVE,
        TaskPhase.COMPLETED,
        TaskPhase.FAILED,
    },
    TaskPhase.CALLBACK_ACTIVE: {TaskPhase.POST_CALL_REVIEW, TaskPhase.FAILED},
    TaskPhase.COMPLETED: set(),
    TaskPhase.FAILED: set(),
}


@dataclass
class TaskAuditEntry:
    """Immutable audit record for one TaskPhase transition. Mirror of
    BookingAuditEntry but typed against TaskPhase."""

    timestamp: float
    from_phase: TaskPhase
    to_phase: TaskPhase
    reason: str
    evidence: dict[str, Any]


@dataclass
class TaskState:
    """Universal task state — the v1 primary type, replacing ``BookingState``.

    The v1 model:
    - The slot schema is **dynamic**, generated at runtime by the Layer 1
      task planner from the user's free-form task description; there are no
      booking-specific fields (``restaurant_name`` / ``date`` / etc. are gone).
    - Phases come from the **generic** ``TaskPhase`` machine, with
      ``DRAFT`` / ``TASK_PLANNING`` added at the front and
      ``EXECUTION_ACTIVE`` instead of ``IN_CALL`` so non-phone modes reuse
      the same enum.
    - All other state (readiness, clarifications, audit log, language
      fields, merchant_held flag) carries forward unchanged from the legacy
      booking model.

    Field grouping:
    - Identity / origin.
    - Task definition (filled after the Layer 1 task planner runs).
    - Execution state (mutated by preflight, clarification, and the merchant agent).
    - State machine.
    - v1.x extensibility hooks (hardcoded defaults in v1; do not remove).
    """

    # Identity
    session_id: str
    created_at: float = field(default_factory=time.monotonic)

    # Task definition — empty until Layer 1 fills them
    user_task_description: str = ""
    task_category: str = ""
    slots_schema: list[SlotDef] = field(default_factory=list)
    optional_slots_schema: list[SlotDef] = field(default_factory=list)
    conversation_goals: list[str] = field(default_factory=list)
    merchant_etiquette_notes: str = ""
    readiness_criteria_text: str = ""
    relay_strategy: str = ""

    # Execution state
    user_lang: str | None = None
    merchant_lang: str | None = None
    slots: dict[str, Any] = field(default_factory=dict)
    readiness: ReadinessVerdict | None = None
    pending_clarifications: list[ClarificationItem] = field(default_factory=list)
    merchant_held: bool = False

    # v1.0 RC fields (B3a §3.8)
    auto_translate_merchant: bool = True
    uncertain_assumptions: list[SlotAssumption] = field(default_factory=list)
    pending_callbacks: list[CallbackEntry] = field(default_factory=list)
    clarification_holds_used: int = 0
    user_takeover_active: bool = False
    call_segments: list[CallSegment] = field(default_factory=list)
    transcripts: list[TranscriptMessage] = field(default_factory=list)
    completion_summary: str | None = None

    # State machine
    phase: TaskPhase = TaskPhase.DRAFT
    audit_log: list[TaskAuditEntry] = field(default_factory=list)

    # v1.x extensibility hooks (do not remove — kept as forward-compat)
    preferred_voice_id: str | None = None
    mode: Literal["phone", "in-person"] = "phone"

    def transition(
        self,
        new: TaskPhase,
        *,
        reason: str,
        evidence: dict[str, Any] | None = None,
    ) -> None:
        """Atomic phase transition; mirror of BookingState.transition()."""
        if new not in LEGAL_TASK_TRANSITIONS[self.phase]:
            raise DialogueOrchestratorError(
                f"illegal transition {self.phase.value} → {new.value}: {reason}"
            )
        self.audit_log.append(
            TaskAuditEntry(
                timestamp=time.monotonic(),
                from_phase=self.phase,
                to_phase=new,
                reason=reason,
                evidence=evidence or {},
            )
        )
        self.phase = new

    def get_slot(self, name: str) -> Any:
        """Returns None if slot not yet collected (instead of KeyError)."""
        return self.slots.get(name)

    def critical_slots_missing(self) -> list[str]:
        """Names of H-level slots in schema that are not yet in `slots`."""
        return [s.name for s in self.slots_schema if s.criticality == "H" and s.name not in self.slots]

    def record_uncertain_assumption(
        self,
        *,
        slot: str,
        question: str,
        assumed_value: Any,
        source: Literal["user_timeout", "merchant_impatience"],
    ) -> SlotAssumption:
        sa = SlotAssumption(
            id=uuid.uuid4().hex,
            slot=slot,
            question=question,
            assumed_value=assumed_value,
            source=source,
            created_at=_dt.datetime.now(_dt.timezone.utc),
        )
        self.uncertain_assumptions.append(sa)
        return sa

    def confirm_assumption(
        self,
        assumption_id: str,
        *,
        choice: Literal["correct", "wrong"],
        correction: str | None,
        note: str | None = None,
    ) -> CallbackEntry | None:
        sa = self.find_assumption_by_id(assumption_id)
        if sa is None:
            raise KeyError(assumption_id)

        if choice == "correct":
            sa.status = "confirmed"
            return None

        if correction is None:
            raise ValueError("correction is required when choice='wrong'")

        existing = next(
            (
                callback for callback in self.pending_callbacks
                if callback.assumption_id == sa.id
                and callback.status in {"queued", "in_progress"}
            ),
            None,
        )
        if existing is not None:
            sa.status = "corrected"
            sa.correction = correction
            sa.note = note
            sa.callback_id = existing.id
            if existing.status == "queued":
                existing.correction = correction
                existing.note = note
            return existing

        cb = CallbackEntry(
            id=uuid.uuid4().hex,
            assumption_id=sa.id,
            correction=correction,
            note=note,
            created_at=_dt.datetime.now(_dt.timezone.utc),
        )
        sa.status = "corrected"
        sa.correction = correction
        sa.note = note
        sa.callback_id = cb.id
        self.pending_callbacks.append(cb)
        return cb

    def find_assumption_by_id(self, assumption_id: str) -> SlotAssumption | None:
        for assumption in self.uncertain_assumptions:
            if assumption.id == assumption_id:
                return assumption
        return None

    def set_user_takeover(self, *, active: bool) -> None:
        self.user_takeover_active = active

    def reset_clarification_holds(self) -> None:
        self.clarification_holds_used = 0

    def start_call_segment(self) -> CallSegment:
        segment = CallSegment.new(
            index=len(self.call_segments) + 1,
            started_at=_dt.datetime.now(_dt.timezone.utc),
        )
        self.call_segments.append(segment)
        return segment

    def end_current_segment(
        self,
        *,
        interrupted: bool = False,
        reason: Literal["ws_close", "user_hangup", "merchant_impatience"] | None = None,
    ) -> None:
        if not self.call_segments:
            return
        segment = self.call_segments[-1]
        if segment.ended_at is not None:
            return
        segment.ended_at = _dt.datetime.now(_dt.timezone.utc)
        segment.interrupted = interrupted
        segment.interrupt_reason = reason

    def mark_current_segment_interrupted(
        self,
        *,
        reason: Literal["ws_close", "user_hangup", "merchant_impatience"],
    ) -> None:
        self.end_current_segment(interrupted=True, reason=reason)


# ---------------------------------------------------------------------------
# v1 RC: SlotAssumption / CallbackEntry / TranscriptMessage (B3a §3.8).
# ---------------------------------------------------------------------------


class CallSegment(BaseModel):
    """One contiguous EXECUTION_ACTIVE phone-call segment."""

    id: str
    index: int
    started_at: _dt.datetime
    ended_at: _dt.datetime | None = None
    interrupted: bool = False
    interrupt_reason: Literal["ws_close", "user_hangup", "merchant_impatience"] | None = None

    @classmethod
    def new(cls, *, index: int, started_at: _dt.datetime) -> "CallSegment":
        return cls(id=uuid.uuid4().hex, index=index, started_at=started_at)


class SlotAssumption(BaseModel):
    """One AI-default-filled slot recorded for post-call user review.

    See spec §3.8 for the field semantics and §3.6 / §3.7 for the two
    sources (`user_timeout` and `merchant_impatience`).
    """

    id: str
    slot: str
    question: str
    assumed_value: Any
    source: Literal["user_timeout", "merchant_impatience"]
    created_at: _dt.datetime
    status: Literal["pending_review", "confirmed", "corrected"] = "pending_review"
    correction: str | None = None
    note: str | None = None
    callback_id: str | None = None


class CallbackEntry(BaseModel):
    """User-triggered callback queue entry (spec §3.6 step 5)."""

    id: str
    assumption_id: str
    correction: str
    note: str | None = None
    status: Literal[
        "queued",
        "in_progress",
        "completed",
        "failed",
        "cancelled",
        "triggered",
    ] = "queued"
    created_at: _dt.datetime
    started_at: _dt.datetime | None = None
    completed_at: _dt.datetime | None = None
    transcript_segment_id: str | None = None


class TranscriptMessage(BaseModel):
    """Identity model for `transcript_update` frame payloads (spec §4.4).

    Server-assigned `id` (uuid4 string); frontend reconciles by id.
    """

    id: str
    role: Literal[
        "ai_to_user",
        "ai_to_merchant",
        "merchant_to_ai",
        "user_supplement",
        "user_takeover_passthrough",
        "system",
    ]
    text: str
    lang: Literal["zh", "en"] | None = None
    is_final: bool
    subtype: Literal[
        "original",
        "translation",
        "user_supplement",
        "user_takeover_passthrough",
        "callback_segment",
    ] = "original"
    parent_id: str | None = None
    segment_id: str | None = None
    created_at: _dt.datetime


__all__ = [
    "BookingAuditEntry",
    "BookingPhase",
    "BookingState",
    "CallSegment",
    "CallbackEntry",
    "ClarificationItem",
    "DialogueOrchestratorError",
    "LEGAL_TRANSITIONS",
    "LEGAL_TASK_TRANSITIONS",
    "ReadinessVerdict",
    "SlotAssumption",
    "SlotDef",
    "TaskAuditEntry",
    "TaskPhase",
    "TaskState",
    "TranscriptMessage",
    "_schema_check",
]
