"""
Critical systemd service visibility for Raven (read-only).
"""

from __future__ import annotations

from dataclasses import dataclass

from crow.checks._subprocess import run_command
from crow.config import (
    VULTURE_BOT_SYSTEMD_UNIT,
    VULTURE_SCHEDULER_SYSTEMD_UNIT,
)
from crow.system._status import StatusItem, StatusLevel


@dataclass(frozen=True)
class ServiceCheck:
    label: str
    unit: str
    active: bool
    state: str


def _resolve_ssh_unit() -> str:
    for candidate in ("ssh", "sshd"):
        ok, out = run_command(["systemctl", "is-active", f"{candidate}.service"])
        if ok and out.strip().lower() == "active":
            return f"{candidate}.service"
        ok_enabled, enabled_out = run_command(["systemctl", "is-enabled", f"{candidate}.service"])
        if ok_enabled and enabled_out.strip().lower() in ("enabled", "static", "masked"):
            return f"{candidate}.service"
    return "ssh.service"


def _systemctl_state(unit: str) -> tuple[bool, str]:
    ok, out = run_command(["systemctl", "is-active", unit])
    state = out.strip().lower() if out else "unknown"
    if ok and state == "active":
        return True, "active"
    if state in ("inactive", "failed", "dead", "activating", "deactivating"):
        return False, state
    if not ok and state:
        return False, state
    return False, "unknown"


def check_service(label: str, unit: str) -> ServiceCheck:
    active, state = _systemctl_state(unit)
    return ServiceCheck(label=label, unit=unit, active=active, state=state)


def get_critical_service_statuses() -> list[ServiceCheck]:
    ssh_unit = _resolve_ssh_unit()
    return [
        check_service("SSH", ssh_unit),
        check_service("Tailscale", "tailscaled"),
        check_service("Samba", "smbd"),
        check_service("Docker", "docker"),
        check_service("Vulture Bot", VULTURE_BOT_SYSTEMD_UNIT),
        check_service("Vulture Scheduler", VULTURE_SCHEDULER_SYSTEMD_UNIT),
    ]


def service_level(check: ServiceCheck) -> StatusLevel:
    if check.active:
        return "ok"
    if check.state in ("inactive", "failed", "dead"):
        if check.label.startswith("Vulture"):
            return "fail"
        return "warn"
    return "warn"


def service_to_status_item(check: ServiceCheck) -> StatusItem:
    level = service_level(check)
    if check.active:
        detail = "ACTIVE"
    else:
        detail = check.state.upper() if check.state != "unknown" else "INACTIVE"
    return StatusItem(label=check.label, level=level, detail=detail)


def format_service_line(check: ServiceCheck) -> str:
    if check.active:
        return f"{check.label:<18} ACTIVE"
    return f"❌ {check.label}"
