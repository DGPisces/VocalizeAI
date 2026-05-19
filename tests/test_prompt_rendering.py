"""Layer 1 of test stack: snapshot tests for prompt template rendering.

Detects: template regression (placeholder rot), schema-prompt drift.
Cost: free (no LLM calls).
Frequency: every commit.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from vocalize.dialogue.state import SlotDef, TaskPhase, TaskState


SNAPSHOT_DIR = Path(__file__).parent / "snapshots"
SNAPSHOT_DIR.mkdir(exist_ok=True)


def _make_booking_state() -> TaskState:
    return TaskState(
        session_id="test",
        user_task_description="help me book Haidilao",
        task_category="restaurant-booking",
        slots_schema=[
            SlotDef(
                name="merchant_lang",
                description_zh="merchant language",
                description_en="merchant lang",
                criticality="H",
                expected_type="enum",
                enum_values=("zh", "en"),
            ),
            SlotDef(
                name="restaurant_branch",
                description_zh="branch",
                description_en="branch",
                criticality="H",
                expected_type="string",
            ),
            SlotDef(
                name="booking_date",
                description_zh="date",
                description_en="date",
                criticality="H",
                expected_type="date",
            ),
        ],
        optional_slots_schema=[
            SlotDef(
                name="special_requirements",
                description_zh="special requests",
                description_en="special",
                criticality="L",
                expected_type="string",
            ),
        ],
        conversation_goals=["confirm availability", "confirm time", "get confirmation number"],
        merchant_etiquette_notes="Restaurant usually answers with 'Hello'",
        readiness_criteria_text="All H slots filled and valid",
        relay_strategy="Numbers verbatim",
        slots={"merchant_lang": "zh"},
        user_lang="zh",
        merchant_lang="zh",
        phase=TaskPhase.COLLECTING,
    )


def _check_snapshot(name: str, content: str) -> None:
    snap_path = SNAPSHOT_DIR / f"{name}.txt"
    if not snap_path.exists():
        snap_path.write_text(content, encoding="utf-8")
        pytest.skip(f"snapshot {name} created; rerun to verify")
    expected = snap_path.read_text(encoding="utf-8")
    assert content == expected, (
        f"snapshot mismatch for {name}. To accept, delete {snap_path} and rerun."
    )


def test_preflight_collector_zh_rendering():
    """Verify preflight prompt renders slot placeholders correctly."""
    from vocalize.dialogue.orchestrator import _render_prompt

    state = _make_booking_state()
    rendered = _render_prompt("preflight_collector", state)
    _check_snapshot("preflight_collector_zh_booking", rendered)
    assert "restaurant-booking" in rendered
    assert "merchant_lang: zh" in rendered
    assert "restaurant_branch" in rendered
    assert "{task_category}" not in rendered


def test_merchant_agent_zh_rendering():
    """Verify merchant agent prompt renders goals and etiquette."""
    from vocalize.dialogue.orchestrator import _render_prompt

    state = _make_booking_state()
    rendered = _render_prompt("merchant_agent", state)
    _check_snapshot("merchant_agent_zh_booking", rendered)
    assert "confirm availability" in rendered
    assert "{conversation_goals_pretty}" not in rendered


def test_clarification_collector_zh_rendering():
    """Verify clarification prompt renders slot context."""
    from vocalize.dialogue.orchestrator import _render_prompt

    state = _make_booking_state()
    rendered = _render_prompt(
        "clarification_collector",
        state,
        merchant_question="do you have allergies?",
        slot_name="allergy",
        slot_description_zh="allergies",
    )
    _check_snapshot("clarification_collector_zh_allergy", rendered)
    assert "do you have allergies?" in rendered
    assert "allergy" in rendered


def test_merchant_agent_falls_back_to_user_lang_when_merchant_lang_unknown():
    """When merchant_lang has not been collected yet (e.g. dial-now
    short-circuit before preflight asked for it), the merchant_agent
    prompt must use user_lang as fallback — not hardcoded zh — so the
    merchant LLM receives instructions in the right language for the
    channel.
    """
    from vocalize.dialogue.orchestrator import _render_prompt
    from vocalize.dialogue.state import TaskState

    state = TaskState(session_id="t-fallback")
    state.user_lang = "en"
    state.merchant_lang = None
    state.task_category = "x"

    rendered = _render_prompt("merchant_agent", state)
    # The English merchant_agent prompt has English headings; the zh one
    # has CJK. Verify we got the en variant.
    assert "merchant_agent_en" in rendered or "You are" in rendered, (
        "expected en merchant_agent prompt when user_lang='en' and "
        "merchant_lang is None"
    )


def test_unfilled_optional_shows_in_optional_section():
    """Verify optional slots appear in the optional section of rendered prompt."""
    from vocalize.dialogue.orchestrator import _render_prompt

    state = _make_booking_state()
    rendered = _render_prompt("preflight_collector", state)
    assert "special_requirements" in rendered
    assert "special" in rendered


def test_hold_filler_templates_load() -> None:
    from vocalize.dialogue.prompts import load_prompt

    zh = load_prompt("hold_filler_zh")
    en = load_prompt("hold_filler_en")

    assert "等一下" in zh
    assert "back to you" in en.lower()


def test_impatience_end_templates_load() -> None:
    from vocalize.dialogue.prompts import load_prompt

    zh = load_prompt("impatience_end_zh")
    en = load_prompt("impatience_end_en")

    assert "再回您电话" in zh
    assert "call you back shortly" in en.lower()


def test_clarification_keepalive_templates_load() -> None:
    from vocalize.dialogue.prompts import load_prompt

    zh = load_prompt("clarification_keepalive_zh")
    en = load_prompt("clarification_keepalive_en")

    assert "正在确认" in zh
    assert "still checking" in en.lower()


def test_preflight_collector_zh_has_supplement_rule() -> None:
    from vocalize.dialogue.prompts import load_prompt

    rendered = load_prompt("preflight_collector_zh")
    assert "不要复述" in rendered or "自然地吸收" in rendered


def test_preflight_collector_en_has_supplement_rule() -> None:
    from vocalize.dialogue.prompts import load_prompt

    rendered = load_prompt("preflight_collector_en")
    assert (
        "do not metaphrase" in rendered.lower()
        or "do not echo" in rendered.lower()
    )


def test_merchant_agent_zh_has_supplement_priority_rule() -> None:
    from vocalize.dialogue.prompts import load_prompt

    rendered = load_prompt("merchant_agent_zh")
    assert "[USER HINT]" in rendered
    assert "优先" in rendered


def test_merchant_agent_zh_requires_clarification_before_user_slot_changes() -> None:
    from vocalize.dialogue.prompts import load_prompt

    rendered = load_prompt("merchant_agent_zh")
    assert "不能主动替用户改" in rendered
    assert "request_user_clarification" in rendered


def test_merchant_agent_zh_forbids_internal_reasoning_in_speech() -> None:
    from vocalize.dialogue.prompts import load_prompt

    rendered = load_prompt("merchant_agent_zh")
    assert "只输出要对商家说的话" in rendered
    assert "不要输出内部推理" in rendered
    assert "绝对不要输出括号里的自我说明" in rendered
    assert "不要说\"可能是在确认人数或者有其他含义\"" in rendered


def test_merchant_agent_en_has_supplement_priority_rule() -> None:
    from vocalize.dialogue.prompts import load_prompt

    rendered = load_prompt("merchant_agent_en")
    assert "[USER HINT]" in rendered
    assert "priority" in rendered.lower()


def test_merchant_agent_en_requires_clarification_before_user_slot_changes() -> None:
    from vocalize.dialogue.prompts import load_prompt

    rendered = load_prompt("merchant_agent_en")
    assert "must not offer, accept, or confirm" in rendered.lower()
    assert "request_user_clarification" in rendered


def test_merchant_agent_en_forbids_internal_reasoning_in_speech() -> None:
    from vocalize.dialogue.prompts import load_prompt

    rendered = load_prompt("merchant_agent_en")
    assert "merchant-facing speech only" in rendered
    assert "internal reasoning" in rendered.lower()
    assert "status updates" in rendered.lower()
    assert "Never output parenthetical" in rendered
    assert "Never say \"this may mean...\"" in rendered


def test_merchant_agent_zh_natural_booking_close() -> None:
    from vocalize.dialogue.prompts import load_prompt

    rendered = load_prompt("merchant_agent_zh")
    assert "订位任务的自然收尾" in rendered
    assert "如果商家说没有确认号" in rendered
    assert "不要继续追问确认号" in rendered
    assert "不要静默调用 finalize_task" in rendered
    assert "谢谢" in rendered
    assert "再见" in rendered


def test_merchant_agent_en_natural_booking_close() -> None:
    from vocalize.dialogue.prompts import load_prompt

    rendered = load_prompt("merchant_agent_en")
    assert "Natural booking close" in rendered
    assert "says there is no" in rendered
    assert "confirmation number" in rendered
    assert "do not ask again for a confirmation number" in rendered
    assert "Do not silently call `finalize_task`" in rendered
    assert "thank you" in rendered.lower()
    assert "goodbye" in rendered.lower()


def test_callback_correction_zh_loads_with_placeholders() -> None:
    from vocalize.dialogue.prompts import load_prompt

    rendered = load_prompt("callback_correction_zh")
    assert "回拨通话" in rendered
    assert "{{slot}}" in rendered
    assert "{{assumed_value}}" in rendered
    assert "{{correction}}" in rendered
    assert "{{note}}" in rendered
    assert "finalize_task" in rendered


def test_callback_correction_en_loads_with_placeholders() -> None:
    from vocalize.dialogue.prompts import load_prompt

    rendered = load_prompt("callback_correction_en")
    assert "follow-up callback" in rendered.lower() or "callback" in rendered.lower()
    assert "{{slot}}" in rendered
    assert "{{assumed_value}}" in rendered
    assert "{{correction}}" in rendered
    assert "{{note}}" in rendered
    assert "finalize_task" in rendered
