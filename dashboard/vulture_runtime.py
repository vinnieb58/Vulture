"""Read-only Vulture process and scheduler runtime visibility."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from host_commands import run_host_command
from host_status import ServiceStatus, _check_service
from subprocess_util import run_command

LOG_PATH = Path(os.environ.get("VULTURE_LOG_PATH", "/app/logs/vulture.log"))
SCHEDULER_FRESH_MINUTES = int(os.environ.get("DASHBOARD_SCHEDULER_FRESH_MINUTES", "30"))


@dataclass
class ProcessMatch:
    label: str
    running: bool
    detail: str
    warning: str | None = None
    status_text: str | None = None


def _service_active(svc: ServiceStatus) -> bool:
    return svc.active == "active"


def _service_enabled(svc: ServiceStatus) -> bool:
    return svc.enabled in ("enabled", "static")


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


def _format_scheduler_detail(
    timer_svc: ServiceStatus,
    service_svc: ServiceStatus,
    proc: bool,
    proc_detail: str,
    freshness: dict[str, Any],
) -> str:
    parts: list[str] = []
    timer = _systemd_detail(timer_svc)
    if timer:
        parts.append(f"timer {timer}")
    if service_svc.unit:
        parts.append(f"worker: {service_svc.active} (oneshot)")
    if proc:
        parts.append(f"cycle running: {proc_detail[:80]}")
    if freshness.get("detail"):
        parts.append(str(freshness["detail"]))
    return " · ".join(parts) if parts else "not detected"


def _pgrep_running(pattern: str) -> tuple[bool, str]:
    ok, out = run_host_command(["pgrep", "-af", pattern], timeout=8.0)
    if not ok or not out.strip():
        return False, "not running"
    lines = [line.strip() for line in out.splitlines() if line.strip()]
    preferred = [
        ln
        for ln in lines
        if "/projects/vulture/" in ln and "while true" not in ln and "tmux" not in ln
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
        if pattern in line and "grep" not in line and "while true" not in line:
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
    m = re.search(r"(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2}:\d{2})", line)
    if m:
        raw = f"{m.group(1)} {m.group(2)}"
        try:
            return datetime.strptime(raw, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    return None


def _freshness_from_lines(lines: list[str], *, source: str) -> dict[str, Any] | None:
    success_keywords = ("hunt cycle completed",)
    activity_keywords = (
        "starting vulture hunt cycle",
        "starting hunt:",
        "done hunt",
        "hunt cycle",
    )
    for line in reversed(lines):
        lower = line.lower()
        if not any(k in lower for k in (*success_keywords, *activity_keywords)):
            continue
        ts = _parse_log_timestamp(line)
        if ts is None:
            continue
        age_min = (datetime.now(timezone.utc) - ts).total_seconds() / 60
        completed = any(k in lower for k in success_keywords)
        status = "fresh" if age_min <= SCHEDULER_FRESH_MINUTES else "stale"
        detail = (
            f"Last {'successful ' if completed else ''}cycle ~{int(age_min)} min ago ({source})"
        )
        warning = None
        if status == "stale":
            warning = f"No scheduler activity within {SCHEDULER_FRESH_MINUTES} min"
        return {"status": status, "detail": detail, "warning": warning, "completed": completed}
    return None


def _scheduler_freshness(log_lines: list[str]) -> dict[str, Any]:
    """Prefer vulture-scheduler journal; fall back to vulture.log tail."""
    journal = _journal_lines("vulture-scheduler")
    from_journal = _freshness_from_lines(journal, source="journal")
    if from_journal:
        return from_journal

    from_log = _freshness_from_lines(log_lines, source="vulture.log")
    if from_log:
        return from_log

    if journal:
        return {
            "status": "seen",
            "detail": "Scheduler journal entries present",
            "warning": None,
            "completed": False,
        }

    return {
        "status": "unknown",
        "detail": "No recent scheduler lines in journal or log tail",
        "warning": None,
        "completed": False,
    }


def _scheduler_healthy(
    timer_svc: ServiceStatus,
    freshness: dict[str, Any],
    cycle_running: bool,
) -> bool:
    timer_ok = _service_active(timer_svc) and _service_enabled(timer_svc)
    journal_ok = freshness.get("status") == "fresh" and freshness.get("completed", False)
    return timer_ok and (journal_ok or cycle_running)


def get_vulture_runtime(log_lines: list[str] | None = None) -> dict[str, Any]:
    bot_svc = _check_service("vulture-bot", ("vulture-bot.service", "vulture-bot"))
    timer_svc = _check_service(
        "vulture-scheduler.timer",
        ("vulture-scheduler.timer",),
    )
    worker_svc = _check_service(
        "vulture-scheduler",
        ("vulture-scheduler.service", "vulture-scheduler"),
    )

    bot_proc, bot_detail = _process_running("discord_bot.py")
    sched_proc, sched_detail = _process_running("main.py")

    sessions, tmux_warn = _tmux_sessions()
    log_mtime, log_warn = _log_mtime()
    freshness = _scheduler_freshness(log_lines or [])

    warnings: list[str] = []
    if tmux_warn:
        warnings.append(tmux_warn)
    if log_warn:
        warnings.append(log_warn)
    if freshness.get("warning"):
        warnings.append(str(freshness["warning"]))
    if not _service_active(timer_svc):
        warnings.append("vulture-scheduler.timer is not active")

    bot_running = _service_active(bot_svc) or bot_proc
    sched_healthy = _scheduler_healthy(timer_svc, freshness, sched_proc)

    processes = [
        ProcessMatch(
            label="Discord bot",
            running=bot_running,
            detail=_format_runtime_detail(bot_svc, bot_proc, bot_detail),
            warning=None if bot_running else "Bot not detected",
        ),
        ProcessMatch(
            label="Scheduler",
            running=sched_healthy,
            status_text="healthy" if sched_healthy else "unhealthy",
            detail=_format_scheduler_detail(
                timer_svc, worker_svc, sched_proc, sched_detail, freshness
            ),
            warning=None
            if sched_healthy
            else "Scheduler timer or recent journal activity not healthy",
        ),
    ]

    return {
        "systemd": {
            "bot": bot_svc,
            "scheduler_timer": timer_svc,
            "scheduler_worker": worker_svc,
        },
        "processes": processes,
        "tmux_sessions": sessions,
        "log_mtime": log_mtime,
        "scheduler_freshness": freshness,
        "warnings": warnings,
    }
