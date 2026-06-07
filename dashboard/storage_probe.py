"""Robust Raven storage mount probing for the Vulture Dashboard."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from host_commands import run_host_command, systemctl_is_active
from parsers import DiskEntry, parse_df_human, parse_findmnt_line, parse_mountinfo
from storage_config import (
    DEFAULT_EXPECTED_DRIVES,
    ExpectedDrive,
    container_to_host_path,
    path_to_systemd_unit,
)
from subprocess_util import run_command

HOST_PROC = Path(os.environ.get("DASHBOARD_HOST_PROC", "/host/proc"))
STORAGE_CMD_TIMEOUT = 2.0

AUTOFS_SOURCES = frozenset({"systemd-1", "autofs", "none"})
STALE_MARKERS = (
    "transport endpoint is not connected",
    "stale file handle",
    "input/output error",
)

GREEN_STATUSES = frozenset({"OK", "OK_AUTOMOUNTED"})
YELLOW_STATUSES = frozenset(
    {
        "AUTOMOUNT_WAITING",
        "NOT_MOUNTED",
        "DEVICE_MISSING",
        "LEGACY_PATH",
    }
)


@dataclass
class StorageStatus:
    name: str
    path: str
    expected_uuid: str | None = None
    expected_fstype: str | None = None
    expected_label: str | None = None
    role: str = "storage"
    required: bool = True
    legacy: bool = False
    path_exists: bool = False
    is_mountpoint: bool = False
    automount_unit_state: str | None = None
    mount_unit_state: str | None = None
    actual_source: str | None = None
    actual_fstype: str | None = None
    actual_label: str | None = None
    actual_uuid: str | None = None
    size: str | None = None
    used: str | None = None
    available: str | None = None
    percent_used: float | None = None
    status: str = "ERROR"
    message: str = ""
    # Backward-compatible fields used by the dashboard template and warnings.
    label: str = ""
    mounted: bool = False
    filesystem: str | None = None
    warning: str | None = None

    def __post_init__(self) -> None:
        if not self.label:
            self.label = self.name
        self.mounted = self.status in GREEN_STATUSES
        self.filesystem = self.actual_source
        if self.status not in GREEN_STATUSES and self.message:
            self.warning = self.message


def _normalize_uuid(value: str | None) -> str | None:
    if not value:
        return None
    return value.replace("-", "").upper()


def _fstype_matches(expected: str, actual: str) -> bool:
    expected_l = expected.lower()
    actual_l = actual.lower()
    if expected_l == actual_l:
        return True
    if expected_l == "ntfs3" and actual_l in ("ntfs", "ntfs3", "fuseblk"):
        return True
    return False


def _is_autofs_placeholder(source: str | None, fstype: str | None) -> bool:
    if not source and not fstype:
        return False
    source_l = (source or "").lower()
    fstype_l = (fstype or "").lower()
    return source_l in AUTOFS_SOURCES or fstype_l == "autofs"


def _read_mountinfo() -> dict[str, tuple[str, str]]:
    for candidate in (HOST_PROC / "self/mountinfo", HOST_PROC / "1/mountinfo"):
        if candidate.is_file():
            try:
                return parse_mountinfo(candidate.read_text(encoding="utf-8"))
            except OSError:
                continue
    return {}


def _mountpoint_from_proc(host_path: str) -> tuple[bool, str | None, str | None]:
    normalized = host_path.rstrip("/") or "/"
    mounts = _read_mountinfo()
    entry = mounts.get(normalized)
    if entry is None:
        return False, None, None
    return True, entry[0], entry[1]


def _run_findmnt_mountpoint(host_path: str) -> tuple[str | None, str | None, str | None]:
    ok, out = run_host_command(
        ["findmnt", "--mountpoint", host_path, "-n", "-o", "SOURCE,FSTYPE,UUID"],
        timeout=STORAGE_CMD_TIMEOUT,
    )
    if not ok or not out.strip():
        return None, None, None
    return parse_findmnt_line(out)


def _df_for_path(path: str) -> tuple[DiskEntry | None, str | None]:
    ok, out = run_command(["df", "-h", path], timeout=STORAGE_CMD_TIMEOUT)
    if not ok:
        lower = (out or "").lower()
        if "timed out" in lower:
            return None, "DF_TIMEOUT"
        for marker in STALE_MARKERS:
            if marker in lower:
                return None, "STALE_MOUNT"
        return None, "ERROR"
    entries = parse_df_human(out)
    normalized = path.rstrip("/") or "/"
    for entry in entries:
        if entry.mount.rstrip("/") == normalized:
            return entry, None
    return entries[-1] if entries else None, None


def _root_source() -> str | None:
    host_root = container_to_host_path(str(Path(os.environ.get("DASHBOARD_HOST_ROOT", "/host/root"))))
    is_mp, source, _ = _mountpoint_from_proc(host_root)
    if is_mp and source:
        return source
    _, source, _ = _run_findmnt_mountpoint(host_root)
    if source:
        return source
    ok, out = run_command(["df", "-h", str(Path(os.environ.get("DASHBOARD_HOST_ROOT", "/host/root")))], timeout=STORAGE_CMD_TIMEOUT)
    if ok:
        entries = parse_df_human(out)
        if entries:
            return entries[0].filesystem
    return None


def _lookup_uuid(source: str | None) -> tuple[str | None, str | None]:
    if not source or _is_autofs_placeholder(source, None):
        return None, None
    if source.upper().startswith("UUID="):
        return source.split("=", 1)[1], None
    ok, out = run_host_command(
        ["blkid", "-o", "value", "-s", "UUID", source],
        timeout=STORAGE_CMD_TIMEOUT,
    )
    if not ok:
        lower = (out or "").lower()
        if "timed out" in lower:
            return None, "BLKID_TIMEOUT"
        return None, None
    value = out.splitlines()[0].strip() if out else ""
    return value or None, None


def _lookup_label(source: str | None) -> str | None:
    if not source or _is_autofs_placeholder(source, None):
        return None
    ok, out = run_host_command(
        ["blkid", "-o", "value", "-s", "LABEL", source],
        timeout=STORAGE_CMD_TIMEOUT,
    )
    if not ok or not out.strip():
        return None
    return out.splitlines()[0].strip()


def _systemd_unit_state(path: str) -> tuple[str | None, str | None]:
    unit_base = path_to_systemd_unit(path)
    if not unit_base:
        return None, None
    _, automount_state = systemctl_is_active(f"{unit_base}.automount", timeout=STORAGE_CMD_TIMEOUT)
    _, mount_state = systemctl_is_active(f"{unit_base}.mount", timeout=STORAGE_CMD_TIMEOUT)
    return automount_state or None, mount_state or None


def status_display_class(status: str, *, required: bool, legacy: bool = False) -> str:
    if status in GREEN_STATUSES:
        return "ok"
    if status in YELLOW_STATUSES and (not required or legacy):
        return "warn"
    if status == "AUTOMOUNT_WAITING":
        return "warn"
    return "bad"


def probe_expected_drive(drive: ExpectedDrive, *, root_source: str | None) -> StorageStatus:
    host_path = container_to_host_path(drive.path)
    path_obj = Path(drive.path)
    path_exists = path_obj.exists()

    proc_is_mp, proc_source, proc_fstype = _mountpoint_from_proc(host_path)
    findmnt_source, findmnt_fstype, findmnt_uuid = _run_findmnt_mountpoint(host_path)

    is_mountpoint = proc_is_mp or findmnt_source is not None

    actual_source = proc_source or findmnt_source
    actual_fstype = proc_fstype or findmnt_fstype
    actual_uuid = findmnt_uuid

    automount_state, mount_state = _systemd_unit_state(drive.path)

    autofs_only = is_mountpoint and _is_autofs_placeholder(actual_source, actual_fstype)
    real_mount = is_mountpoint and not autofs_only

    df_entry: DiskEntry | None = None
    df_error: str | None = None
    df_source: str | None = None

    if path_exists:
        df_entry, df_error = _df_for_path(drive.path)
        if df_entry:
            df_source = df_entry.filesystem

    if df_error == "STALE_MOUNT":
        return StorageStatus(
            name=drive.name,
            path=drive.path,
            expected_uuid=drive.expected_uuid,
            expected_fstype=drive.expected_fstype,
            expected_label=drive.expected_label,
            role=drive.role,
            required=drive.required,
            legacy=drive.legacy,
            path_exists=path_exists,
            is_mountpoint=is_mountpoint,
            automount_unit_state=automount_state,
            mount_unit_state=mount_state,
            actual_source=actual_source,
            actual_fstype=actual_fstype,
            status="STALE_MOUNT",
            message="Mount appears stale (transport endpoint not connected or I/O error).",
        )

    if not path_exists:
        return StorageStatus(
            name=drive.name,
            path=drive.path,
            expected_uuid=drive.expected_uuid,
            expected_fstype=drive.expected_fstype,
            expected_label=drive.expected_label,
            role=drive.role,
            required=drive.required,
            legacy=drive.legacy,
            path_exists=False,
            automount_unit_state=automount_state,
            mount_unit_state=mount_state,
            status="PATH_MISSING",
            message="Storage path does not exist on the host.",
        )

    if drive.legacy and not real_mount:
        if df_source and root_source and df_source == root_source:
            message = (
                f"Legacy path exists but is not mounted; df resolves to root filesystem "
                f"{root_source}. Former Portable Beast drive was renamed to pelican_backup."
            )
        else:
            message = "Legacy deprecated path; not configured as active storage."
        return StorageStatus(
            name=drive.name,
            path=drive.path,
            role=drive.role,
            required=drive.required,
            legacy=True,
            path_exists=True,
            is_mountpoint=is_mountpoint,
            automount_unit_state=automount_state,
            mount_unit_state=mount_state,
            actual_source=df_source,
            actual_fstype=actual_fstype,
            status="LEGACY_PATH",
            message=message,
        )

    if autofs_only:
        if mount_state in ("inactive", "dead", "failed"):
            status = "AUTOMOUNT_WAITING"
            message = (
                "Automount placeholder present but backing device is not mounted; "
                f"mount unit is {mount_state or 'inactive'}."
            )
        else:
            status = "AUTOMOUNT_WAITING"
            message = "Only systemd autofs placeholder is present; real disk is not mounted."
        return StorageStatus(
            name=drive.name,
            path=drive.path,
            expected_uuid=drive.expected_uuid,
            expected_fstype=drive.expected_fstype,
            expected_label=drive.expected_label,
            role=drive.role,
            required=drive.required,
            legacy=drive.legacy,
            path_exists=True,
            is_mountpoint=is_mountpoint,
            automount_unit_state=automount_state,
            mount_unit_state=mount_state,
            actual_source=actual_source,
            actual_fstype=actual_fstype,
            status=status,
            message=message,
        )

    if not real_mount:
        if automount_state in ("active", "waiting") and mount_state in ("inactive", "dead"):
            return StorageStatus(
                name=drive.name,
                path=drive.path,
                expected_uuid=drive.expected_uuid,
                expected_fstype=drive.expected_fstype,
                expected_label=drive.expected_label,
                role=drive.role,
                required=drive.required,
                legacy=drive.legacy,
                path_exists=True,
                is_mountpoint=False,
                automount_unit_state=automount_state,
                mount_unit_state=mount_state,
                actual_source=df_source,
                status="AUTOMOUNT_WAITING",
                message=(
                    "Automount unit is waiting but the drive is not currently mounted; "
                    "device may be unplugged."
                ),
            )

        if df_source and root_source and df_source == root_source and drive.role != "root":
            return StorageStatus(
                name=drive.name,
                path=drive.path,
                expected_uuid=drive.expected_uuid,
                expected_fstype=drive.expected_fstype,
                expected_label=drive.expected_label,
                role=drive.role,
                required=drive.required,
                legacy=drive.legacy,
                path_exists=True,
                is_mountpoint=False,
                automount_unit_state=automount_state,
                mount_unit_state=mount_state,
                actual_source=df_source,
                actual_fstype=actual_fstype,
                status="NOT_MOUNTED_PARENT_ROOT",
                message=(
                    f"Path exists but is not mounted; df resolves to root filesystem {root_source}. "
                    "Drive is unplugged or automount has not mounted it."
                ),
            )

        if not drive.required:
            status = "NOT_MOUNTED"
            message = "Optional storage path exists but is not mounted."
        else:
            status = "NOT_MOUNTED"
            message = "Required storage path exists but is not mounted."
        return StorageStatus(
            name=drive.name,
            path=drive.path,
            expected_uuid=drive.expected_uuid,
            expected_fstype=drive.expected_fstype,
            expected_label=drive.expected_label,
            role=drive.role,
            required=drive.required,
            legacy=drive.legacy,
            path_exists=True,
            is_mountpoint=False,
            automount_unit_state=automount_state,
            mount_unit_state=mount_state,
            actual_source=df_source,
            status=status,
            message=message,
        )

    if actual_source and root_source and actual_source == root_source and drive.role != "root":
        return StorageStatus(
            name=drive.name,
            path=drive.path,
            expected_uuid=drive.expected_uuid,
            expected_fstype=drive.expected_fstype,
            expected_label=drive.expected_label,
            role=drive.role,
            required=drive.required,
            legacy=drive.legacy,
            path_exists=True,
            is_mountpoint=True,
            automount_unit_state=automount_state,
            mount_unit_state=mount_state,
            actual_source=actual_source,
            actual_fstype=actual_fstype,
            status="NOT_MOUNTED_PARENT_ROOT",
            message=(
                f"Mount source matches root filesystem {root_source}; "
                "this path is not backed by its own storage device."
            ),
        )

    if df_error == "DF_TIMEOUT":
        return StorageStatus(
            name=drive.name,
            path=drive.path,
            expected_uuid=drive.expected_uuid,
            expected_fstype=drive.expected_fstype,
            expected_label=drive.expected_label,
            role=drive.role,
            required=drive.required,
            legacy=drive.legacy,
            path_exists=True,
            is_mountpoint=True,
            automount_unit_state=automount_state,
            mount_unit_state=mount_state,
            actual_source=actual_source,
            actual_fstype=actual_fstype,
            status="ERROR",
            message="Timed out reading disk usage (df).",
        )

    blkid_uuid, blkid_err = _lookup_uuid(actual_source)
    if not actual_uuid:
        actual_uuid = blkid_uuid
    actual_label = _lookup_label(actual_source)

    if blkid_err == "BLKID_TIMEOUT":
        return StorageStatus(
            name=drive.name,
            path=drive.path,
            expected_uuid=drive.expected_uuid,
            expected_fstype=drive.expected_fstype,
            expected_label=drive.expected_label,
            role=drive.role,
            required=drive.required,
            legacy=drive.legacy,
            path_exists=True,
            is_mountpoint=True,
            automount_unit_state=automount_state,
            mount_unit_state=mount_state,
            actual_source=actual_source,
            actual_fstype=actual_fstype,
            status="ERROR",
            message="Timed out reading block device identity (blkid).",
        )

    if drive.expected_uuid and actual_uuid:
        if _normalize_uuid(drive.expected_uuid) != _normalize_uuid(actual_uuid):
            return StorageStatus(
                name=drive.name,
                path=drive.path,
                expected_uuid=drive.expected_uuid,
                expected_fstype=drive.expected_fstype,
                expected_label=drive.expected_label,
                role=drive.role,
                required=drive.required,
                legacy=drive.legacy,
                path_exists=True,
                is_mountpoint=True,
                automount_unit_state=automount_state,
                mount_unit_state=mount_state,
                actual_source=actual_source,
                actual_fstype=actual_fstype,
                actual_label=actual_label,
                actual_uuid=actual_uuid,
                status="UUID_MISMATCH",
                message=(
                    f"Mounted device UUID {actual_uuid} does not match expected "
                    f"{drive.expected_uuid}."
                ),
            )

    if drive.expected_fstype and actual_fstype and not _fstype_matches(drive.expected_fstype, actual_fstype):
        return StorageStatus(
            name=drive.name,
            path=drive.path,
            expected_uuid=drive.expected_uuid,
            expected_fstype=drive.expected_fstype,
            expected_label=drive.expected_label,
            role=drive.role,
            required=drive.required,
            legacy=drive.legacy,
            path_exists=True,
            is_mountpoint=True,
            automount_unit_state=automount_state,
            mount_unit_state=mount_state,
            actual_source=actual_source,
            actual_fstype=actual_fstype,
            actual_label=actual_label,
            actual_uuid=actual_uuid,
            status="FSTYPE_MISMATCH",
            message=(
                f"Mounted filesystem type {actual_fstype} does not match expected "
                f"{drive.expected_fstype}."
            ),
        )

    if drive.expected_label and actual_label and actual_label != drive.expected_label:
        return StorageStatus(
            name=drive.name,
            path=drive.path,
            expected_uuid=drive.expected_uuid,
            expected_fstype=drive.expected_fstype,
            expected_label=drive.expected_label,
            role=drive.role,
            required=drive.required,
            legacy=drive.legacy,
            path_exists=True,
            is_mountpoint=True,
            automount_unit_state=automount_state,
            mount_unit_state=mount_state,
            actual_source=actual_source,
            actual_fstype=actual_fstype,
            actual_label=actual_label,
            actual_uuid=actual_uuid,
            status="LABEL_MISMATCH",
            message=(
                f"Mounted volume label {actual_label!r} does not match expected "
                f"{drive.expected_label!r}."
            ),
        )

    size = used = available = None
    percent_used = None
    if real_mount:
        if df_entry is None:
            df_entry, df_error = _df_for_path(drive.path)
        if df_entry:
            size, used, available = df_entry.size, df_entry.used, df_entry.available
            percent_used = df_entry.percent_used
        elif df_error:
            return StorageStatus(
                name=drive.name,
                path=drive.path,
                expected_uuid=drive.expected_uuid,
                expected_fstype=drive.expected_fstype,
                expected_label=drive.expected_label,
                role=drive.role,
                required=drive.required,
                legacy=drive.legacy,
                path_exists=True,
                is_mountpoint=True,
                automount_unit_state=automount_state,
                mount_unit_state=mount_state,
                actual_source=actual_source,
                actual_fstype=actual_fstype,
                actual_label=actual_label,
                actual_uuid=actual_uuid,
                status="ERROR",
                message="Could not read disk usage for mounted storage.",
            )

    status = "OK"
    message = f"Mounted on {actual_source}"
    if automount_state in ("active", "waiting"):
        status = "OK_AUTOMOUNTED"
        message = f"Mounted via automount on {actual_source}"

    if drive.role == "root" and drive.expected_source and actual_source != drive.expected_source:
        # Root still OK if mounted, but surface source mismatch in message only when severe.
        if not re.search(r"sda2", actual_source or "", re.I):
            message = f"Root mounted on {actual_source} (expected {drive.expected_source})"

    warning = None
    if percent_used is not None and percent_used >= 90:
        warning = f"Disk usage high ({percent_used:.0f}%)"

    result = StorageStatus(
        name=drive.name,
        path=drive.path,
        expected_uuid=drive.expected_uuid,
        expected_fstype=drive.expected_fstype,
        expected_label=drive.expected_label,
        role=drive.role,
        required=drive.required,
        legacy=drive.legacy,
        path_exists=True,
        is_mountpoint=True,
        automount_unit_state=automount_state,
        mount_unit_state=mount_state,
        actual_source=actual_source,
        actual_fstype=actual_fstype,
        actual_label=actual_label,
        actual_uuid=actual_uuid,
        size=size,
        used=used,
        available=available,
        percent_used=percent_used,
        status=status,
        message=message,
        warning=warning,
    )
    return result


def get_storage_status(
    expected_drives: tuple[ExpectedDrive, ...] | None = None,
) -> list[StorageStatus]:
    drives = expected_drives or DEFAULT_EXPECTED_DRIVES
    try:
        root_src = _root_source()
    except Exception:  # noqa: BLE001
        root_src = None
    results: list[StorageStatus] = []
    for drive in drives:
        try:
            results.append(probe_expected_drive(drive, root_source=root_src))
        except Exception as exc:  # noqa: BLE001 — dashboard must never crash on storage checks
            results.append(
                StorageStatus(
                    name=drive.name,
                    path=drive.path,
                    expected_uuid=drive.expected_uuid,
                    expected_fstype=drive.expected_fstype,
                    expected_label=drive.expected_label,
                    role=drive.role,
                    required=drive.required,
                    legacy=drive.legacy,
                    status="ERROR",
                    message=f"Storage probe failed: {exc}",
                )
            )
    return results
