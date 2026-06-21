"""
Pelican monitor configuration — aggregate paths, Discord webhook, backup overrides.
"""

from __future__ import annotations

import os
from pathlib import Path

PROJECT_ROOT = Path(os.getenv("PELICAN_MONITOR_PROJECT_ROOT", ".")).resolve()
DATA_DIR = Path(os.getenv("PELICAN_MONITOR_DATA_DIR", str(PROJECT_ROOT / "data")))
LOGS_DIR = Path(os.getenv("PELICAN_MONITOR_LOGS_DIR", str(PROJECT_ROOT / "logs")))

STATUS_PATH = Path(
    os.getenv("PELICAN_MONITOR_STATUS_PATH", str(DATA_DIR / "backup_monitor_status.json"))
)
ALERT_STATE_PATH = Path(
    os.getenv("PELICAN_MONITOR_ALERT_STATE_PATH", str(DATA_DIR / "canary_alert_state.json"))
)

HOST_ROOT = Path(os.getenv("PELICAN_MONITOR_HOST_ROOT", "/")).resolve()
DISPLAY_TIMEZONE = os.getenv("PELICAN_MONITOR_TIMEZONE", "America/Chicago").strip() or "America/Chicago"

DISCORD_WEBHOOK_URL = (
    os.getenv("PELICAN_MONITOR_DISCORD_WEBHOOK_URL", os.getenv("DISCORD_WEBHOOK_URL", "")).strip()
)

# Comma-separated backup IDs; empty means all registered definitions use their default enabled flag.
_enabled_raw = os.getenv("PELICAN_MONITOR_ENABLED_BACKUPS", "").strip()
ENABLED_BACKUP_IDS: frozenset[str] | None = (
    frozenset(item.strip() for item in _enabled_raw.split(",") if item.strip())
    if _enabled_raw
    else None
)

# Raven recovery bundle (first Pelican-managed backup definition)
RAVEN_RECOVERY_TARGET = (
    os.getenv("PELICAN_RAVEN_RECOVERY_TARGET", "/mnt/storage/pelican_backup").strip()
    or "/mnt/storage/pelican_backup"
)
RAVEN_RECOVERY_TIMER_UNIT = (
    os.getenv("PELICAN_RAVEN_RECOVERY_TIMER_UNIT", "pelican-backup.timer").strip()
    or "pelican-backup.timer"
)
RAVEN_RECOVERY_SERVICE_UNIT = (
    os.getenv("PELICAN_RAVEN_RECOVERY_SERVICE_UNIT", "pelican-backup.service").strip()
    or "pelican-backup.service"
)
RAVEN_RECOVERY_WARN_HOURS = float(os.getenv("PELICAN_RAVEN_RECOVERY_WARN_HOURS", "30"))
RAVEN_RECOVERY_CRITICAL_HOURS = float(os.getenv("PELICAN_RAVEN_RECOVERY_CRITICAL_HOURS", "36"))

# Subprocess timeouts (shared with checker implementations)
TIMEOUT_SYSTEMCTL = float(os.getenv("PELICAN_MONITOR_TIMEOUT_SYSTEMCTL", "10"))
TIMEOUT_FINDMNT = float(os.getenv("PELICAN_MONITOR_TIMEOUT_FINDMNT", "5"))
TIMEOUT_PATH = float(os.getenv("PELICAN_MONITOR_TIMEOUT_PATH", "3"))
