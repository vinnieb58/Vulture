"""Read-only Vulture process and scheduler runtime visibility."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
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


def _parse_log_timestamp(line: str) -> datetime | None:
    # Assumes log timestamps are in UTC. If the host runs in a non-UTC timezone
    # the embedded HH:MM:SS is interpreted as UTC, which may cause a fixed offset
    # in stale-age math. As long as the host clock is UTC this is exact.
    m = re.search(r"(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2}:\d{2})", line)
    if m:
        raw = f"{m.group(1)} {m.group(2)}"
        try:
            return datetime.strptime(raw, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    return None


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


def _parse_timer_next_run(output: str, unit: str) -> str | None:
    """Extract the next-run timestamp from ``systemctl list-timers`` output.

    Returns the full ``"DayOfWeek YYYY-MM-DD HH:MM:SS TZ"`` string, or ``None``
    when the timer has no upcoming run (``n/a``) or when the output is truncated
    to fewer than four whitespace-separated tokens and a full timestamp cannot be
    recovered.  Returning ``None`` rather than a partial value (e.g. just the
    weekday) keeps downstream consumers honest about what they actually know.
    """
    for line in output.splitlines():
        if unit not in line:
            continue
        parts = line.split()
        if not parts or parts[0] in ("NEXT", "n/a"):
            return None
        # Expected format: "DayOfWeek YYYY-MM-DD HH:MM:SS TZ ..."
        # Require at least 4 parts and a well-formed date in position 1 so that
        # a line truncated to "Thu" or "Thu 2026-06-11" does not produce a
        # misleading single-word or partial result.
        if len(parts) >= 4 and re.match(r"\d{4}-\d{2}-\d{2}$", parts[1]):
            # Validate that parts[2] looks like a time (HH:MM:SS), not a unit name
            if re.match(r"\d{2}:\d{2}:\d{2}$", parts[2]):
                return " ".join(parts[0:4])
        # Cannot recover a full timestamp (truncated or unexpected format).
        return None
    return None


def _get_timer_next_run_show(unit: str) -> str | None:
    """Query next run via ``systemctl show --property=NextElapseUSecRealtime``.

    This machine-readable approach returns an integer microsecond timestamp
    that we convert to a human-readable string.  It is immune to the
    400-character truncation that can corrupt ``list-timers`` output when many
    system timers appear before the vulture entry.  Returns ``None`` when the
    property is unset (0) or when systemctl is unavailable.
    """
    ok, out = run_systemctl(
        ["show", unit, "--property=NextElapseUSecRealtime", "--value"],
        timeout=10.0,
    )
    if not ok or not out.strip():
        return None
    val = out.strip()
    if not val or val == "0":
        return None
    try:
        usec = int(val)
        if usec == 0:
            return None
        ts = datetime.fromtimestamp(usec / 1_000_000, tz=timezone.utc)
        return ts.strftime("%a %Y-%m-%d %H:%M:%S UTC")
    except (ValueError, OSError, OverflowError):
        return None


def _list_timer_next_run(unit: str = SCHEDULER_TIMER_UNIT) -> str | None:
    """Return the next scheduled run timestamp for *unit*, or ``None``.

    Tries three sources in order of reliability:

    1. ``systemctl show --property=NextElapseUSecRealtime --value`` — machine-readable
       integer microsecond timestamp; immune to output truncation.
    2. Unit-filtered ``systemctl list-timers <unit>`` — compact output (~250 chars)
       that fits within the 400-char ``_run_raw`` limit.
    3. Unfiltered ``systemctl list-timers --all`` — fallback for older systemd
       versions that do not accept a unit-name argument.
    """
    # Preferred: machine-readable property avoids all parsing fragility.
    result = _get_timer_next_run_show(unit)
    if result:
        return result

    # Unit-specific query: header + one data row ≈ 250 chars, safe from truncation.
    ok, out = run_systemctl(["list-timers", unit, "--all", "--no-pager"], timeout=10.0)
    if ok and out.strip():
        result = _parse_timer_next_run(out, unit)
        if result:
            return result

    # Fallback: older systemd may not accept a unit filter for list-timers.
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


def _log_mtime_age_minutes() -> int | None:
    """Return age of vulture.log in whole minutes, or None if unreadable."""
    if not LOG_PATH.exists():
        return None
    try:
        mtime_ts = datetime.fromtimestamp(LOG_PATH.stat().st_mtime, tz=timezone.utc)
        return int((datetime.now(timezone.utc) - mtime_ts).total_seconds() / 60)
    except OSError:
        return None


def _evaluate_scheduler_health(log_lines: list[str]) -> dict[str, Any]:
    """
    Scheduler health for oneshot service + timer architecture.

    Health rules (in priority order):
    - Timer missing/inactive → unhealthy
    - Service in failed state → unhealthy
    - Service currently active → running (hunt cycle in progress)
    - Timer active + recent journal/log success → fresh
    - Timer active + valid next_run scheduled → seen (OK)
    - Timer active + journal confirms stale activity + no next_run → stale (WARN)
    - Timer active + only log-file evidence of staleness (journal inaccessible/no
      keyword matches) → seen (OK); log-file mtime alone is not authoritative

    The systemd timer is the authoritative health heartbeat.  ``vulture.log``
    mtime is a weak fallback only — stale log evidence must NOT override a
    healthy timer when journal is inaccessible or has no keyword matches.
    Journal is preferred over the log file; ``stale`` is only emitted when
    journal provides positive confirmation of old activity AND no next_run is
    visible.
    """
    timer_svc = _check_service(
        "vulture-scheduler timer",
        (SCHEDULER_TIMER_UNIT, SCHEDULER_TIMER_UNIT.replace(".timer", "")),
    )
    service_svc = _check_scheduler_service()
    next_run = _list_timer_next_run()

    journal = _journal_lines(SCHEDULER_SERVICE_UNIT.replace(".service", ""))
    journal_available = bool(journal)

    # Prefer journal over log file for freshness detection.
    from_journal = _freshness_from_lines(journal, source="journal")
    from_log = _freshness_from_lines(log_lines, source="vulture.log")
    activity = from_journal or from_log

    last_success: str | None = None
    last_success_age_min: int | None = None
    last_success_source: str | None = None
    success_journal = _freshness_from_lines(journal, source="journal", success_only=True)
    success_log = _freshness_from_lines(log_lines, source="vulture.log", success_only=True)
    success = success_journal or success_log
    if success:
        last_success = success.get("last_success")
        last_success_age_min = success.get("last_success_age_min")
        last_success_source = "journal" if success is success_journal else "vulture.log"

    log_mtime_age_min = _log_mtime_age_minutes()

    timer_healthy = _timer_is_healthy(timer_svc)
    service_running = service_svc.active == "active"
    service_failed = service_svc.active == "failed"

    warning: str | None = None
    status = "unknown"
    detail_parts: list[str] = []
    scheduler_status_reason = "unknown"

    if timer_svc.unit is None or timer_svc.active in ("not found",):
        warning = "Scheduler timer missing/inactive"
        status = "unhealthy"
        detail_parts.append("timer not found")
        scheduler_status_reason = "timer not found"
    elif not timer_healthy:
        warning = "Scheduler timer missing/inactive"
        status = "unhealthy"
        detail_parts.append(f"timer {timer_svc.active}")
        scheduler_status_reason = f"timer {timer_svc.active}"
    elif service_failed:
        warning = "Scheduler service failed"
        status = "unhealthy"
        detail_parts.append(f"service {service_svc.active}")
        scheduler_status_reason = "service failed"
    elif service_running:
        status = "running"
        detail_parts.append("hunt cycle in progress")
        scheduler_status_reason = "service active"
    elif activity and activity.get("status") == "stale":
        # Timer is healthy; the systemd timer is the authoritative health source.
        # ``stale`` is only emitted when we have POSITIVE journal evidence of old
        # activity AND no next_run is scheduled.  If journal is inaccessible (no
        # lines returned) or has no keyword matches (from_journal is None), log-file
        # staleness alone must not trigger WARN — the timer being active is
        # sufficient to consider the scheduler seen/healthy.
        recent_journal_success = (
            success_journal is not None and success_journal.get("status") == "fresh"
        )
        if next_run or recent_journal_success:
            status = "seen"
            scheduler_status_reason = (
                "next_run scheduled" if next_run else "recent journal success"
            )
        elif from_journal is not None:
            # Journal IS accessible and keyword matches confirm stale activity.
            # This is positive stale evidence; no next_run → stale.
            status = "stale"
            scheduler_status_reason = "journal confirms stale activity; no next_run"
        else:
            # Only log-file evidence of staleness; journal has no keyword matches
            # (inaccessible or empty).  Timer is active — do not mark stale on
            # log-file evidence alone.
            status = "seen"
            scheduler_status_reason = (
                "log stale; journal inaccessible, timer active"
                if not journal_available
                else "log stale; no scheduler keywords in journal, timer active"
            )
        detail_parts.append(activity["detail"])
        # Annotate detail so the operator knows journal was not consulted.
        if status == "seen" and from_journal is None and not journal_available:
            detail_parts.append("journal unavailable")
    elif activity and activity.get("status") == "fresh":
        status = "fresh"
        detail_parts.append(activity["detail"])
        scheduler_status_reason = "recent activity in journal/log"
    elif next_run:
        # Timer healthy with a scheduled next run; absence of recent log activity
        # is normal between runs — report "seen" so the card shows OK.
        status = "seen"
        detail_parts.append("timer scheduled; no recent log activity")
        scheduler_status_reason = "next_run scheduled; no log activity"
    elif journal_available:
        status = "seen"
        detail_parts.append("scheduler journal entries present")
        scheduler_status_reason = "journal entries present"
    else:
        # Timer is healthy (active) but no log/journal evidence yet — no failure
        # signal, so treat as "seen" rather than leaving status as "unknown".
        status = "seen"
        scheduler_status_reason = "timer active; no log/journal evidence"
        if not journal_available:
            detail_parts.append("timer active; journal unavailable, no recent log activity")
        else:
            detail_parts.append("timer active; no recent scheduler activity")

    # Build rich detail: prepend timer state, append next_run, last_success, and log age.
    timer_enabled = timer_svc.enabled
    timer_state_str = f"timer {timer_svc.active}"
    if timer_enabled not in ("not configured", "unknown", None, ""):
        timer_state_str += f"/{timer_enabled}"

    # Prepend timer state for non-unhealthy statuses, unless the first part already
    # starts with "timer " (avoids "timer active/enabled · timer active; ...").
    if status not in ("unhealthy",) and not (
        detail_parts and detail_parts[0].startswith("timer ")
    ):
        detail_parts.insert(0, timer_state_str)

    if next_run:
        detail_parts.append(f"next run {next_run}")
    if last_success:
        src = f" ({last_success_source})" if last_success_source else ""
        detail_parts.append(f"last success {last_success}{src}")
    if log_mtime_age_min is not None and from_log is not None and from_log.get("status") == "stale":
        # Explicitly label the log mtime so it is clear it is not the primary source.
        detail_parts.append(f"vulture.log age {log_mtime_age_min} min")

    return {
        "status": status,
        "detail": " · ".join(detail_parts),
        "warning": warning,
        "timer": timer_svc,
        "service": service_svc,
        "timer_active": timer_svc.active,
        "timer_enabled": timer_enabled,
        "service_active": service_svc.active,
        "next_run": next_run,
        "last_success": last_success,
        "last_success_age_min": last_success_age_min,
        "last_success_source": last_success_source,
        "journal_available": journal_available,
        "log_mtime_age_minutes": log_mtime_age_min,
        "scheduler_status_reason": scheduler_status_reason,
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
        "last_success_source": scheduler.get("last_success_source"),
        "timer_active": scheduler.get("timer_active"),
        "timer_enabled": scheduler.get("timer_enabled"),
        "service_active": scheduler.get("service_active"),
        "journal_available": scheduler.get("journal_available"),
        "log_mtime_age_minutes": scheduler.get("log_mtime_age_minutes"),
        "scheduler_status_reason": scheduler.get("scheduler_status_reason"),
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
