"""Read-only Vulture process and scheduler runtime visibility."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from host_commands import run_host_command, run_systemctl, systemctl_is_active, systemctl_is_enabled
from host_status import ServiceStatus, _check_service, _normalize_unit_state, _resolve_unit
from subprocess_util import run_command

HOST_ROOT = Path(os.environ.get("DASHBOARD_HOST_ROOT", "/host/root"))
LOG_PATH = Path(os.environ.get("VULTURE_LOG_PATH", "/app/logs/vulture.log"))
SCHEDULER_FRESH_MINUTES = int(os.environ.get("DASHBOARD_SCHEDULER_FRESH_MINUTES", "30"))
SCHEDULER_TIMER_UNIT = os.environ.get("VULTURE_SCHEDULER_TIMER", "vulture-scheduler.timer")
SCHEDULER_SERVICE_UNIT = os.environ.get("VULTURE_SCHEDULER_SERVICE", "vulture-scheduler.service")

SUCCESS_KEYWORDS = (
    "hunt cycle completed",
    "done hunt",
    "deactivated successfully",
    "finished",
    "succeeded",
)
ACTIVITY_KEYWORDS = (
    "starting hunt",
    "done hunt",
    "hunt cycle",
    "starting vulture hunt cycle",
    "deactivated successfully",
    "finished",
    "succeeded",
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


@lru_cache(maxsize=1)
def _local_log_timezone() -> timezone | ZoneInfo:
    """Best-effort host-local timezone for naive Vulture file log timestamps."""
    for name in (os.environ.get("DASHBOARD_LOG_TIMEZONE"), os.environ.get("TZ")):
        if not name:
            continue
        try:
            return ZoneInfo(name)
        except ZoneInfoNotFoundError:
            continue

    for path in (HOST_ROOT / "etc/timezone", Path("/etc/timezone")):
        try:
            name = path.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if name:
            try:
                return ZoneInfo(name)
            except ZoneInfoNotFoundError:
                pass

    for path in (HOST_ROOT / "etc/localtime", Path("/etc/localtime")):
        try:
            if not path.is_symlink():
                continue
            target = os.readlink(path)
        except OSError:
            continue
        marker = "/zoneinfo/"
        if marker in target:
            name = target.split(marker, 1)[1]
            try:
                return ZoneInfo(name)
            except ZoneInfoNotFoundError:
                pass

    return timezone.utc


def _format_utc(ts: datetime) -> str:
    return ts.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _parse_log_timestamp(line: str, *, default_tz: timezone | ZoneInfo | None = None) -> datetime | None:
    iso = re.search(
        r"(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2}:\d{2})(Z|[+-]\d{2}:?\d{2})",
        line,
    )
    if iso:
        raw = f"{iso.group(1)}T{iso.group(2)}{iso.group(3)}"
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        try:
            return datetime.strptime(raw, "%Y-%m-%dT%H:%M:%S%z").astimezone(timezone.utc)
        except ValueError:
            pass

    m = re.search(r"(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2}:\d{2})(?:,\d+)?", line)
    if m:
        raw = f"{m.group(1)} {m.group(2)}"
        try:
            tz = default_tz or _local_log_timezone()
            return datetime.strptime(raw, "%Y-%m-%d %H:%M:%S").replace(tzinfo=tz).astimezone(
                timezone.utc
            )
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
                "last_success": _format_utc(ts),
                "last_success_age_min": int(age_min),
            }
        return {
            "status": "stale",
            "detail": f"Last scheduler activity ~{int(age_min)} min ago ({source})",
            "warning": f"No scheduler activity within {SCHEDULER_FRESH_MINUTES} min",
            "last_success": _format_utc(ts),
            "last_success_age_min": int(age_min),
        }
    return None


def _consume_timer_timestamp(tokens: list[str], start: int) -> tuple[str | None, int]:
    if start >= len(tokens):
        return None, start
    if tokens[start] == "n/a":
        return None, start + 1
    if (
        start + 3 < len(tokens)
        and re.fullmatch(r"\d{4}-\d{2}-\d{2}", tokens[start + 1])
        and re.fullmatch(r"\d{2}:\d{2}:\d{2}", tokens[start + 2])
    ):
        return " ".join(tokens[start : start + 4]), start + 4
    return tokens[start], start + 1


def _parse_timer_schedule(output: str, unit: str) -> dict[str, Any] | None:
    for line in output.splitlines():
        if unit not in line:
            continue
        parts = line.split()
        if not parts or parts[0] == "NEXT":
            continue
        try:
            unit_idx = parts.index(unit)
        except ValueError:
            continue

        next_raw, next_end = _consume_timer_timestamp(parts, 0)

        last_idx = None
        for idx in range(next_end, unit_idx):
            if (
                idx + 3 < unit_idx
                and re.fullmatch(r"\d{4}-\d{2}-\d{2}", parts[idx + 1])
                and re.fullmatch(r"\d{2}:\d{2}:\d{2}", parts[idx + 2])
            ):
                last_idx = idx
                break

        left_tokens = parts[next_end : last_idx if last_idx is not None else unit_idx]
        left = " ".join(left_tokens).strip() or None
        last_run = None
        passed = None
        if last_idx is not None:
            last_run, last_end = _consume_timer_timestamp(parts, last_idx)
            passed_tokens = parts[last_end:unit_idx]
            passed = " ".join(passed_tokens).strip() or None

        next_run = None
        if next_raw:
            next_run = f"{next_raw} ({left})" if left and left != "n/a" else next_raw

        return {
            "available": True,
            "next_run": next_run,
            "last_run": last_run,
            "left": left,
            "passed": passed,
            "raw": line.strip(),
            "warning": None,
        }
    return None


def _parse_timer_next_run(output: str, unit: str) -> str | None:
    schedule = _parse_timer_schedule(output, unit)
    if schedule:
        return schedule.get("next_run")
    return None


def _list_timer_schedule(unit: str = SCHEDULER_TIMER_UNIT) -> dict[str, Any]:
    ok, out = run_systemctl(["list-timers", "--all", "--no-pager", "--no-legend"], timeout=10.0)
    if not ok or not out.strip():
        return {
            "available": False,
            "next_run": None,
            "last_run": None,
            "left": None,
            "passed": None,
            "raw": None,
            "warning": out or "systemctl list-timers unavailable",
        }
    schedule = _parse_timer_schedule(out, unit)
    if schedule:
        return schedule
    return {
        "available": True,
        "next_run": None,
        "last_run": None,
        "left": None,
        "passed": None,
        "raw": None,
        "warning": f"{unit} not listed by systemctl list-timers",
    }


def _list_timer_next_run(unit: str = SCHEDULER_TIMER_UNIT) -> str | None:
    return _list_timer_schedule(unit).get("next_run")


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
    timer_svc = _check_service(
        "vulture-scheduler timer",
        (SCHEDULER_TIMER_UNIT, SCHEDULER_TIMER_UNIT.replace(".timer", "")),
    )
    service_svc = _check_scheduler_service()
    schedule = _list_timer_schedule()
    next_run = schedule.get("next_run")

    journal = _journal_lines(SCHEDULER_SERVICE_UNIT)
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
    schedule_available = bool(schedule.get("available"))
    has_upcoming_run = bool(next_run)

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
    elif activity and activity.get("status") == "fresh":
        status = "fresh"
        detail_parts.append(activity["detail"])
    elif has_upcoming_run:
        status = "scheduled"
        detail_parts.append("timer active with upcoming run")
        if activity:
            detail_parts.append(activity["detail"])
    elif schedule_available:
        status = "stale"
        warning = f"No recent scheduler run and no upcoming timer run within {SCHEDULER_FRESH_MINUTES} min"
        if activity:
            detail_parts.append(activity["detail"])
        else:
            detail_parts.append("no scheduler run found in journal or log tail")
    elif journal:
        status = "seen"
        detail_parts.append("scheduler journal entries present")
    else:
        detail_parts.append("timer schedule unavailable")
        detail_parts.append("no recent scheduler lines in journal or log tail")

    if next_run:
        detail_parts.append(f"next run {next_run}")
    elif schedule.get("warning") and status not in ("unhealthy", "stale"):
        detail_parts.append(str(schedule["warning"]))
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
        "timer_schedule": schedule,
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
