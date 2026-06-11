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
# Tokens that indicate scheduler / hunt-cycle activity, NOT generic adapter
# warnings.  Lines like "[WARNING] Swappa: zero model slugs ..." intentionally
# do not match anything here so they cannot make the scheduler look stale.
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


_TS_WITH_TZ = re.compile(
    r"(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2}:\d{2})(?:[.,]\d+)?\s*([+-])(\d{2}):?(\d{2})"
)
_TS_NAIVE = re.compile(r"(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2}:\d{2})")


def _parse_log_timestamp(line: str) -> datetime | None:
    """Parse the first ISO-8601 timestamp on a log line into a tz-aware UTC datetime.

    Lines with explicit offsets (e.g. journalctl `short-iso` output
    ``2026-06-05T21:55:07-0500 ...``) are converted to UTC.  Naive timestamps
    (e.g. vulture.log ``2026-06-11 11:35:06,318 [WARNING] ...``) are assumed to
    be UTC because that is what Vulture writes; the important property is that
    every datetime returned by this helper is timezone-aware so stale-math
    against ``datetime.now(timezone.utc)`` never mixes naive/aware values.
    """
    m = _TS_WITH_TZ.search(line)
    if m:
        date, time_, sign, hh, mm = m.groups()
        try:
            naive = datetime.strptime(f"{date} {time_}", "%Y-%m-%d %H:%M:%S")
        except ValueError:
            naive = None
        if naive is not None:
            sign_mul = 1 if sign == "+" else -1
            delta = timedelta(hours=int(hh), minutes=int(mm)) * sign_mul
            return naive.replace(tzinfo=timezone(delta)).astimezone(timezone.utc)

    m = _TS_NAIVE.search(line)
    if m:
        try:
            return datetime.strptime(
                f"{m.group(1)} {m.group(2)}", "%Y-%m-%d %H:%M:%S"
            ).replace(tzinfo=timezone.utc)
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
    """Extract the NEXT column for ``unit`` from ``systemctl list-timers`` output.

    ``list-timers`` is a column-aligned table whose columns can each contain
    spaces (``NEXT`` is e.g. ``Thu 2026-06-11 17:30:00 UTC`` and ``LEFT`` is
    e.g. ``50min left``).  The legacy implementation split on any whitespace
    and returned ``parts[0]`` which is just ``"Thu"`` — that's why the
    dashboard previously displayed ``Next run Thu``.  Columns are always
    separated by two or more spaces, so split accordingly and reassemble the
    NEXT column with the optional LEFT (``in 50min``) suffix for readability.
    """
    for line in output.splitlines():
        if unit not in line:
            continue
        cols = [c.strip() for c in re.split(r"\s{2,}", line.strip()) if c.strip()]
        if not cols or cols[0] in ("NEXT", "n/a", "-"):
            # Header row, or timer with no scheduled next run.
            if cols and cols[0] in ("n/a", "-"):
                return None
            continue
        next_run = cols[0]
        left = cols[1] if len(cols) > 1 else ""
        left = re.sub(r"\s*left\s*$", "", left).strip()
        if left and left not in ("n/a", "-"):
            return f"{next_run} (in {left})"
        return next_run
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
    Scheduler health for oneshot service + timer architecture.

    The dashboard distinguishes three different signals:

    * **Scheduler timer status** – authoritative for *schedule health*.  A
      healthy ``vulture-scheduler.timer`` with a scheduled next run is the
      definitive indicator that the scheduler will keep firing.  This is the
      ONLY signal allowed to raise a warning on the scheduler card.
    * **Scheduler cycle success** – ``hunt cycle completed`` / ``done hunt``
      lines, preferentially read from
      ``journalctl -u vulture-scheduler.service`` (single-purpose, never
      polluted by adapter chatter) and only falling back to ``vulture.log``.
      Used for the "last success" timestamp; informational only.
    * **General Vulture log activity** – everything else in ``vulture.log``.
      Reported as informational context but never used to mark the
      scheduler stale, so adapter warnings like
      ``Swappa: zero model slugs ...`` cannot trip the scheduler card.
    """
    timer_svc = _check_service(
        "vulture-scheduler timer",
        (SCHEDULER_TIMER_UNIT, SCHEDULER_TIMER_UNIT.replace(".timer", "")),
    )
    service_svc = _check_scheduler_service()
    next_run = _list_timer_next_run()

    journal = _journal_lines(SCHEDULER_SERVICE_UNIT.replace(".service", ""))
    # Prefer authoritative signals (service journal) over noisy vulture.log
    # tails for both general activity and explicit success markers.
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
    elif activity and activity.get("status") == "stale":
        # Timer is healthy; log staleness is informational only, never a
        # warning.  Only timer / service state can flip the scheduler card to
        # WARN or FAIL (see _compute_vulture_card).
        status = "stale"
        detail_parts.append(activity["detail"])
    elif activity and activity.get("status") == "fresh":
        status = "fresh"
        detail_parts.append(activity["detail"])
    elif journal:
        status = "seen"
        detail_parts.append("scheduler journal entries present")
    else:
        detail_parts.append("no recent scheduler lines in journal or log tail")

    if next_run:
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
        "timer_healthy": timer_healthy,
        "next_run": next_run,
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
        "last_success_age_min": scheduler.get("last_success_age_min"),
        "timer_active": scheduler.get("timer_active"),
        "timer_healthy": scheduler.get("timer_healthy", False),
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
