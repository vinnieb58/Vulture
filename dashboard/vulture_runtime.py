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


def _offset_to_tz(token: str) -> timezone | None:
    """Convert an offset token like ``+0000``/``-05:00``/``Z`` to a tzinfo."""
    if token in ("Z", "z"):
        return timezone.utc
    cleaned = token.replace(":", "")
    if len(cleaned) != 5 or cleaned[0] not in "+-":
        return None
    sign = 1 if cleaned[0] == "+" else -1
    hours = int(cleaned[1:3])
    minutes = int(cleaned[3:5])
    return timezone(sign * timedelta(hours=hours, minutes=minutes))


def _parse_log_timestamp(line: str) -> datetime | None:
    """Parse the first timestamp on a log/journal line as a tz-aware UTC datetime.

    journalctl ``short-iso`` lines carry an explicit offset (e.g. ``-0500``); when
    present it is honoured and normalised to UTC so stale math never mixes naive
    local and UTC datetimes. Plain ``vulture.log`` lines have no offset and are
    assumed to be UTC (Vulture logs in UTC).
    """
    m = re.search(
        r"(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2}:\d{2})(?:[.,]\d+)?\s*([+-]\d{2}:?\d{2}|Z)?",
        line,
    )
    if not m:
        return None
    raw = f"{m.group(1)} {m.group(2)}"
    try:
        dt = datetime.strptime(raw, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
    tz = _offset_to_tz(m.group(3)) if m.group(3) else timezone.utc
    if tz is None:
        tz = timezone.utc
    return dt.replace(tzinfo=tz).astimezone(timezone.utc)


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


def _parse_timer_datetime(value: str | None) -> datetime | None:
    """Parse a ``systemctl list-timers`` NEXT/LAST cell into a tz-aware UTC datetime.

    Cells look like ``Thu 2026-06-11 17:00:00 UTC``. UTC and numeric offsets are
    honoured; other timezone abbreviations are assumed to be UTC so the value is
    always tz-aware (Raven runs in UTC).
    """
    if not value:
        return None
    m = re.search(
        r"(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}:\d{2})(?:\s+([A-Za-z]{1,5}|[+-]\d{2}:?\d{2}))?",
        value,
    )
    if not m:
        return None
    raw = f"{m.group(1)} {m.group(2)}"
    try:
        dt = datetime.strptime(raw, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
    token = m.group(3)
    tz = timezone.utc
    if token and token[0] in "+-":
        tz = _offset_to_tz(token) or timezone.utc
    return dt.replace(tzinfo=tz).astimezone(timezone.utc)


def _parse_timer_schedule(output: str, unit: str) -> dict[str, str | None]:
    """Extract the NEXT / LEFT / LAST columns for ``unit`` from list-timers output.

    Columns are separated by runs of 2+ spaces while each cell (e.g. the NEXT
    timestamp ``Thu 2026-06-11 17:00:00 UTC``) contains single spaces, so the
    whole cell is preserved rather than just the leading weekday token.
    """
    empty = ("", "n/a", "-")
    for line in output.splitlines():
        if unit not in line:
            continue
        cols = re.split(r"\s{2,}", line.strip())
        next_run = cols[0].strip() if len(cols) >= 1 else ""
        left = cols[1].strip() if len(cols) >= 2 else ""
        last_run = cols[2].strip() if len(cols) >= 3 else ""
        return {
            "next_run": None if next_run in empty else next_run,
            "left": None if left in empty else left,
            "last_run": None if last_run in empty else last_run,
        }
    return {"next_run": None, "left": None, "last_run": None}


def _list_timer_next_run(unit: str = SCHEDULER_TIMER_UNIT) -> str | None:
    ok, out = run_systemctl(["list-timers", "--all", "--no-pager"], timeout=10.0)
    if not ok or not out.strip():
        return None
    return _parse_timer_schedule(out, unit).get("next_run")


def _relative_phrase(target: datetime, now: datetime) -> str:
    """Human relative duration between ``now`` and ``target`` (both tz-aware)."""
    seconds = (target - now).total_seconds()
    future = seconds >= 0
    seconds = abs(seconds)
    if seconds < 90 * 60:
        magnitude = f"{int(round(seconds / 60))} min"
    elif seconds < 36 * 3600:
        magnitude = f"{seconds / 3600:.1f} hr"
    else:
        magnitude = f"{seconds / 86400:.1f} days"
    return f"in {magnitude}" if future else f"{magnitude} ago"


def _format_next_run(next_run: str | None, now: datetime) -> str | None:
    """Return a display string for the next run with a relative hint when possible."""
    if not next_run:
        return None
    dt = _parse_timer_datetime(next_run)
    if dt is None:
        return next_run
    return f"{next_run} ({_relative_phrase(dt, now)})"


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
    Scheduler health for oneshot service + timer architecture.

    - Timer active = heartbeat OK
    - Service inactive/dead after success = idle between runs
    - Service active = hunt cycle in progress
    - Timer missing/inactive = unhealthy
    """
    now = datetime.now(timezone.utc)
    timer_svc = _check_service(
        "vulture-scheduler timer",
        (SCHEDULER_TIMER_UNIT, SCHEDULER_TIMER_UNIT.replace(".timer", "")),
    )
    service_svc = _check_scheduler_service()
    next_run = _list_timer_next_run()
    next_run_display = _format_next_run(next_run, now)
    # systemctl only lists a NEXT cell when the timer has an upcoming activation,
    # so its presence is the authoritative signal that the schedule is healthy.
    has_upcoming_run = next_run is not None

    journal = _journal_lines(SCHEDULER_SERVICE_UNIT.replace(".service", ""))
    from_journal = _freshness_from_lines(journal, source="journal")
    from_log = _freshness_from_lines(log_lines, source="vulture.log")
    activity = from_journal or from_log

    last_success = None
    last_success_age_min = None
    success_journal = _freshness_from_lines(journal, source="journal", success_only=True)
    success_log = _freshness_from_lines(log_lines, source="vulture.log", success_only=True)
    success = success_journal or success_log
    if success:
        last_success = success.get("last_success")
        last_success_age_min = success.get("last_success_age_min")

    timer_healthy = _timer_is_healthy(timer_svc)
    service_running = service_svc.active == "active"
    service_failed = service_svc.active == "failed"

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
    elif has_upcoming_run:
        # Timer is active with an upcoming run scheduled -> the schedule itself is
        # healthy. Stale log tails and ordinary adapter warnings (e.g. "zero model
        # slugs") are informational only and must not flag the scheduler.
        status = "fresh"
        detail_parts.append(activity["detail"] if activity else "timer active; next run scheduled")
    elif activity and activity.get("status") == "fresh":
        status = "fresh"
        detail_parts.append(activity["detail"])
    elif activity and activity.get("status") == "stale":
        # Timer active but no upcoming run *and* the last cycle is old -> stale.
        status = "stale"
        warning = "Scheduler has no upcoming run and last cycle is stale"
        detail_parts.append(activity["detail"])
    elif journal:
        status = "seen"
        detail_parts.append("scheduler journal entries present")
    else:
        status = "stale"
        warning = "Scheduler has no upcoming run and no recent activity"
        detail_parts.append("no upcoming run and no recent scheduler activity")

    if next_run_display:
        detail_parts.append(f"next run {next_run_display}")
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
        "next_run": next_run_display,
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
