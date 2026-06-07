"""Expected Raven storage drive definitions for the Vulture Dashboard."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

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


DEFAULT_EXPECTED_DRIVES: tuple[ExpectedDrive, ...] = (
    ExpectedDrive(
        name="Root filesystem",
        path=str(HOST_ROOT),
        expected_source="/dev/sda2",
        expected_fstype="ext4",
        role="root",
        required=True,
    ),
    ExpectedDrive(
        name="MicroSD",
        path="/mnt/storage/microsd",
        expected_uuid="ff481ad2-e9bd-4868-8c8c-6729a461e4b4",
        expected_fstype="ext4",
        expected_label="SK256",
        role="storage",
        required=True,
    ),
    ExpectedDrive(
        name="Toshiba EXT",
        path="/mnt/storage/toshiba_ext",
        expected_uuid="0846863B46862A10",
        expected_fstype="ntfs3",
        expected_label="TOSHIBA EXT",
        role="storage",
        required=True,
    ),
    ExpectedDrive(
        name="Pelican Backup",
        path="/mnt/storage/pelican_backup",
        expected_uuid="b6c0bc2c-5564-4615-bab6-2ff0ded11bbc",
        expected_fstype="ext4",
        expected_label="pelican_backup",
        role="storage",
        required=False,
    ),
    ExpectedDrive(
        name="Raven NVME",
        path="/mnt/storage/raven_nvme",
        expected_uuid="EFBF-2FCB",
        expected_fstype="exfat",
        expected_label="RAVEN_NVME",
        role="storage",
        required=False,
    ),
    ExpectedDrive(
        name="Roost Spinning 0",
        path="/mnt/storage/roost_spinning_0",
        expected_uuid="13dc60fa-ba57-4c18-ac03-22e0ab8d6828",
        expected_fstype="ext4",
        expected_label="roost_spinning_0",
        role="storage",
        required=False,
    ),
    ExpectedDrive(
        name="portable_beast",
        path="/mnt/storage/portable_beast",
        role="storage",
        required=False,
        legacy=True,
    ),
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
