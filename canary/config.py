"""
Canary configuration — intervals, paths, and thresholds only (no secrets).
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from raven_storage_inventory import (  # noqa: E402
    CANARY_STORAGE_PATHS,
    DISK_CRITICAL_PERCENT,
    DISK_WARN_PERCENT,
)

DEFAULT_SUBPROCESS_TIMEOUT = 10.0
DEFAULT_INTERVAL_SECONDS = 300

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
VULTURE_CONCERT_WATCHES_TIMER = (
    os.getenv("CANARY_VULTURE_CONCERT_WATCHES_TIMER", "vulture-concert-watches.timer").strip()
    or "vulture-concert-watches.timer"
)
DASHBOARD_CONTAINER = (
    os.getenv("CANARY_DASHBOARD_CONTAINER", "vulture-dashboard").strip() or "vulture-dashboard"
)

BACKUP_MONITOR_STATUS_PATH = Path(
    os.getenv("CANARY_BACKUP_MONITOR_STATUS_PATH", str(DATA_DIR / "backup_monitor_status.json"))
)
BACKUP_MONITOR_SNAPSHOT_STALE_HOURS = float(os.getenv("CANARY_BACKUP_MONITOR_SNAPSHOT_STALE_HOURS", "8"))

ALERT_STATE_PATH = Path(
    os.getenv("CANARY_ALERT_STATE_PATH", str(DATA_DIR / "canary_alert_state.json"))
)

# Raven external/storage mount paths (UUIDs resolved from fstab or CANARY_STORAGE_VOLUMES)
RAVEN_STORAGE_PATHS: list[tuple[str, str]] = list(CANARY_STORAGE_PATHS)

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
    required: bool = True


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
                required=bool(item.get("required", True)),
            )
        )
    return [v for v in volumes if v.label and v.mount_path]


def default_storage_volume_specs() -> list[StorageVolumeSpec]:
    """Build volume specs from the shared Raven inventory."""
    from raven_storage_inventory import ALL_STORAGE_VOLUMES

    return [
        StorageVolumeSpec(
            label=volume.key,
            mount_path=volume.path,
            uuid=volume.expected_uuid,
            fstype=volume.expected_fstype,
            label_tag=volume.expected_label,
            automount_expected=True,
            required=volume.required,
        )
        for volume in ALL_STORAGE_VOLUMES
    ]


_storage_override = os.getenv("CANARY_STORAGE_VOLUMES", "").strip()
STORAGE_VOLUME_SPECS: list[StorageVolumeSpec] = (
    _parse_storage_volumes_env(_storage_override)
    if _storage_override
    else default_storage_volume_specs()
)
