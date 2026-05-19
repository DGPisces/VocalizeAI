"""Scenario loader for dialogue scenario tests.

Mirrors src/vocalize/config.py's "load once + return" pattern but reads JSONL,
not env. Each line = one scenario per RESEARCH §Validation Architecture.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

_FIXTURES_DIR = Path(__file__).parent


def load_scenarios(filename: str = "scenarios.jsonl") -> list[dict[str, Any]]:
    path = _FIXTURES_DIR / filename
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
