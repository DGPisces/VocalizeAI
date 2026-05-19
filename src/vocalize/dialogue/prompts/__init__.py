"""Prompt-file loader. Plain ``importlib.resources`` — no Jinja, no frontmatter.

Each ``.md`` is a static LLM system message. The v1 5-layer set is
task_planner / preflight_collector / merchant_agent /
clarification_collector / relay, each with ``_zh`` and ``_en`` variants.
The orchestrator selects one per phase + channel + lang and assembles
it into ``ChatMessage(role="system", content=...)``.

Design choices:
- Plain markdown, not JSON / YAML / TOML — the LLM consumes the file
  contents directly; extra structure spends tokens for no LLM benefit.
- ``importlib.resources`` (stdlib), not Jinja — prompts are static
  system prompts; runtime variables (filled slots, missing slots, today's
  date) are substituted with simple ``str.replace`` at the call site,
  keeping the system prompt cacheable.
- ``load_prompt`` returns are not cached — files are tiny (< 2 KB each)
  and a single disk read is cheaper than the cache-staleness footgun.
"""
from __future__ import annotations

from importlib import resources


def load_prompt(name: str, **substitutions: str) -> str:
    """Read ``prompts/{name}.md`` and return its text, with optional
    ``{{KEY}}`` placeholder substitution.

    Substitution is intentionally minimal — plain ``str.replace`` over
    ``{{KEY}}`` tokens, no Jinja, no escaping, no conditionals. Sole
    purpose: inject runtime values (today's date, etc.) that the LLM
    cannot infer from training data alone. The ``{{KEY}}`` syntax is
    inert in markdown / LLM input, so files without placeholders are
    unaffected by passing kwargs.

    Args:
        name: prompt file basename (without ``.md``).
        **substitutions: ``KEY=value`` pairs; each ``{{KEY}}`` literal in
            the prompt body is replaced with ``value``. Missing
            placeholders silently leave kwargs unused; missing kwargs
            silently leave ``{{KEY}}`` literals in place (caller is
            responsible for either always passing them or accepting the
            literal as fallback).

    Raises:
        FileNotFoundError: if ``{name}.md`` does not exist in this package.
            Tests rely on the bare ``FileNotFoundError`` (not a wrapped
            DialogueOrchestratorError) so callers can distinguish "typo"
            from "package install missing .md files".
    """
    text = (
        resources.files(__package__)
        .joinpath(f"{name}.md")
        .read_text(encoding="utf-8")
    )
    for key, value in substitutions.items():
        text = text.replace(f"{{{{{key}}}}}", value)
    return text


__all__ = ["load_prompt"]
