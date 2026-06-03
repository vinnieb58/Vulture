"""
Read-only process and tmux session visibility.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass

from crow.checks._subprocess import run_command
from crow.config import DEFAULT_SUBPROCESS_TIMEOUT

ServiceState = str  # "running" | "not detected" | "unknown"


@dataclass
class ServiceStatus:
    name: str
    state: ServiceState
    detail: str | None = None


def _pgrep_matches(pattern: str) -> ServiceState:
    try:
        result = subprocess.run(
            ["pgrep", "-af", pattern],
            capture_output=True,
            text=True,
            timeout=DEFAULT_SUBPROCESS_TIMEOUT,
            check=False,
        )
    except FileNotFoundError:
        return "unknown"
    except subprocess.TimeoutExpired:
        return "unknown"
    except OSError:
        return "unknown"

    if result.returncode == 0 and result.stdout.strip():
        return "running"
    if result.returncode == 1:
        return "not detected"
    return "unknown"


def _tmux_session_exists(name: str) -> ServiceState:
    ok, out = run_command(["tmux", "ls"])
    if not ok:
        if "command not found" in out.lower():
            return "unknown"
        return "unknown"
    for line in out.splitlines():
        session = line.split(":")[0].strip()
        if session == name:
            return "running"
    return "not detected"


def check_discord_bot() -> ServiceStatus:
    state = _pgrep_matches("discord_bot.py")
    if state == "not detected":
        state = _pgrep_matches("discord_bot")
    detail = None
    if state == "running":
        detail = "discord_bot.py process detected"
    return ServiceStatus("Discord bot / Crow", state, detail)


def check_scheduler_process() -> ServiceStatus:
    state = _pgrep_matches("main.py")
    detail = "main.py process detected" if state == "running" else None
    return ServiceStatus("Vulture scheduler (main.py)", state, detail)


def check_scheduler_tmux() -> ServiceStatus:
    state = _tmux_session_exists("scheduler")
    detail = "tmux session 'scheduler'" if state == "running" else None
    return ServiceStatus("Scheduler tmux session", state, detail)


def check_bot_tmux() -> ServiceStatus:
    state = _tmux_session_exists("bot")
    detail = "tmux session 'bot'" if state == "running" else None
    return ServiceStatus("Bot tmux session", state, detail)


def get_all_service_statuses() -> list[ServiceStatus]:
    return [
        check_discord_bot(),
        check_bot_tmux(),
        check_scheduler_process(),
        check_scheduler_tmux(),
    ]


def format_services_message(statuses: list[ServiceStatus]) -> str:
    from crow.formatting import join_lines

    lines = ["**Service check** (read-only — no restarts)"]
    for s in statuses:
        extra = f" — {s.detail}" if s.detail else ""
        lines.append(f"• **{s.name}**: {s.state}{extra}")
    return join_lines(lines)
