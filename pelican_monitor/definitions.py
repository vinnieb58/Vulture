"""
Backup definition registry for the Pelican monitor.

Each definition describes one Pelican-managed backup target. Adding a future backup
(for example Time Machine or Windows backup status) is a small, isolated registry entry
plus a checker module — not a redesign of the runner or timer.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from pelican_monitor import config
from pelican_monitor.checkers.raven_recovery import check_raven_recovery
from pelican_monitor.results import BackupCheckResult

CheckerFn = Callable[[], BackupCheckResult]


@dataclass(frozen=True)
class BackupDefinition:
    backup_id: str
    display_name: str
    enabled: bool
    checker: CheckerFn
    target_path: str
    archive_pattern: str | None
    warn_threshold_hours: float
    critical_threshold_hours: float
    checksum_expected: bool
    timer_unit: str | None = None
    service_unit: str | None = None


def _definition_enabled(defn: BackupDefinition) -> bool:
    if config.ENABLED_BACKUP_IDS is not None:
        return defn.backup_id in config.ENABLED_BACKUP_IDS
    return defn.enabled


def registered_backup_definitions() -> list[BackupDefinition]:
    """All Pelican-managed backup definitions (enabled flag may still filter at run time)."""
    return [
        BackupDefinition(
            backup_id="raven_recovery",
            display_name="Pelican backup (repo, DBs, telemetry/history)",
            enabled=True,
            checker=check_raven_recovery,
            target_path=config.RAVEN_RECOVERY_TARGET,
            archive_pattern=r"^raven-recovery-\d{8}T\d{6}Z\.tar\.(?:zst|gz)$",
            warn_threshold_hours=config.RAVEN_RECOVERY_WARN_HOURS,
            critical_threshold_hours=config.RAVEN_RECOVERY_CRITICAL_HOURS,
            checksum_expected=True,
            timer_unit=config.RAVEN_RECOVERY_TIMER_UNIT,
            service_unit=config.RAVEN_RECOVERY_SERVICE_UNIT,
        ),
    ]


def enabled_backup_definitions() -> list[BackupDefinition]:
    return [d for d in registered_backup_definitions() if _definition_enabled(d)]
