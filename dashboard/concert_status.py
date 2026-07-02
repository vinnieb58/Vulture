"""Read-only Vulture Concerts visibility for the Nest dashboard."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from host_status import ServiceStatus, _check_service, _normalize_unit_state, _resolve_unit
from host_commands import systemctl_is_active, systemctl_is_enabled
from vulture_runtime import _journal_lines, _list_timer_next_run, _parse_log_timestamp

CONCERT_TIMER_UNIT = os.environ.get(
    "VULTURE_CONCERT_WATCHES_TIMER", "vulture-concert-watches.timer"
)
CONCERT_SERVICE_UNIT = os.environ.get(
    "VULTURE_CONCERT_WATCHES_SERVICE", "vulture-concert-watches.service"
)
CONCERT_STATUS_PATH = Path(
    os.environ.get("CONCERT_WATCH_STATUS_PATH", "/app/data/concert_watch_status.json")
)
CONCERT_LOG_PATH = Path(
    os.environ.get("CONCERT_WATCH_LOG_PATH", "/app/logs/concert_watches.log")
)
RECENT_DAYS = int(os.environ.get("DASHBOARD_CONCERT_RECENT_DAYS", "7"))

CYCLE_KEYWORDS = (
    "concert watch cycle",
    "watches=",
)
SUCCESS_KEYWORDS = (
    "concert watch cycle:",
    "concert watch cycle completed",
)
ERROR_KEYWORDS = (
    "error",
    "failed",
    "exception",
    "traceback",
)


def _provider_configured() -> dict[str, bool]:
    return {
        "ticketmaster": bool(os.getenv("TICKETMASTER_API_KEY", "").strip()),
        "seatgeek": bool(os.getenv("SEATGEEK_CLIENT_ID", "").strip()),
    }


def _read_status_file() -> dict[str, Any] | None:
    if not CONCERT_STATUS_PATH.is_file():
        return None
    try:
        data = json.loads(CONCERT_STATUS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _read_log_lines(limit: int = 80) -> list[str]:
    if not CONCERT_LOG_PATH.is_file():
        return []
    try:
        text = CONCERT_LOG_PATH.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return lines[-limit:]


def _check_concert_service() -> ServiceStatus:
    """Oneshot concert watch service: inactive between runs is expected."""
    unit = _resolve_unit(
        (CONCERT_SERVICE_UNIT, CONCERT_SERVICE_UNIT.replace(".service", ""))
    )
    if unit is None:
        return ServiceStatus(
            label="vulture-concert-watches service",
            unit=None,
            active="not found",
            enabled="not configured",
            warning=None,
        )

    ok_active, active_out = systemctl_is_active(unit)
    ok_enabled, enabled_out = systemctl_is_enabled(unit)
    active = _normalize_unit_state(active_out if ok_active else "unknown", missing_label="unknown")
    enabled = _normalize_unit_state(
        enabled_out if ok_enabled else "unknown",
        missing_label="not configured",
    )

    warning = None
    if not ok_active and active == "unknown":
        warning = "vulture-concert-watches service: systemctl unavailable"
    elif active == "failed":
        warning = "Concert watch service failed"

    return ServiceStatus(
        label="vulture-concert-watches service",
        unit=unit,
        active=active,
        enabled=enabled,
        warning=warning,
    )


def _parse_journal_last_run(journal: list[str]) -> dict[str, str | None]:
    """Extract last run timestamp and result from service journal lines."""
    last_run: str | None = None
    last_result: str | None = None

    for line in reversed(journal):
        lower = line.lower()
        if "finished" in lower and CONCERT_SERVICE_UNIT.replace(".service", "") in lower:
            ts = _parse_log_timestamp(line)
            if ts:
                last_run = ts.strftime("%Y-%m-%d %H:%M:%S UTC")
            if "code=exited" in lower or "status=" in lower:
                if "status=0" in lower or "code=exited, status=0" in lower:
                    last_result = "success"
                elif "status=" in lower or "code=exited" in lower:
                    last_result = "failed"
            if last_run and last_result:
                break

    if last_result is None:
        for line in reversed(journal):
            lower = line.lower()
            if not any(k in lower for k in CYCLE_KEYWORDS):
                continue
            ts = _parse_log_timestamp(line)
            if ts and last_run is None:
                last_run = ts.strftime("%Y-%m-%d %H:%M:%S UTC")
            if "error" in lower or "failed" in lower or "exception" in lower:
                last_result = "failed"
                break
            if any(k in lower for k in SUCCESS_KEYWORDS):
                last_result = "success"
                break

    return {"last_run": last_run, "last_result": last_result}


def _freshness_from_log(lines: list[str]) -> dict[str, str | None]:
    for line in reversed(lines):
        lower = line.lower()
        if not any(k in lower for k in CYCLE_KEYWORDS):
            continue
        ts = _parse_log_timestamp(line)
        if ts is None:
            continue
        if any(k in lower for k in ERROR_KEYWORDS):
            return {
                "last_run": ts.strftime("%Y-%m-%d %H:%M:%S UTC"),
                "last_result": "failed",
            }
        return {
            "last_run": ts.strftime("%Y-%m-%d %H:%M:%S UTC"),
            "last_result": "success",
        }
    return {"last_run": None, "last_result": None}


def evaluate_concert_watch_timer() -> dict[str, Any]:
    """Timer/service health for the concert watch oneshot + timer pair."""
    timer_svc = _check_service(
        "vulture-concert-watches timer",
        (CONCERT_TIMER_UNIT, CONCERT_TIMER_UNIT.replace(".timer", "")),
    )
    service_svc = _check_concert_service()
    next_run = _list_timer_next_run(CONCERT_TIMER_UNIT)

    journal = _journal_lines(CONCERT_SERVICE_UNIT.replace(".service", ""))
    journal_available = bool(journal)
    journal_run = _parse_journal_last_run(journal) if journal else {"last_run": None, "last_result": None}

    log_lines = _read_log_lines()
    log_run = _freshness_from_log(log_lines) if log_lines else {"last_run": None, "last_result": None}

    last_run = journal_run.get("last_run") or log_run.get("last_run")
    last_result = journal_run.get("last_result") or log_run.get("last_result")

    timer_healthy = timer_svc.unit is not None and timer_svc.active == "active"
    service_failed = service_svc.active == "failed"
    service_running = service_svc.active == "active"

    warning: str | None = None
    if timer_svc.unit is None or timer_svc.active in ("not found",):
        status = "FAIL"
        headline = "Concert watch timer not found"
        warning = "Concert watch timer missing/inactive"
    elif not timer_healthy:
        status = "FAIL"
        headline = f"Concert watch timer {timer_svc.active}"
        warning = "Concert watch timer missing/inactive"
    elif service_failed:
        status = "FAIL"
        headline = "Concert watch service failed"
        warning = "Concert watch service failed"
    elif service_running:
        status = "OK"
        headline = "Concert watch cycle in progress"
    elif next_run:
        status = "OK"
        headline = f"Concert watch timer active; next run {next_run}"
    elif last_result == "failed":
        status = "WARN"
        headline = "Last concert watch cycle reported errors"
        warning = "Last concert watch cycle reported errors"
    elif last_run:
        status = "OK"
        headline = f"Concert watch timer active; last run {last_run}"
    else:
        status = "UNKNOWN"
        headline = "Concert watch timer active; no recent cycle evidence"

    if service_svc.warning:
        warning = warning or service_svc.warning

    return {
        "status": status,
        "headline": headline,
        "warning": warning,
        "timer_active": timer_svc.active,
        "timer_enabled": timer_svc.enabled,
        "service_active": service_svc.active,
        "next_run": next_run,
        "last_run": last_run,
        "last_result": last_result,
        "journal_available": journal_available,
    }


def _cycle_summary_from_snapshot(snapshot: dict[str, Any] | None) -> dict[str, Any]:
    if not snapshot:
        return {}
    return {
        "checked_at": snapshot.get("checked_at"),
        "watches_checked": snapshot.get("watches_checked"),
        "events_found": snapshot.get("events_found"),
        "alerts_sent": snapshot.get("alerts_sent"),
        "errors": snapshot.get("errors") or [],
        "active_watch_count": snapshot.get("active_watch_count"),
        "paused_watch_count": snapshot.get("paused_watch_count"),
        "last_success_at": snapshot.get("last_success_at"),
        "last_error_at": snapshot.get("last_error_at"),
    }


def build_concert_card(db: dict[str, Any]) -> dict[str, Any]:
    """Plain-English Vulture Concerts card for the Nest overview."""
    timer = evaluate_concert_watch_timer()
    snapshot = _read_status_file()
    cycle = _cycle_summary_from_snapshot(snapshot)
    providers = _provider_configured()

    concert_counts = db.get("concert_counts") or {}
    active_watches = cycle.get("active_watch_count")
    paused_watches = cycle.get("paused_watch_count")
    if active_watches is None:
        active_watches = concert_counts.get("active", 0)
    if paused_watches is None:
        paused_watches = concert_counts.get("paused", 0)

    return {
        "status": timer["status"],
        "headline": timer["headline"],
        "warning": timer.get("warning"),
        "timer_active": timer.get("timer_active"),
        "timer_enabled": timer.get("timer_enabled"),
        "service_active": timer.get("service_active"),
        "next_run": timer.get("next_run"),
        "last_run": timer.get("last_run"),
        "last_result": timer.get("last_result"),
        "active_watches": active_watches,
        "paused_watches": paused_watches,
        "recent_events": concert_counts.get("recent_events", 0),
        "recent_alerts": concert_counts.get("recent_alerts", 0),
        "cycle": cycle,
        "providers": {
            "ticketmaster": providers["ticketmaster"],
            "seatgeek": providers["seatgeek"],
        },
    }
