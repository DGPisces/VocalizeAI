"""Prometheus metric declarations for the VocalizeAI orchestrator.

This module is the single source of truth for all ``vocalize_*`` Prometheus
metrics.  It is imported by:
- ``src/vocalize/server/__init__.py`` (wires the instrumentator + refresh middleware)
- ``src/vocalize/server/ws.py`` (increments WS lifecycle counters)

Design note: the ``refresh_runtime_gauges`` helper is called on every
``/metrics`` scrape (not on every request) to keep scrape cost bounded.
``install_error_counter`` must be called once at app startup.

Reference: prometheus_client pattern from
``infra/gpu-services/sensevoice/server.py:75-161``.
"""
from __future__ import annotations

import logging
import platform
import resource
import time

from prometheus_client import Counter, Gauge

# ---------------------------------------------------------------------------
# Module-level epoch for uptime gauge
# ---------------------------------------------------------------------------
_START_T = time.time()

# ---------------------------------------------------------------------------
# Counters
# ---------------------------------------------------------------------------
ERROR_LOG_TOTAL = Counter(
    "vocalize_error_log_total",
    "ERROR-level log entries since process start",
)
WS_SESSIONS_OPENED_TOTAL = Counter(
    "vocalize_ws_sessions_opened_total",
    "Sessions opened on /ws/sessions/{id}",
)
WS_SESSIONS_CLOSED_TOTAL = Counter(
    "vocalize_ws_sessions_closed_total",
    "Sessions closed",
    ["reason"],
)

# ---------------------------------------------------------------------------
# Gauges
# ---------------------------------------------------------------------------
ACTIVE_SESSIONS = Gauge(
    "vocalize_active_sessions",
    "Live sessions in SessionRegistry",
)
PROCESS_UPTIME_SECONDS = Gauge(
    "vocalize_process_uptime_seconds",
    "Seconds since process started",
)
PROCESS_RSS_BYTES = Gauge(
    "vocalize_process_rss_bytes",
    "Process resident set size in bytes",
)


# ---------------------------------------------------------------------------
# Error-log handler
# ---------------------------------------------------------------------------
class ErrorCounterHandler(logging.Handler):
    """Logging handler that increments ERROR_LOG_TOTAL on every ERROR+."""

    def emit(self, record: logging.LogRecord) -> None:
        if record.levelno >= logging.ERROR:
            try:
                ERROR_LOG_TOTAL.inc()
            except Exception:
                self.handleError(record)


def install_error_counter() -> None:
    """Add ErrorCounterHandler to the root logger; idempotent (de-dup by type)."""
    root = logging.getLogger()
    if not any(isinstance(h, ErrorCounterHandler) for h in root.handlers):
        root.addHandler(ErrorCounterHandler())


# ---------------------------------------------------------------------------
# Gauge refresh helper (called on /metrics scrape)
# ---------------------------------------------------------------------------
def refresh_runtime_gauges(registry: object) -> None:
    """Refresh process-level gauges just before a Prometheus scrape.

    Args:
        registry: ``SessionRegistry`` instance (passed from ``app.state.registry``).
                  Uses ``getattr(..., "_sessions", {})`` for safety so this never
                  raises if registry is not yet initialised.
    """
    PROCESS_UPTIME_SECONDS.set(time.time() - _START_T)
    rss_raw = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    # Linux (Pi): ru_maxrss is in kilobytes → convert to bytes.
    # macOS/BSD: ru_maxrss is already in bytes.
    if platform.system() == "Linux":
        rss = rss_raw * 1024
    else:
        rss = rss_raw
    PROCESS_RSS_BYTES.set(rss)
    # len(_sessions) is the number of sessions in the registry
    ACTIVE_SESSIONS.set(len(getattr(registry, "_sessions", {})))


__all__ = [
    "ERROR_LOG_TOTAL",
    "WS_SESSIONS_OPENED_TOTAL",
    "WS_SESSIONS_CLOSED_TOTAL",
    "ACTIVE_SESSIONS",
    "PROCESS_UPTIME_SECONDS",
    "PROCESS_RSS_BYTES",
    "ErrorCounterHandler",
    "install_error_counter",
    "refresh_runtime_gauges",
]
