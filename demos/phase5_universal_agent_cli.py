#!/usr/bin/env python3
"""v1 universal phone agent — headless CLI demo.

Demonstrates the Layer 1 task planner + Layer 2 preflight collector
without requiring any audio hardware. The user describes their task
in natural language and the agent:
  1. Plans the task schema (Layer 1) via LLM
  2. Collects critical slots interactively (Layer 2 preflight)

Usage:
  python demos/phase5_universal_agent_cli.py "帮我订海底捞，明天晚上7点，三个人"
  python demos/phase5_universal_agent_cli.py                    # interactive prompt
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from vocalize.config import Config
from vocalize.dialogue.state import TaskPhase, TaskState
from vocalize.dialogue.task_planner import generate_task_schema
from vocalize.llm.openai_compat import OpenAICompatClient


async def main() -> None:
    if len(sys.argv) < 2:
        task = input("What task should I handle? > ")
    else:
        task = " ".join(sys.argv[1:])

    if not task.strip():
        print("No task provided. Exiting.")
        return

    config = Config.from_env()
    llm = OpenAICompatClient.from_app_config(config)

    # Detect language using the same heuristic as the orchestrator so the
    # demo CLI and production code agree on what counts as zh vs en.
    from vocalize.dialogue.language import detect_lang_from_text
    user_lang = detect_lang_from_text(task)
    print(f"Task: {task}")
    print(f"Language: {user_lang}")
    print("-" * 40)

    # Layer 1: Task planning
    print("[Layer 1] Planning task schema...")
    schema = await generate_task_schema(task, user_lang=user_lang, llm=llm)

    if schema.refused:
        print(f"REFUSED: {schema.reasoning}")
        return

    print(f"Category: {schema.task_category}")
    print(f"H-slots ({len(schema.slots_schema)}):")
    for s in schema.slots_schema:
        extra = ""
        if s.enum_values:
            extra = f" [values: {', '.join(s.enum_values)}]"
        print(f"  - {s.name} ({s.description_zh}){extra}")
    if schema.optional_slots_schema:
        print(f"Optional slots ({len(schema.optional_slots_schema)}):")
        for s in schema.optional_slots_schema:
            extra = ""
            if s.enum_values:
                extra = f" [values: {', '.join(s.enum_values)}]"
            print(f"  - {s.name} ({s.description_zh}){extra}")
    print(f"Goals: {schema.conversation_goals}")
    print(f"Readiness: {schema.readiness_criteria_text}")
    print(f"Relay strategy: {schema.relay_strategy}")
    print("-" * 40)

    # Create state
    state = TaskState(session_id="demo", user_task_description=task)
    state.user_lang = user_lang
    state.task_category = schema.task_category
    state.slots_schema = schema.slots_schema
    state.optional_slots_schema = schema.optional_slots_schema
    state.conversation_goals = schema.conversation_goals
    state.merchant_etiquette_notes = schema.merchant_etiquette_notes
    state.readiness_criteria_text = schema.readiness_criteria_text
    state.relay_strategy = schema.relay_strategy

    # Layer 2: Simple interactive preflight
    print("[Layer 2] Preflight - collecting critical slots...")
    state.phase = TaskPhase.COLLECTING
    while state.phase == TaskPhase.COLLECTING:
        missing = state.critical_slots_missing()
        if not missing:
            print("All critical slots filled!")
            break
        slot = missing[0]
        sdef = next(s for s in schema.slots_schema if s.name == slot)
        desc = sdef.description_zh if user_lang == "zh" else sdef.description_en
        value = input(f"[{desc}] {slot}: ")
        if value.strip():
            state.slots[slot] = value.strip()
            # Track merchant_lang for downstream layers
            if slot == "merchant_lang":
                state.merchant_lang = value.strip()
        else:
            print("(skipped)")

    print("\nCollected slots:")
    for k, v in state.slots.items():
        print(f"  {k}: {v}")

    print("\nDemo complete. Full merchant call loop is in orchestrator.")


if __name__ == "__main__":
    if not os.environ.get("OPENAI_API_KEY"):
        print(
            "WARNING: OPENAI_API_KEY not set. The demo will fail at the LLM step.\n"
            "Set it via: export OPENAI_API_KEY=your-key",
            file=sys.stderr,
        )
    asyncio.run(main())
