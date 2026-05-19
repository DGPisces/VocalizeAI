from __future__ import annotations

import pytest


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--ai-provider-required",
        action="store_true",
        default=False,
        help="Require DeepSeek-V4-Flash merchant generation; disables scripted fallback.",
    )
    parser.addoption(
        "--release-audio",
        action="store_true",
        default=False,
        help="Run the release-only real-audio scenario set.",
    )
