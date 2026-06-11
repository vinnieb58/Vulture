"""Read-only Vulture process and scheduler runtime visibility."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

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


_NAIVE_TS_RE = re.compile(r"(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2}:\d{2})")
# journalctl -o short-iso prefixes lines with e.g. 2026-06-11T11:35:06-0500
_JOURNAL_TS_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:[.,]\d+)?([+-]\d{2}:?\d{2})?"
)
MAX_UTC_OFFSET = timedelta(hours=14)


def _parse_naive_timestamp(line: str) -> datetime | None:
    m = _NAIVE_TS_RE.search(line)
    if m:
        try:
            return datetime.strptime(f"{m.group(1)} {m.group(2)}", "%Y-%m-%d %H:%M:%S")
        except ValueError:
            pass
    return None


def _parse_journal_timestamp(line: str) -> datetime | None:
    """Parse a journalctl short-iso line honoring its explicit UTC offset."""
    m = _JOURNAL_TS_RE.match(line)
    if not m:
        return None
    raw, offset = m.group(1), m.group(2)
    try:
        if offset:
            return datetime.strptime(f"{raw}{offset.replace(':', '')}", "%Y-%m-%dT%H:%M:%S%z").astimezone(
                timezone.utc
            )
        return datetime.strptime(raw, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _infer_log_utc_offset(log_lines: list[str]) -> timedelta | None:
    """
    Infer the UTC offset of naive vulture.log timestamps.

    Python logging writes %(asctime)s in host-local time with no zone marker.
    The log file's mtime is the (timezone-unambiguous) moment the last line was
    written, so the gap between the last parseable line timestamp (read as UTC)
    and the mtime is the log's UTC offset. Rounded to 15 min to absorb jitter.
    """
    try:
        mtime = LOG_PATH.stat().st_mtime
    except OSError:
        return None
    for line in reversed(log_lines):
        naive = _parse_naive_timestamp(line)
        if naive is None:
            continue
        mtime_utc = datetime.fromtimestamp(mtime, tz=timezone.utc)
        delta = naive.replace(tzinfo=timezone.utc) - mtime_utc
        seconds = round(delta.total_seconds() / 900.0) * 900
        offset = timedelta(seconds=seconds)
        if abs(offset) > MAX_UTC_OFFSET:
            return None
        return offset
    return None


def _log_utc_offset(log_lines: list[str]) -> timedelta:
    """UTC offset of vulture.log timestamps: env override, mtime inference, else UTC."""
    tz_name = os.environ.get("VULTURE_LOG_TZ", "").strip()
    if tz_name:
        try:
            from zoneinfo import ZoneInfo

            offset = datetime.now(ZoneInfo(tz_name)).utcoffset()
            if offset is not None:
                return offset
        except Exception:
            pass
    inferred = _infer_log_utc_offset(log_lines)
    return inferred if inferred is not None else timedelta(0)


def _parse_log_timestamp(line: str, utc_offset: timedelta | None = None) -> datetime | None:
    """Parse a naive vulture.log timestamp into aware UTC using the given offset."""
    naive = _parse_naive_timestamp(line)
    if naive is None:
        return None
    return naive.replace(tzinfo=timezone.utc) - (utc_offset or timedelta(0))


def _freshness_from_lines(
    lines: list[str],
    *,
    source: str,
    success_only: bool = False,
    parse_ts: Callable[[str], datetime | None] | None = None,
) -> dict[str, Any] | None:
    parse = parse_ts or _parse_log_timestamp
    keywords = SUCCESS_KEYWORDS if success_only else ACTIVITY_KEYWORDS
    for line in reversed(lines):
        lower = line.lower()
        if not any(k in lower for k in keywords):
            continue
        ts = parse(line)
        if ts is None:
            continue
        age_min = max((datetime.now(timezone.utc) - ts).total_seconds() / 60, 0.0)
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


# systemctl list-timers timestamps: "Thu 2026-06-11 12:00:00 UTC" (zone optional)
_TIMER_TS_RE = re.compile(
    r"[A-Z][a-z]{2}\s+\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}(?:\s+(?:[A-Z]{2,5}|[+-]\d{2}:?\d{2}))?"
)

_EMPTY_TIMER_INFO: dict[str, str | None] = {
    "next_run": None,
    "next_left": None,
    "last_run": None,
    "last_passed": None,
}


def _parse_timer_row(output: str, unit: str) -> dict[str, str | None] | None:
    """Parse the NEXT/LEFT/LAST/PASSED columns of a systemctl list-timers row."""
    for line in output.splitlines():
        if unit not in line:
            continue
        head = line.split(unit, 1)[0]
        stamps = _TIMER_TS_RE.findall(head)
        next_run: str | None = None
        last_run: str | None = None
        if head.lstrip().lower().startswith(("n/a", "-")):
            last_run = stamps[0] if stamps else None
        else:
            next_run = stamps[0] if stamps else None
            last_run = stamps[1] if len(stamps) > 1 else None
        next_left: str | None = None
        if next_run:
            m = re.search(re.escape(next_run) + r"\s+(.+?)\s+left\b", head)
            if m:
                next_left = m.group(1).strip()
        last_passed: str | None = None
        if last_run:
            m = re.search(re.escape(last_run) + r"\s+(.+?)\s+ago\b", head)
            if m:
                last_passed = m.group(1).strip()
        return {
            "next_run": next_run,
            "next_left": next_left,
            "last_run": last_run,
            "last_passed": last_passed,
        }
    return None


def _list_timer_info(unit: str = SCHEDULER_TIMER_UNIT) -> dict[str, str | None]:
    ok, out = run_systemctl(["list-timers", "--all", "--no-pager"], timeout=10.0)
    if not ok or not out.strip():
        return dict(_EMPTY_TIMER_INFO)
    return _parse_timer_row(out, unit) or dict(_EMPTY_TIMER_INFO)


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

    - Timer state and its next-run schedule are the authoritative health signal
    - Service inactive/dead after success = idle between runs
    - Service active = hunt cycle in progress
    - Timer missing/inactive or service failed = unhealthy
    - Stale only when the timer has no upcoming run AND no recent cycle activity
    - Journal (offset-aware) is preferred over vulture.log for last-run recency;
      adapter warnings in the log never count as scheduler activity or failure
    """
    timer_svc = _check_service(
        "vulture-scheduler timer",
        (SCHEDULER_TIMER_UNIT, SCHEDULER_TIMER_UNIT.replace(".timer", "")),
    )
    service_svc = _check_scheduler_service()
    timer_info = _list_timer_info()

    next_run = timer_info.get("next_run")
    next_run_display: str | None = None
    if next_run:
        next_run_display = next_run
        if timer_info.get("next_left"):
            next_run_display = f"{next_run} (in {timer_info['next_left']})"

    journal = _journal_lines(SCHEDULER_SERVICE_UNIT.replace(".service", ""))
    log_offset = _log_utc_offset(log_lines)

    def parse_log_ts(line: str) -> datetime | None:
        return _parse_log_timestamp(line, utc_offset=log_offset)

    from_journal = _freshness_from_lines(
        journal, source="journal", parse_ts=_parse_journal_timestamp
    )
    from_log = _freshness_from_lines(log_lines, source="vulture.log", parse_ts=parse_log_ts)
    activity = from_journal or from_log

    last_success = None
    last_success_age_min = None
    success_journal = _freshness_from_lines(
        journal, source="journal", success_only=True, parse_ts=_parse_journal_timestamp
    )
    success_log = _freshness_from_lines(
        log_lines, source="vulture.log", success_only=True, parse_ts=parse_log_ts
    )
    success = success_journal or success_log
    if success:
        last_success = success.get("last_success")
        last_success_age_min = success.get("last_success_age_min")

    timer_healthy = _timer_is_healthy(timer_svc)
    service_running = service_svc.active == "active"
    service_failed = service_svc.active == "failed"
    fresh_activity = bool(activity and activity.get("status") == "fresh")

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
    elif fresh_activity:
        status = "fresh"
        detail_parts.append(activity["detail"])
    elif next_run:
        # Timer is active with an upcoming run: schedule is healthy even if the
        # log tail has no recent cycle lines (oneshot service idle between runs).
        status = "scheduled"
        if activity:
            detail_parts.append(activity["detail"])
        else:
            detail_parts.append("timer active; waiting for next run")
    else:
        status = "stale"
        warning = "Scheduler timer has no upcoming run and no recent cycle activity"
        if activity:
            detail_parts.append(activity["detail"])
        else:
            detail_parts.append("no recent scheduler activity; timer has no upcoming run")

    if next_run_display:
        detail_parts.append(f"next run {next_run_display}")
    if last_success:
        detail_parts.append(f"last success {last_success}")
    elif timer_info.get("last_run"):
        trigger = timer_info["last_run"]
        if timer_info.get("last_passed"):
            trigger = f"{trigger} ({timer_info['last_passed']} ago)"
        detail_parts.append(f"timer last triggered {trigger}")

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
