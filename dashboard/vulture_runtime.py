"""Read-only Vulture process and scheduler runtime visibility."""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from host_status import _check_service
from subprocess_util import run_command

LOG_PATH = Path(os.environ.get("VULTURE_LOG_PATH", "/app/logs/vulture.log"))
SCHEDULER_FRESH_MINUTES = int(os.environ.get("DASHBOARD_SCHEDULER_FRESH_MINUTES", "30"))


@dataclass
class ProcessMatch:
    label: str
    running: bool
    detail: str
    warning: str | None = None


def _process_running(pattern: str) -> tuple[bool, str]:
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


def _scheduler_freshness(log_lines: list[str]) -> dict[str, Any]:
    """Heuristic: look for recent scheduler activity in log tail."""
    keywords = ("scheduler", "scheduled", "hunt run", "running hunt", "cycle")
    recent: list[str] = []
    for line in reversed(log_lines):
        lower = line.lower()
        if any(k in lower for k in keywords):
            recent.append(line)
            if len(recent) >= 3:
                break

    if not recent:
        return {
            "status": "unknown",
            "detail": "No scheduler lines in recent log tail",
            "warning": None,
        }

    # Try to parse timestamp prefix like 2026-06-05 12:34:56
    import re

    for line in recent:
        m = re.match(r"^(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})", line)
        if m:
            try:
                ts = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S").replace(
                    tzinfo=timezone.utc
                )
                age_min = (datetime.now(timezone.utc) - ts).total_seconds() / 60
                if age_min <= SCHEDULER_FRESH_MINUTES:
                    return {
                        "status": "fresh",
                        "detail": f"Last scheduler activity ~{int(age_min)} min ago",
                        "warning": None,
                    }
                return {
                    "status": "stale",
                    "detail": f"Last scheduler activity ~{int(age_min)} min ago",
                    "warning": f"No scheduler log activity within {SCHEDULER_FRESH_MINUTES} min",
                }
            except ValueError:
                continue

    return {
        "status": "seen",
        "detail": "Scheduler lines present in log tail",
        "warning": None,
    }


def get_vulture_runtime(log_lines: list[str] | None = None) -> dict[str, Any]:
    bot_svc = _check_service("vulture-bot", ("vulture-bot.service", "vulture-bot"))
    sched_svc = _check_service("vulture-scheduler", ("vulture-scheduler.service", "vulture-scheduler"))

    bot_proc, bot_detail = _process_running("discord_bot.py")
    sched_proc, sched_detail = _process_running("scheduler")
    if not sched_proc:
        sched_alt, sched_alt_detail = _process_running("main.py")
        if sched_alt:
            sched_proc, sched_detail = sched_alt, sched_alt_detail

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

    processes = [
        ProcessMatch(
            label="Discord bot",
            running=bot_svc.active == "active" or bot_proc,
            detail=(
                f"systemd: {bot_svc.active}"
                if bot_svc.unit
                else bot_detail
            ),
            warning=None if (bot_svc.active == "active" or bot_proc) else "Bot not detected",
        ),
        ProcessMatch(
            label="Scheduler",
            running=sched_svc.active == "active" or sched_proc,
            detail=(
                f"systemd: {sched_svc.active}"
                if sched_svc.unit
                else sched_detail
            ),
            warning=None
            if (sched_svc.active == "active" or sched_proc)
            else "Scheduler not detected",
        ),
    ]

    return {
        "systemd": {
            "bot": bot_svc,
            "scheduler": sched_svc,
        },
        "processes": processes,
        "tmux_sessions": sessions,
        "log_mtime": log_mtime,
        "scheduler_freshness": freshness,
        "warnings": warnings,
    }
