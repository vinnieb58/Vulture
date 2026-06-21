"""
Canary configuration — intervals, paths, and thresholds only (no secrets).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_SUBPROCESS_TIMEOUT = 10.0
DEFAULT_INTERVAL_SECONDS = 300

DISK_WARN_PERCENT = 80
DISK_CRITICAL_PERCENT = 90

# Per-command timeouts (seconds)
TIMEOUT_PING = 8.0
TIMEOUT_DF = 10.0
TIMEOUT_LSBLK = 10.0
TIMEOUT_BLKID = 8.0
TIMEOUT_FINDMNT = 5.0
TIMEOUT_SYSTEMCTL = 10.0
TIMEOUT_DOCKER = 15.0
TIMEOUT_PATH = 3.0

PROJECT_ROOT = Path(os.getenv("CANARY_PROJECT_ROOT", ".")).resolve()
DATA_DIR = Path(os.getenv("CANARY_DATA_DIR", str(PROJECT_ROOT / "data")))
LOGS_DIR = Path(os.getenv("CANARY_LOGS_DIR", str(PROJECT_ROOT / "logs")))
STATUS_PATH = Path(os.getenv("CANARY_STATUS_PATH", str(DATA_DIR / "canary_status.json")))
LOG_PATH = Path(os.getenv("CANARY_LOG_PATH", str(LOGS_DIR / "canary.log")))

INTERVAL_SECONDS = int(os.getenv("CANARY_INTERVAL_SECONDS", str(DEFAULT_INTERVAL_SECONDS)))

HOST_ROOT = Path(os.getenv("CANARY_HOST_ROOT", "/")).resolve()
FSTAB_PATH = Path(os.getenv("CANARY_FSTAB_PATH", str(HOST_ROOT / "etc" / "fstab")))

DISPLAY_TIMEZONE = os.getenv("CANARY_TIMEZONE", "America/Chicago").strip() or "America/Chicago"

VULTURE_BOT_UNIT = os.getenv("CANARY_VULTURE_BOT_UNIT", "vulture-bot").strip() or "vulture-bot"
VULTURE_SCHEDULER_TIMER = (
    os.getenv("CANARY_VULTURE_SCHEDULER_TIMER", "vulture-scheduler.timer").strip()
    or "vulture-scheduler.timer"
)
DASHBOARD_CONTAINER = (
    os.getenv("CANARY_DASHBOARD_CONTAINER", "vulture-dashboard").strip() or "vulture-dashboard"
)

PELICAN_TIMER_UNIT = (
    os.getenv("CANARY_PELICAN_TIMER_UNIT", "pelican-backup.timer").strip() or "pelican-backup.timer"
)
PELICAN_SERVICE_UNIT = (
    os.getenv("CANARY_PELICAN_SERVICE_UNIT", "pelican-backup.service").strip()
    or "pelican-backup.service"
)
PELICAN_BACKUP_TARGET = (
    os.getenv("CANARY_PELICAN_BACKUP_TARGET", "/mnt/storage/pelican_backup").strip()
    or "/mnt/storage/pelican_backup"
)
PELICAN_STALE_HOURS = float(os.getenv("CANARY_PELICAN_STALE_HOURS", "36"))
PELICAN_STALE_WARN_HOURS = float(os.getenv("CANARY_PELICAN_STALE_WARN_HOURS", "30"))

DISCORD_WEBHOOK_URL = (
    os.getenv("CANARY_DISCORD_WEBHOOK_URL", os.getenv("DISCORD_WEBHOOK_URL", "")).strip()
)
ALERT_STATE_PATH = Path(
    os.getenv("CANARY_ALERT_STATE_PATH", str(DATA_DIR / "canary_alert_state.json"))
)

# Raven external/storage mount paths (UUIDs resolved from fstab or CANARY_STORAGE_VOLUMES)
RAVEN_STORAGE_PATHS: list[tuple[str, str]] = [
    ("toshiba_ext", "/mnt/storage/toshiba_ext"),
    ("pelican_backup", "/mnt/storage/pelican_backup"),
    ("roost_spinning_0", "/mnt/storage/roost_spinning_0"),
    ("raven_nvme", "/mnt/storage/raven_nvme"),
    ("microsd", "/mnt/storage/microsd"),
]

ROOT_MOUNT = ("root", "/")


@dataclass(frozen=True)
class StorageVolumeSpec:
    label: str
    mount_path: str
    uuid: str | None = None
    fstype: str | None = None
    label_tag: str | None = None
    automount_unit: str | None = None
    automount_expected: bool = False


def _parse_storage_volumes_env(raw: str) -> list[StorageVolumeSpec]:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return []
    volumes: list[StorageVolumeSpec] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        volumes.append(
            StorageVolumeSpec(
                label=str(item.get("label", "")),
                mount_path=str(item.get("mount_path", "")),
                uuid=item.get("uuid"),
                fstype=item.get("fstype"),
                label_tag=item.get("label_tag"),
                automount_unit=item.get("automount_unit"),
                automount_expected=bool(item.get("automount_expected", False)),
            )
        )
    return [v for v in volumes if v.label and v.mount_path]


def default_storage_volume_specs() -> list[StorageVolumeSpec]:
    """Build volume specs from known Raven paths; UUID/fstype filled from fstab at runtime."""
    return [
        StorageVolumeSpec(label=label, mount_path=path, automount_expected=True)
        for label, path in RAVEN_STORAGE_PATHS
    ]


_storage_override = os.getenv("CANARY_STORAGE_VOLUMES", "").strip()
STORAGE_VOLUME_SPECS: list[StorageVolumeSpec] = (
    _parse_storage_volumes_env(_storage_override)
    if _storage_override
    else default_storage_volume_specs()
)
