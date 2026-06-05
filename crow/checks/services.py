"""
Read-only process and systemd service visibility.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass

from crow.checks._subprocess import run_command
from crow.config import (
    DEFAULT_SUBPROCESS_TIMEOUT,
    VULTURE_BOT_SYSTEMD_UNIT,
    VULTURE_SCHEDULER_SYSTEMD_UNIT,
)

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


def _systemctl_is_active(unit: str) -> tuple[ServiceState, str | None]:
    """Return (state, detail) from `systemctl is-active`."""
    try:
        result = subprocess.run(
            ["systemctl", "is-active", unit],
            capture_output=True,
            text=True,
            timeout=DEFAULT_SUBPROCESS_TIMEOUT,
            check=False,
        )
    except FileNotFoundError:
        return "unknown", "systemctl not found"
    except subprocess.TimeoutExpired:
        return "unknown", "systemctl timed out"
    except OSError as exc:
        return "unknown", f"os error: {exc}"

    state_text = (result.stdout or result.stderr or "").strip().lower()
    if result.returncode == 0 and state_text == "active":
        return "running", f"systemctl is-active {unit}"
    if state_text in ("inactive", "failed", "dead", "activating"):
        return "not detected", f"systemctl: {state_text}"
    if state_text:
        return "not detected", f"systemctl: {state_text}"
    return "unknown", None


def get_journal_excerpt(unit: str, lines: int = 5) -> str | None:
    """Return a short, redacted journal excerpt for a systemd unit."""
    ok, out = run_command(
        ["journalctl", "-u", unit, "-n", str(lines), "--no-pager"],
        timeout=max(DEFAULT_SUBPROCESS_TIMEOUT, 15.0),
    )
    if not ok or not out.strip():
        return None
    return out.strip()


def check_discord_bot() -> ServiceStatus:
    state = _pgrep_matches("discord_bot.py")
    if state == "not detected":
        state = _pgrep_matches("discord_bot")
    detail = None
    if state == "running":
        detail = "discord_bot.py process detected"
    return ServiceStatus("Discord bot / Crow (process)", state, detail)


def check_bot_systemd() -> ServiceStatus:
    state, detail = _systemctl_is_active(VULTURE_BOT_SYSTEMD_UNIT)
    return ServiceStatus(f"systemd {VULTURE_BOT_SYSTEMD_UNIT}", state, detail)


def check_scheduler_process() -> ServiceStatus:
    state = _pgrep_matches("main.py")
    detail = "main.py process detected" if state == "running" else None
    return ServiceStatus("Vulture scheduler (main.py process)", state, detail)


def check_scheduler_systemd() -> ServiceStatus:
    state, detail = _systemctl_is_active(VULTURE_SCHEDULER_SYSTEMD_UNIT)
    return ServiceStatus(
        f"systemd {VULTURE_SCHEDULER_SYSTEMD_UNIT}",
        state,
        detail,
    )


def get_all_service_statuses() -> list[ServiceStatus]:
    return [
        check_bot_systemd(),
        check_scheduler_systemd(),
        check_discord_bot(),
        check_scheduler_process(),
    ]


def format_services_message(statuses: list[ServiceStatus]) -> str:
    from crow.formatting import join_lines, truncate

    lines = ["**Service check** (read-only — no restarts)"]
    for s in statuses:
        extra = f" — {s.detail}" if s.detail else ""
        lines.append(f"• **{s.name}**: {s.state}{extra}")

    bot_journal = get_journal_excerpt(VULTURE_BOT_SYSTEMD_UNIT, lines=5)
    if bot_journal:
        lines.append(f"• **Recent {VULTURE_BOT_SYSTEMD_UNIT} logs**:\n```\n{bot_journal}\n```")

    scheduler_journal = get_journal_excerpt(VULTURE_SCHEDULER_SYSTEMD_UNIT, lines=5)
    if scheduler_journal:
        lines.append(
            f"• **Recent {VULTURE_SCHEDULER_SYSTEMD_UNIT} logs**:\n```\n{scheduler_journal}\n```"
        )

    return truncate(join_lines(lines), 1900)
