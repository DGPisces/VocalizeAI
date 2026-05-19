"""24-hour stability driver for the VocalizeAI Pi orchestrator.

Triggers one ~5-minute text-bypass session every 30 minutes for N minutes
against the *live Pi orchestrator* over the network using raw httpx/websockets.
NOT TestClient-based — see PATTERNS.md §scripts/stability-24h-driver.py.

Usage (run from a remote runner host):
    ssh <your-runner-host>
    cd /path/to/VocalizeAI
    export VOCALIZE_API_BASE=https://vocalize-api.dgpisces.com
    export VOCALIZE_INVITE_TOKEN=<token>
    python scripts/stability-24h-driver.py --duration-minutes 1440 \\
        --scenario balance_inquiry_en_query --seed direct

Environment variables:
    VOCALIZE_API_BASE     Base URL for the Pi orchestrator REST API.
                          Default: https://vocalize-api.dgpisces.com
    VOCALIZE_INVITE_TOKEN Required when API base is non-localhost.
                          Set to match VOCALIZE_INVITE_TOKEN on the Pi.

Prerequisites on Pi (MUST be set before the 24h run):
    VOCALIZE_ENABLE_TEST_FRAMES=1  in /opt/vocalize/.env (or your VOCALIZE_HOME path)
    VOCALIZE_INVITE_TOKEN=<token>  in /opt/vocalize/.env (or your VOCALIZE_HOME path)

After the run, paste the output file into
docs/release/24h-stability-evidence.md for the release record.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import textwrap
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import websockets
import yaml

log = logging.getLogger("stability-driver")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
INTERVAL_S = 30 * 60  # one cycle every 30 minutes
DEFAULT_DURATION_MIN = 24 * 60  # 24 hours
DEFAULT_SCENARIO_ID = "balance_inquiry_en_query"
DEFAULT_SEED_ID = "direct"
DEFAULT_API_BASE = "https://vocalize-api.dgpisces.com"

# Path to scenarios.yaml (sibling of tests/integration/ai_merchant.py)
_SCENARIOS_YAML = (
    Path(__file__).parent.parent / "tests" / "integration" / "scenarios.yaml"
)


# ---------------------------------------------------------------------------
# Scenario loading
# ---------------------------------------------------------------------------
def _load_scenario_index() -> dict[str, Any]:
    """Load scenarios.yaml and return a dict keyed by scenario id."""
    if not _SCENARIOS_YAML.exists():
        log.warning("scenarios.yaml not found at %s; driver will use minimal task text", _SCENARIOS_YAML)
        return {}
    with _SCENARIOS_YAML.open() as f:
        data = yaml.safe_load(f)
    index: dict[str, Any] = {}
    for scenario in data.get("scenarios", []):
        index[scenario["id"]] = scenario
    return index


_SCENARIO_INDEX: dict[str, Any] = _load_scenario_index()


def _get_merchant_turns(scenario_id: str, seed_id: str) -> list[str]:
    """Return the merchant_turns list for the given scenario + seed pair."""
    scenario = _SCENARIO_INDEX.get(scenario_id)
    if scenario is None:
        return ["Hello, how can I help you?", "Okay, I'll take care of that."]
    for seed in scenario.get("seeds", []):
        if seed["id"] == seed_id:
            return list(seed.get("merchant_turns", []))
    return ["Hello, how can I help you?", "Okay, I'll take care of that."]


def _get_task_text(scenario_id: str) -> str:
    """Return the task text for a given scenario."""
    scenario = _SCENARIO_INDEX.get(scenario_id)
    if scenario is None:
        return "Perform a balance inquiry."
    return scenario.get("task", "Perform a balance inquiry.")


def _get_user_lang(scenario_id: str) -> str:
    scenario = _SCENARIO_INDEX.get(scenario_id)
    if scenario is None:
        return "en"
    return scenario.get("user_lang", "en")


def _get_merchant_lang(scenario_id: str) -> str:
    scenario = _SCENARIO_INDEX.get(scenario_id)
    if scenario is None:
        return "en"
    return scenario.get("merchant_lang", "en")


# ---------------------------------------------------------------------------
# Cycle implementation (raw httpx/websockets — NOT TestClient)
# ---------------------------------------------------------------------------
async def one_cycle(
    api_base: str,
    invite_token: str | None,
    scenario_id: str,
    seed_id: str,
) -> dict[str, Any]:
    """Run one text-bypass session cycle against the remote Pi orchestrator.

    Mirrors the WS frame choreography from tests/integration/ai_merchant.py:639-700
    but uses raw httpx and websockets instead of TestClient.

    Returns dict with keys: t (epoch), session_id, ok (bool), error (str|None).
    """
    cycle_start = time.time()
    headers: dict[str, str] = {}
    if invite_token:
        headers["X-Invite-Token"] = invite_token

    user_lang = _get_user_lang(scenario_id)
    merchant_lang = _get_merchant_lang(scenario_id)
    merchant_turns = _get_merchant_turns(scenario_id, seed_id)
    task_text = _get_task_text(scenario_id)
    dial_phrase = "dial now" if user_lang == "en" else "现在打吧"

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            # 1. Create session
            resp = await client.post(
                f"{api_base}/api/sessions",
                headers=headers,
                json={"default_lang": user_lang},
            )
            resp.raise_for_status()
            session_data = resp.json()
            session_id: str = session_data["session_id"]
            ws_url: str = session_data["ws_url"]
            log.info("cycle: session_id=%s ws_url=%s", session_id, ws_url)

            # 2. Set task
            task_resp = await client.post(
                f"{api_base}/api/sessions/{session_id}/task",
                headers=headers,
                json={"task": task_text},
            )
            task_resp.raise_for_status()

        # 3. WS choreography: text_input → readiness_change → mode_change → merchant_text_inject loop
        # Per ai_merchant.py:639-700 frame shapes.
        async with websockets.connect(ws_url, additional_headers=headers) as ws:
            # --- text_input: trigger preflight ---
            await ws.send(json.dumps(
                {"type": "text_input", "text": dial_phrase, "lang_hint": user_lang},
                ensure_ascii=False,
            ))

            # --- wait for readiness_change passed=True ---
            deadline = time.time() + 10.0
            readiness_ok = False
            while time.time() < deadline:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=0.5)
                except asyncio.TimeoutError:
                    continue
                frame = json.loads(raw)
                if frame.get("type") == "readiness_change" and frame.get("passed"):
                    readiness_ok = True
                    break
            if not readiness_ok:
                raise RuntimeError(f"no readiness_change before deadline (session {session_id})")

            # --- mode_change call_listening ---
            await ws.send(json.dumps({"type": "mode_change", "mode": "call_listening"}))

            # Wait for mode_ack
            deadline = time.time() + 5.0
            mode_ack_ok = False
            while time.time() < deadline:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=0.5)
                except asyncio.TimeoutError:
                    continue
                frame = json.loads(raw)
                if frame.get("type") == "mode_ack" and frame.get("mode") == "call_listening":
                    mode_ack_ok = True
                    break
            if not mode_ack_ok:
                log.warning("cycle: no mode_ack for call_listening (session %s) — continuing", session_id)

            # --- merchant_text_inject loop ---
            for turn in merchant_turns:
                inject_frame = {
                    "type": "merchant_text_inject",
                    "text": turn,
                    "scenario_id": scenario_id,
                    "seed": seed_id,
                    "lang_hint": merchant_lang,
                }
                await ws.send(json.dumps(inject_frame, ensure_ascii=False))
                # Wait briefly for transcript_update or ai_to_merchant response
                deadline = time.time() + 5.0
                while time.time() < deadline:
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=0.5)
                    except asyncio.TimeoutError:
                        break
                    frame = json.loads(raw)
                    ftype = frame.get("type", "")
                    if ftype in ("transcript_update", "tts_audio_start", "mode_ack"):
                        break

            # --- Hang up: mode_change call_completed ---
            await ws.send(json.dumps({"type": "mode_change", "mode": "call_completed"}))
            # Drain briefly to let post-call review start
            for _ in range(5):
                try:
                    await asyncio.wait_for(ws.recv(), timeout=0.3)
                except asyncio.TimeoutError:
                    break
                except Exception:
                    break

        return {
            "t": cycle_start,
            "session_id": session_id,
            "scenario_id": scenario_id,
            "seed": seed_id,
            "ok": True,
            "error": None,
            "elapsed_s": round(time.time() - cycle_start, 1),
        }

    except Exception as exc:
        log.error("cycle failed: %s", exc)
        return {
            "t": cycle_start,
            "session_id": None,
            "scenario_id": scenario_id,
            "seed": seed_id,
            "ok": False,
            "error": str(exc),
            "elapsed_s": round(time.time() - cycle_start, 1),
        }


# ---------------------------------------------------------------------------
# Metrics snapshot
# ---------------------------------------------------------------------------
async def fetch_metrics_snapshot(
    api_base: str,
    invite_token: str | None = None,
) -> str:
    """GET {api_base}/metrics and return the Prometheus exposition text."""
    headers: dict[str, str] = {}
    if invite_token:
        headers["X-Invite-Token"] = invite_token
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(f"{api_base}/metrics", headers=headers)
            resp.raise_for_status()
            return resp.text
    except Exception as exc:
        return f"# ERROR fetching /metrics: {exc}\n"


def _extract_metric(prometheus_text: str, metric_name: str) -> str:
    """Extract the numeric value for a gauge/counter from Prometheus text."""
    for line in prometheus_text.splitlines():
        if line.startswith(metric_name + " ") or line.startswith(metric_name + "{"):
            # Take the last whitespace-delimited token
            parts = line.rsplit(None, 1)
            if len(parts) == 2:
                return parts[1]
    return "N/A"


# ---------------------------------------------------------------------------
# Evidence file writer
# ---------------------------------------------------------------------------
def _write_evidence(
    out_path: str,
    session_log: list[dict[str, Any]],
    metrics_snapshots: list[tuple[float, str]],
    args: argparse.Namespace,
) -> None:
    """Write a Markdown evidence file to out_path."""
    p = Path(out_path)
    p.parent.mkdir(parents=True, exist_ok=True)

    run_start_dt = datetime.fromtimestamp(
        session_log[0]["t"] if session_log else time.time(), tz=timezone.utc
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    run_end_dt = datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    ok_count = sum(1 for r in session_log if r["ok"])
    fail_count = len(session_log) - ok_count

    lines: list[str] = []
    lines.append(f"# 24h Stability Run Evidence\n")
    lines.append(f"## Run Record\n")
    lines.append(f"| Field | Value |")
    lines.append(f"| --- | --- |")
    lines.append(f"| API base | `{args.api_base}` |")
    lines.append(f"| Scenario | `{args.scenario}` |")
    lines.append(f"| Seed | `{args.seed}` |")
    lines.append(f"| Duration (min) | {args.duration_minutes} |")
    lines.append(f"| Start | {run_start_dt} |")
    lines.append(f"| End | {run_end_dt} |")
    lines.append(f"| Cycles OK | {ok_count} |")
    lines.append(f"| Cycles FAIL | {fail_count} |")
    lines.append(f"")

    lines.append(f"## Session Log\n")
    lines.append(f"| # | Timestamp | Scenario | Seed | OK | Session ID | Error | Elapsed (s) |")
    lines.append(f"| --- | --- | --- | --- | --- | --- | --- | --- |")
    for i, r in enumerate(session_log, 1):
        ts = datetime.fromtimestamp(r["t"], tz=timezone.utc).strftime("%H:%M:%S")
        ok_str = "OK" if r["ok"] else "FAIL"
        sid = r.get("session_id") or ""
        err = (r.get("error") or "").replace("|", "\\|")[:80]
        lines.append(
            f"| {i} | {ts} | {r['scenario_id']} | {r['seed']} | {ok_str} | {sid} | {err} | {r.get('elapsed_s', '')} |"
        )
    lines.append(f"")

    lines.append(f"## /metrics Snapshots\n")
    lines.append(f"| Timestamp | uptime_seconds | rss_bytes | error_log_total | active_sessions |")
    lines.append(f"| --- | --- | --- | --- | --- |")
    for snap_t, snap_text in metrics_snapshots:
        ts = datetime.fromtimestamp(snap_t, tz=timezone.utc).strftime("%H:%M:%S")
        uptime = _extract_metric(snap_text, "vocalize_process_uptime_seconds")
        rss = _extract_metric(snap_text, "vocalize_process_rss_bytes")
        errors = _extract_metric(snap_text, "vocalize_error_log_total")
        active = _extract_metric(snap_text, "vocalize_active_sessions")
        lines.append(f"| {ts} | {uptime} | {rss} | {errors} | {active} |")
    lines.append(f"")

    lines.append(textwrap.dedent("""\
        ## Paste Instructions

        Copy this file's contents into `docs/release/24h-stability-evidence.md`
        under the **Synthetic workload log** and **Prometheus snapshots** sections
        for the release record. Fill in the Operator Record and Pass Criteria
        table cells from the Pi's systemd output before signing off.
        """))

    p.write_text("\n".join(lines), encoding="utf-8")
    log.info("evidence written to: %s", p)


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------
async def main(args: argparse.Namespace) -> None:
    api_base = args.api_base
    invite_token = args.invite_token

    # Require token for non-localhost
    is_localhost = api_base.startswith("http://127.") or api_base.startswith("http://localhost")
    if not is_localhost and not invite_token:
        log.error(
            "VOCALIZE_INVITE_TOKEN is required when VOCALIZE_API_BASE is not localhost.\n"
            "Set it in your environment before running the driver."
        )
        sys.exit(1)

    log.info(
        "stability-24h-driver starting: api_base=%s scenario=%s seed=%s duration_min=%d",
        api_base, args.scenario, args.seed, args.duration_minutes,
    )

    start = time.time()
    end = start + args.duration_minutes * 60

    session_log: list[dict[str, Any]] = []
    metrics_snapshots: list[tuple[float, str]] = []

    # Initial metrics snapshot
    snap = await fetch_metrics_snapshot(api_base, invite_token)
    metrics_snapshots.append((time.time(), snap))
    log.info("initial /metrics snapshot taken")

    cycle_num = 0
    while time.time() < end:
        cycle_num += 1
        cycle_start = time.time()
        log.info("--- cycle %d start (remaining: %.0f min) ---", cycle_num, (end - cycle_start) / 60)

        result = await one_cycle(api_base, invite_token, args.scenario, args.seed)
        session_log.append(result)
        status = "OK" if result["ok"] else f"FAIL: {result.get('error', '')}"
        log.info("cycle %d result: %s (%.1fs)", cycle_num, status, result.get("elapsed_s", 0))

        # Periodic metrics snapshot
        last_snap_t = metrics_snapshots[-1][0]
        if (cycle_start - last_snap_t) >= args.metrics_snapshot_interval_hours * 3600:
            snap = await fetch_metrics_snapshot(api_base, invite_token)
            metrics_snapshots.append((time.time(), snap))
            log.info("metrics snapshot taken at cycle %d", cycle_num)

        elapsed = time.time() - cycle_start
        sleep_s = max(0.0, INTERVAL_S - elapsed)
        remaining = end - time.time()
        if sleep_s > 0 and remaining > 0:
            log.info("sleeping %.0fs until next cycle (%.0f min remaining)", min(sleep_s, remaining), remaining / 60)
            await asyncio.sleep(min(sleep_s, remaining))

    # Final metrics snapshot
    snap = await fetch_metrics_snapshot(api_base, invite_token)
    metrics_snapshots.append((time.time(), snap))
    log.info("final /metrics snapshot taken; %d cycles completed", cycle_num)

    # Write evidence file
    _write_evidence(args.evidence_out, session_log, metrics_snapshots, args)
    log.info("run complete: %d ok / %d fail", sum(1 for r in session_log if r["ok"]), sum(1 for r in session_log if not r["ok"]))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "--duration-minutes",
        type=int,
        default=DEFAULT_DURATION_MIN,
        metavar="MIN",
        help=f"Total run duration in minutes (default: {DEFAULT_DURATION_MIN}; = 24h)",
    )
    p.add_argument(
        "--scenario",
        default=DEFAULT_SCENARIO_ID,
        metavar="SCENARIO_ID",
        help=f"Scenario id from tests/integration/scenarios.yaml (default: {DEFAULT_SCENARIO_ID})",
    )
    p.add_argument(
        "--seed",
        default=DEFAULT_SEED_ID,
        metavar="SEED_ID",
        help=f"Seed id within the chosen scenario (default: {DEFAULT_SEED_ID})",
    )
    p.add_argument(
        "--evidence-out",
        default=None,
        metavar="PATH",
        help=(
            "Path to write the per-run evidence markdown file. "
            "Default: docs/release/24h-stability-evidence-runs/<utc-timestamp>.md"
        ),
    )
    p.add_argument(
        "--metrics-snapshot-interval-hours",
        type=float,
        default=4.0,
        metavar="HOURS",
        help="How often to snapshot /metrics during the run (default: 4h)",
    )
    return p


if __name__ == "__main__":
    parser = _build_arg_parser()
    args = parser.parse_args()

    # Resolve env vars
    args.api_base = os.environ.get("VOCALIZE_API_BASE", DEFAULT_API_BASE).rstrip("/")
    args.invite_token = os.environ.get("VOCALIZE_INVITE_TOKEN") or None

    # Default evidence output path
    if args.evidence_out is None:
        ts = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        args.evidence_out = str(
            Path(__file__).parent.parent
            / "docs"
            / "release"
            / "24h-stability-evidence-runs"
            / f"{ts}.md"
        )

    asyncio.run(main(args))
