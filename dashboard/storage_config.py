"""Expected Raven storage drive definitions for the Vulture Dashboard."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from raven_storage_inventory import (  # noqa: E402
    ALL_MONITORED_VOLUMES,
    DISK_CRITICAL_PERCENT,
    DISK_WARN_PERCENT,
    RavenStorageVolume,
)

HOST_ROOT = Path(os.environ.get("DASHBOARD_HOST_ROOT", "/host/root"))


@dataclass(frozen=True)
class ExpectedDrive:
    name: str
    path: str
    expected_uuid: str | None = None
    expected_source: str | None = None
    expected_fstype: str | None = None
    expected_label: str | None = None
    role: str = "storage"
    required: bool = True
    legacy: bool = False


def _volume_to_drive(volume: RavenStorageVolume) -> ExpectedDrive:
    path = volume.path
    if volume.role == "root":
        path = str(HOST_ROOT)
    return ExpectedDrive(
        name=volume.name,
        path=path,
        expected_uuid=volume.expected_uuid,
        expected_source=volume.expected_source,
        expected_fstype=volume.expected_fstype,
        expected_label=volume.expected_label,
        role=volume.role,
        required=volume.required,
        legacy=volume.legacy,
    )


DEFAULT_EXPECTED_DRIVES: tuple[ExpectedDrive, ...] = tuple(
    _volume_to_drive(volume) for volume in ALL_MONITORED_VOLUMES
)


def container_to_host_path(path: str) -> str:
    """Map dashboard container paths to Raven host paths."""
    host_root = str(HOST_ROOT).rstrip("/")
    normalized = path.rstrip("/") or "/"
    if normalized == host_root or normalized == f"{host_root}/":
        return "/"
    if normalized.startswith(f"{host_root}/"):
        return "/" + normalized[len(host_root) + 1 :]
    return normalized


def path_to_systemd_unit(path: str) -> str | None:
    """Derive systemd mount unit name from a host mount path."""
    host_path = container_to_host_path(path)
    stripped = host_path.strip("/")
    if not stripped:
        return None
    return stripped.replace("/", "-")
