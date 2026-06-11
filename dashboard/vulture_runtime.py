"""Read-only Vulture process and scheduler runtime visibility."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from host_commands import run_host_command, run_systemctl, systemctl_is_active, systemctl_is_enabled
from host_status import ServiceStatus, _check_service, _normalize_unit_state, _resolve_unit
from subprocess_util import run_command

LOG_PATH = Path(os.environ.get("VULTURE_LOG_PATH", "/app/logs/vulture.log"))
SCHEDULER_FRESH_MINUTES = int(os.environ.get("DASHBOARD_SCHEDULER_FRESH_MINUTES", "30"))
SCHEDULER_TIMER_UNIT = os.environ.get("VULTURE_SCHEDULER_TIMER", "vulture-scheduler.timer")
SCHEDULER_SERVICE_UNIT = os.environ.get("VULTURE_SCHEDULER_SERVICE", "vulture-scheduler.service")

SUCCESS_KEYWORDS = (
    "hunt cycle completed",
    "done hunt",
)
ACTIVITY_KEYWORDS = (
    "starting hunt",
    "done hunt",
    "hunt cycle",
    "scheduler",
    "starting vulture hunt cycle",
)


@dataclass
class ProcessMatch:
    label: str
    running: bool
    detail: str
    warning: str | None = None


def _service_active(svc: ServiceStatus) -> bool:
    return svc.active == "active"


def _systemd_detail(svc: ServiceStatus) -> str | None:
    if not svc.unit:
        return None
    if svc.active in ("unknown", "not found") or "command not found" in svc.active:
        return None
    return f"systemd: {svc.active} ({svc.enabled})"


def _format_runtime_detail(svc: ServiceStatus, proc: bool, proc_detail: str) -> str:
    parts: list[str] = []
    systemd = _systemd_detail(svc)
    if systemd:
        parts.append(systemd)
    if proc:
        parts.append(f"process: {proc_detail[:120]}")
    return " · ".join(parts) if parts else "not detected"


def _pgrep_running(pattern: str) -> tuple[bool, str]:
    ok, out = run_host_command(["pgrep", "-af", pattern], timeout=8.0)
    if not ok or not out.strip():
        return False, "not running"
    lines = [line.strip() for line in out.splitlines() if line.strip()]
    # Prefer production systemd/python paths over tmux wrappers.
    preferred = [
        ln
        for ln in lines
        if "/projects/vulture/" in ln or "systemd" in ln or ".venv/bin/python" in ln
    ]
    pick = preferred[0] if preferred else lines[0]
    return True, pick[:160]


def _process_running(pattern: str) -> tuple[bool, str]:
    found, detail = _pgrep_running(pattern)
    if found:
        return True, detail

    ok, out = run_host_command(["ps", "aux"], timeout=8.0)
    if not ok:
        ok, out = run_command(["ps", "aux"], timeout=8.0)
    if not ok:
        return False, out or "ps unavailable"

    matches: list[str] = []
    for line in out.splitlines():
        if pattern in line and "grep" not in line:
            matches.append(line.strip())
    if matches:
        return True, matches[0][:160]
    return False, "not running"


def _tmux_sessions() -> tuple[list[str], str | None]:
    ok, out = run_host_command(["tmux", "ls"], timeout=5.0)
    if not ok:
        ok, out = run_command(["tmux", "ls"], timeout=5.0)
    if not ok:
        if "command not found" in out:
            return [], None
        if "no server running" in out.lower() or "error connecting" in out.lower():
            return [], None
        return [], out
    sessions = [line.split(":")[0].strip() for line in out.splitlines() if line.strip()]
    return sessions, None


def _log_mtime() -> tuple[str | None, str | None]:
    if not LOG_PATH.exists():
        return None, f"Log not found at {LOG_PATH}"
    try:
        mtime = datetime.fromtimestamp(LOG_PATH.stat().st_mtime, tz=timezone.utc)
        return mtime.strftime("%Y-%m-%d %H:%M:%S UTC"), None
    except OSError as exc:
        return None, str(exc)


def _journal_lines(unit: str, limit: int = 40) -> list[str]:
    ok, out = run_host_command(
        [
            "journalctl",
            "-u",
            unit,
            "-n",
            str(limit),
            "--no-pager",
            "--no-hostname",
            "-o",
            "short-iso",
        ],
        timeout=12.0,
    )
    if ok and out.strip():
        return [line for line in out.splitlines() if line.strip()]
    return []


_TIMESTAMP_RE = re.compile(
    r"(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2}:\d{2})(?:[.,]\d+)?\s*([+-]\d{2}:?\d{2}|Z)?"
)


def _parse_log_timestamp(line: str) -> datetime | None:
    """Parse the first ISO-ish timestamp in *line* into an aware UTC datetime.

    Handles both naive application log lines (``2026-06-11 11:35:06,318``),
    assumed UTC, and journal ``short-iso`` lines that carry an explicit offset
    (``2026-06-05T21:55:07-0500``). Normalising everything to UTC keeps the
    stale math from mixing naive/aware or local/UTC datetimes.
    """
    m = _TIMESTAMP_RE.search(line)
    if not m:
        return None
    date_s, time_s, tz_s = m.group(1), m.group(2), m.group(3)
    try:
        dt = datetime.strptime(f"{date_s} {time_s}", "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
    if tz_s and tz_s not in ("Z", "z"):
        digits = tz_s.replace(":", "")
        sign = -1 if digits[0] == "-" else 1
        offset = timedelta(hours=int(digits[1:3]), minutes=int(digits[3:5])) * sign
        return dt.replace(tzinfo=timezone(offset)).astimezone(timezone.utc)
    return dt.replace(tzinfo=timezone.utc)


def _format_relative(target: datetime, now: datetime) -> str:
    """Human relative duration like ``in 27 min`` / ``5h 12m ago``."""
    delta = (target - now).total_seconds()
    future = delta >= 0
    minutes = int(abs(delta) // 60)
    if minutes < 60:
        rel = f"{minutes} min"
    elif minutes < 1440:
        rel = f"{minutes // 60}h {minutes % 60}m"
    else:
        rel = f"{minutes // 1440}d {(minutes % 1440) // 60}h"
    return f"in {rel}" if future else f"{rel} ago"


def _freshness_from_lines(
    lines: list[str],
    *,
    source: str,
    success_only: bool = False,
) -> dict[str, Any] | None:
    keywords = SUCCESS_KEYWORDS if success_only else ACTIVITY_KEYWORDS
    for line in reversed(lines):
        lower = line.lower()
        if not any(k in lower for k in keywords):
            continue
        ts = _parse_log_timestamp(line)
        if ts is None:
            continue
        age_min = (datetime.now(timezone.utc) - ts).total_seconds() / 60
        if age_min <= SCHEDULER_FRESH_MINUTES:
            return {
                "status": "fresh",
                "detail": f"Last scheduler activity ~{int(age_min)} min ago ({source})",
                "warning": None,
                "last_success": ts.strftime("%Y-%m-%d %H:%M:%S UTC"),
                "last_success_age_min": int(age_min),
            }
        return {
            "status": "stale",
            "detail": f"Last scheduler activity ~{int(age_min)} min ago ({source})",
            "warning": f"No scheduler activity within {SCHEDULER_FRESH_MINUTES} min",
            "last_success": ts.strftime("%Y-%m-%d %H:%M:%S UTC"),
            "last_success_age_min": int(age_min),
        }
    return None


_NEXT_RUN_RE = re.compile(
    r"(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2}:\d{2})(?:\s+([A-Za-z]{2,5}|[+-]\d{2}:?\d{2}))?"
)


def _parse_timer_next_run(output: str, unit: str) -> str | None:
    """Extract the full NEXT timestamp for *unit* from ``systemctl list-timers``.

    ``list-timers`` rows look like::

        NEXT                        LEFT       LAST                        ... UNIT
        Thu 2026-06-11 17:00:00 UTC 26min left Thu 2026-06-11 11:00:00 UTC ... vulture-scheduler.timer

    The previous implementation returned ``parts[0]`` (the weekday, e.g.
    ``Thu``), which is useless on the dashboard. We instead return the full
    ``YYYY-MM-DD HH:MM:SS TZ`` of the NEXT column.
    """
    for line in output.splitlines():
        if unit not in line:
            continue
        if line.strip().lower().startswith("n/a"):
            return None
        m = _NEXT_RUN_RE.search(line)
        if m:
            date_s, time_s, tz_s = m.group(1), m.group(2), m.group(3)
            return f"{date_s} {time_s}" + (f" {tz_s}" if tz_s else "")
    return None


def _list_timer_next_run(unit: str = SCHEDULER_TIMER_UNIT) -> str | None:
    ok, out = run_systemctl(["list-timers", "--all", "--no-pager"], timeout=10.0)
    if not ok or not out.strip():
        return None
    return _parse_timer_next_run(out, unit)


def _check_scheduler_service() -> ServiceStatus:
    """Oneshot scheduler service: inactive/dead between runs is expected."""
    unit = _resolve_unit((SCHEDULER_SERVICE_UNIT, SCHEDULER_SERVICE_UNIT.replace(".service", "")))
    if unit is None:
        return ServiceStatus(
            label="vulture-scheduler service",
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
        warning = "vulture-scheduler service: systemctl unavailable"
    elif active == "failed":
        warning = "Scheduler service failed"

    return ServiceStatus(
        label="vulture-scheduler service",
        unit=unit,
        active=active,
        enabled=enabled,
        warning=warning,
    )


def _timer_is_healthy(timer_svc: ServiceStatus) -> bool:
    return timer_svc.unit is not None and timer_svc.active == "active"


def _evaluate_scheduler_health(log_lines: list[str]) -> dict[str, Any]:
    """
    Scheduler health for the oneshot service + timer architecture.

    The systemd timer is the authoritative signal for schedule health, so the
    sources are weighted deliberately:

    - **Schedule health**: ``vulture-scheduler.timer`` state + its upcoming
      ``NEXT`` run (``systemctl list-timers``). An active timer with an upcoming
      run is healthy regardless of how chatty ``vulture.log`` has been.
    - **Last cycle run**: explicit cycle-complete lines from
      ``journalctl -u vulture-scheduler.service`` (falling back to the same
      keywords in ``vulture.log``).
    - **General log activity**: informational only. Adapter/no-result warnings
      such as "zero model slugs" never match the scheduler keywords and must
      never be treated as scheduler activity *or* failure.

    Status values: ``running`` (cycle in progress), ``fresh`` (timer healthy /
    scheduled), ``stale`` (timer active but nothing scheduled and no recent
    run), ``unhealthy`` (timer missing/inactive or service failed).
    """
    timer_svc = _check_service(
        "vulture-scheduler timer",
        (SCHEDULER_TIMER_UNIT, SCHEDULER_TIMER_UNIT.replace(".timer", "")),
    )
    service_svc = _check_scheduler_service()
    next_run = _list_timer_next_run()
    next_run_dt = _parse_log_timestamp(next_run) if next_run else None

    journal = _journal_lines(SCHEDULER_SERVICE_UNIT.replace(".service", ""))

    # General activity (informational): most recent scheduler-keyword line.
    activity = _freshness_from_lines(journal, source="journal") or _freshness_from_lines(
        log_lines, source="vulture.log"
    )

    # Last successful cycle: authoritative from explicit cycle-complete lines.
    success = _freshness_from_lines(
        journal, source="journal", success_only=True
    ) or _freshness_from_lines(log_lines, source="vulture.log", success_only=True)
    last_success = success.get("last_success") if success else None
    last_success_age_min = success.get("last_success_age_min") if success else None

    timer_healthy = _timer_is_healthy(timer_svc)
    service_running = service_svc.active == "active"
    service_failed = service_svc.active == "failed"
    has_upcoming_run = bool(next_run)
    recent_success = (
        last_success_age_min is not None and last_success_age_min <= SCHEDULER_FRESH_MINUTES
    )

    warning: str | None = None
    status = "unknown"
    detail_parts: list[str] = []

    if timer_svc.unit is None or timer_svc.active in ("not found",):
        warning = "Scheduler timer missing/inactive"
        status = "unhealthy"
        detail_parts.append("timer not found")
    elif not timer_healthy:
        warning = "Scheduler timer missing/inactive"
        status = "unhealthy"
        detail_parts.append(f"timer {timer_svc.active}")
    elif service_failed:
        warning = "Scheduler service failed"
        status = "unhealthy"
        detail_parts.append(f"service {service_svc.active}")
    elif service_running:
        status = "running"
        detail_parts.append("hunt cycle in progress")
    elif has_upcoming_run or recent_success:
        # Timer is active and either has an upcoming run or recently completed a
        # cycle -> healthy. Old log keyword activity is purely informational here.
        status = "fresh"
        if has_upcoming_run:
            detail_parts.append("timer active; run scheduled")
        else:
            detail_parts.append("timer active; recent cycle success")
    else:
        # Timer is active but systemd reports no upcoming run and we have no
        # recent successful cycle -> genuinely stale / wedged.
        status = "stale"
        warning = "Scheduler timer active but no upcoming run scheduled"
        detail_parts.append("timer active; no upcoming run, no recent cycle success")

    # Informational activity note (never downgrades a timer-healthy status).
    if activity:
        detail_parts.append(activity["detail"])

    next_run_relative = None
    if next_run:
        if next_run_dt is not None:
            next_run_relative = _format_relative(next_run_dt, datetime.now(timezone.utc))
            detail_parts.append(f"next run {next_run} ({next_run_relative})")
        else:
            detail_parts.append(f"next run {next_run}")
    if last_success:
        detail_parts.append(f"last success {last_success}")

    return {
        "status": status,
        "detail": " · ".join(detail_parts),
        "warning": warning,
        "timer": timer_svc,
        "service": service_svc,
        "timer_active": timer_svc.active,
        "service_active": service_svc.active,
        "next_run": next_run,
        "next_run_relative": next_run_relative,
        "last_success": last_success,
        "last_success_age_min": last_success_age_min,
    }


def _scheduler_freshness(log_lines: list[str]) -> dict[str, Any]:
    """Backward-compatible freshness view backed by timer-aware health."""
    return _evaluate_scheduler_health(log_lines)


def get_vulture_runtime(log_lines: list[str] | None = None) -> dict[str, Any]:
    bot_svc = _check_service("vulture-bot", ("vulture-bot.service", "vulture-bot"))
    scheduler = _evaluate_scheduler_health(log_lines or [])

    bot_proc, bot_detail = _process_running("discord_bot.py")
    sched_proc, sched_detail = _process_running("main.py")

    sessions, tmux_warn = _tmux_sessions()
    log_mtime, log_warn = _log_mtime()
    freshness = {
        "status": scheduler["status"],
        "detail": scheduler["detail"],
        "warning": scheduler["warning"],
        "next_run": scheduler.get("next_run"),
        "next_run_relative": scheduler.get("next_run_relative"),
        "last_success": scheduler.get("last_success"),
        "timer_active": scheduler.get("timer_active"),
        "service_active": scheduler.get("service_active"),
    }

    warnings: list[str] = []
    if tmux_warn:
        warnings.append(tmux_warn)
    if log_warn:
        warnings.append(log_warn)
    if scheduler.get("warning"):
        warnings.append(str(scheduler["warning"]))
    if scheduler["service"].warning:
        warnings.append(scheduler["service"].warning)

    bot_running = _service_active(bot_svc) or bot_proc
    sched_running = scheduler["service"].active == "active" or sched_proc
    sched_ok = _timer_is_healthy(scheduler["timer"]) and scheduler["service"].active != "failed"

    scheduler_detail_parts = []
    timer_detail = _systemd_detail(scheduler["timer"])
    service_detail = _systemd_detail(scheduler["service"])
    if timer_detail:
        scheduler_detail_parts.append(timer_detail)
    if service_detail:
        scheduler_detail_parts.append(service_detail)
    if sched_proc:
        scheduler_detail_parts.append(f"process: {sched_detail[:120]}")
    if scheduler.get("next_run"):
        scheduler_detail_parts.append(f"next: {scheduler['next_run']}")
    scheduler_detail = " · ".join(scheduler_detail_parts) if scheduler_detail_parts else "not detected"

    scheduler_warning = None
    if not sched_ok:
        scheduler_warning = scheduler.get("warning") or "Scheduler timer missing/inactive"
    elif sched_running:
        scheduler_warning = None
    elif scheduler.get("warning"):
        scheduler_warning = str(scheduler["warning"])

    processes = [
        ProcessMatch(
            label="Discord bot",
            running=bot_running,
            detail=_format_runtime_detail(bot_svc, bot_proc, bot_detail),
            warning=None if bot_running else "Bot not detected",
        ),
        ProcessMatch(
            label="Scheduler",
            running=sched_running or sched_ok,
            detail=scheduler_detail,
            warning=scheduler_warning,
        ),
    ]

    return {
        "systemd": {
            "bot": bot_svc,
            "scheduler_timer": scheduler["timer"],
            "scheduler_service": scheduler["service"],
            # Backward-compatible alias for templates/tests expecting scheduler key.
            "scheduler": scheduler["timer"],
        },
        "processes": processes,
        "tmux_sessions": sessions,
        "log_mtime": log_mtime,
        "scheduler_freshness": freshness,
        "scheduler_health": scheduler,
        "warnings": warnings,
    }
