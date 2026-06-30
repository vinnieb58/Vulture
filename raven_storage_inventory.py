"""
Authoritative Raven storage inventory shared by dashboard, Canary, and Crow.

Paths use the /mnt/storage/* layout. Legacy top-level mounts (/mnt/microsd, etc.)
are not monitored here.
"""

from __future__ import annotations

from dataclasses import dataclass

STORAGE_PARENT = "/mnt/storage"

# Disk usage thresholds (percent used) — keep aligned across observability tools.
DISK_WARN_PERCENT = 80
DISK_CRITICAL_PERCENT = 90


@dataclass(frozen=True)
class RavenStorageVolume:
    """One monitored filesystem on Raven."""

    key: str
    name: str
    path: str
    expected_uuid: str | None = None
    expected_fstype: str | None = None
    expected_label: str | None = None
    expected_source: str | None = None
    role: str = "storage"
    required: bool = True
    legacy: bool = False


ROOT_VOLUME = RavenStorageVolume(
    key="root",
    name="Root filesystem",
    path="/",
    expected_source="/dev/sde2",
    expected_fstype="ext4",
    role="root",
    required=True,
)

# Expected / normally attached storage under /mnt/storage.
EXPECTED_STORAGE_VOLUMES: tuple[RavenStorageVolume, ...] = (
    RavenStorageVolume(
        key="microsd",
        name="MicroSD",
        path="/mnt/storage/microsd",
        expected_uuid="ff481ad2-e9bd-4868-8c8c-6729a461e4b4",
        expected_fstype="ext4",
        expected_label="SK256",
        required=True,
    ),
    RavenStorageVolume(
        key="toshiba_ext",
        name="Toshiba EXT",
        path="/mnt/storage/toshiba_ext",
        expected_uuid="0846863B46862A10",
        expected_fstype="ntfs3",
        expected_label="TOSHIBA EXT",
        required=True,
    ),
    RavenStorageVolume(
        key="pelican_backup",
        name="Pelican Backup",
        path="/mnt/storage/pelican_backup",
        expected_uuid="b6c0bc2c-5564-4615-bab6-2ff0ded11bbc",
        expected_fstype="ext4",
        expected_label="pelican_backup",
        required=True,
    ),
)

# Optional volumes — absent or automount-idle is a warning, not an error.
OPTIONAL_STORAGE_VOLUMES: tuple[RavenStorageVolume, ...] = (
    RavenStorageVolume(
        key="roost_spinning_0",
        name="Roost Spinning 0",
        path="/mnt/storage/roost_spinning_0",
        expected_uuid="13dc60fa-ba57-4c18-ac03-22e0ab8d6828",
        expected_fstype="ext4",
        expected_label="roost_spinning_0",
        required=False,
    ),
    RavenStorageVolume(
        key="raven_nvme",
        name="Raven NVME",
        path="/mnt/storage/raven_nvme",
        expected_uuid="EFBF-2FCB",
        expected_fstype="exfat",
        expected_label="RAVEN_NVME",
        required=False,
    ),
)

ALL_STORAGE_VOLUMES: tuple[RavenStorageVolume, ...] = (
    *EXPECTED_STORAGE_VOLUMES,
    *OPTIONAL_STORAGE_VOLUMES,
)

ALL_MONITORED_VOLUMES: tuple[RavenStorageVolume, ...] = (ROOT_VOLUME, *ALL_STORAGE_VOLUMES)

# Paths checked by shell health scripts (expected + optional only).
HEALTHCHECK_STORAGE_PATHS: tuple[str, ...] = tuple(v.path for v in ALL_STORAGE_VOLUMES)

# Crow default mount list: root + expected storage.
CROW_DEFAULT_MOUNTS: tuple[tuple[str, str], ...] = (
    (ROOT_VOLUME.name, ROOT_VOLUME.path),
    *((v.name, v.path) for v in EXPECTED_STORAGE_VOLUMES),
)

# Canary volume paths (storage only; root handled separately).
CANARY_STORAGE_PATHS: tuple[tuple[str, str], ...] = tuple(
    (v.key, v.path) for v in ALL_STORAGE_VOLUMES
)


def volume_by_path(path: str) -> RavenStorageVolume | None:
    normalized = path.rstrip("/") or "/"
    for volume in ALL_MONITORED_VOLUMES:
        if volume.path.rstrip("/") == normalized:
            return volume
    return None


def is_optional_path(path: str) -> bool:
    volume = volume_by_path(path)
    return volume is not None and not volume.required and volume.role != "root"
