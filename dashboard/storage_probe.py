"""Robust Raven storage mount probing for the Vulture Dashboard."""

from __future__ import annotations

import errno
import os
import re
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from dataclasses import dataclass
from pathlib import Path

from host_commands import run_host_command, systemctl_is_active
from parsers import DiskEntry, parse_df_human, parse_findmnt_line, parse_mountinfo
from storage_config import (
    DEFAULT_EXPECTED_DRIVES,
    DISK_CRITICAL_PERCENT,
    DISK_WARN_PERCENT,
    ExpectedDrive,
    container_to_host_path,
    path_to_systemd_unit,
)
from subprocess_util import run_command

HOST_PROC = Path(os.environ.get("DASHBOARD_HOST_PROC", "/host/proc"))
STORAGE_CMD_TIMEOUT = 2.0
PATH_ACCESS_TIMEOUT = 2.0

AUTOFS_SOURCES = frozenset({"systemd-1", "autofs", "none"})
STALE_MARKERS = (
    "transport endpoint is not connected",
    "stale file handle",
    "input/output error",
    "no such device",
    "unknown error",
)
ENODEV_MARKERS = (
    "no such device",
    "ENODEV",
)

GREEN_STATUSES = frozenset({"OK", "OK_AUTOMOUNTED"})
YELLOW_STATUSES = frozenset(
    {
        "AUTOMOUNT_WAITING",
        "NOT_MOUNTED",
        "NOT_MOUNTED_PARENT_ROOT",
        "DEVICE_MISSING",
        "PATH_MISSING",
        "LEGACY_PATH",
        "STALE_AUTOMOUNT",
    }
)

_path_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="dashboard-storage")


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
    display_label: str = ""

    def __post_init__(self) -> None:
        if not self.label:
            self.label = self.name
        self.mounted = self.status in GREEN_STATUSES
        self.filesystem = self.actual_source
        # LEGACY_PATH is informational; legacy drives are not expected to be active,
        # so they must not appear as dashboard warnings.
        if self.status not in GREEN_STATUSES and self.status != "LEGACY_PATH" and self.message:
            self.warning = self.message
        if not self.display_label:
            self.display_label = status_display_label(
                self.status,
                required=self.required,
                legacy=self.legacy,
                percent_used=self.percent_used,
            )


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


def _text_indicates_enodev(text: str | None) -> bool:
    if not text:
        return False
    lower = text.lower()
    return any(marker in lower for marker in ENODEV_MARKERS)


def _classify_io_failure(text: str | None) -> str:
    lower = (text or "").lower()
    if "timed out" in lower:
        return "DF_TIMEOUT"
    if _text_indicates_enodev(text):
        return "STALE_AUTOMOUNT"
    for marker in STALE_MARKERS:
        if marker in lower:
            return "STALE_MOUNT"
    return "ERROR"


def _path_access_check(path: str) -> tuple[bool, str | None]:
    """Verify a mount path is reachable; stale automounts often raise ENODEV."""

    def _probe() -> tuple[bool, str | None]:
        target = Path(path)
        try:
            if not target.exists():
                return False, "path not found"
            if target.is_dir():
                next(target.iterdir(), None)
            else:
                target.stat()
        except OSError as exc:
            if exc.errno == errno.ENODEV:
                return False, "no such device"
            return False, str(exc)
        return True, None

    future = _path_executor.submit(_probe)
    try:
        return future.result(timeout=PATH_ACCESS_TIMEOUT)
    except FuturesTimeoutError:
        future.cancel()
        return False, "path access timed out"
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


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
    if ok and out.strip():
        return _pick_best_findmnt_entry(out)

    # Host recursive / parent queries can be empty while per-mountpoint findmnt works.
    # Fall back to the container bind-mount namespace for /mnt/storage paths.
    if host_path.startswith("/mnt/storage"):
        local_ok, local_out = run_command(
            ["findmnt", "--mountpoint", host_path, "-n", "-o", "SOURCE,FSTYPE,UUID"],
            timeout=STORAGE_CMD_TIMEOUT,
        )
        if local_ok and local_out.strip():
            return _pick_best_findmnt_entry(local_out)

    return None, None, None


def _trigger_automount(path: str) -> tuple[bool, str | None]:
    """List/stat a path so systemd automount can attach a backing device."""
    return _path_access_check(path)


def _pick_best_findmnt_entry(text: str) -> tuple[str | None, str | None, str | None]:
    """Prefer a real backing filesystem over an autofs placeholder."""
    entries = [parse_findmnt_line(line) for line in text.splitlines() if line.strip()]
    if not entries:
        return None, None, None
    for source, fstype, uuid in entries:
        if not _is_autofs_placeholder(source, fstype):
            return source, fstype, uuid
    return entries[0]


def _df_for_path(path: str) -> tuple[DiskEntry | None, str | None]:
    ok, out = run_command(["df", "-h", path], timeout=STORAGE_CMD_TIMEOUT)
    if not ok:
        return None, _classify_io_failure(out)
    entries = parse_df_human(out)
    normalized = path.rstrip("/") or "/"
    for entry in entries:
        if entry.mount.rstrip("/") == normalized:
            return entry, None
    if entries:
        return None, "MOUNTPOINT_MISMATCH"
    return None, "ERROR"


def _resolve_backing_mount(
    host_path: str,
) -> tuple[bool, bool, str | None, str | None, str | None]:
    """
    Return (is_mountpoint, autofs_present, backing_source, backing_fstype, backing_uuid).

    Host findmnt is preferred over /proc mountinfo so stale kernel entries cannot
    masquerade as a healthy block-device mount.
    """
    proc_is_mp, proc_source, proc_fstype = _mountpoint_from_proc(host_path)
    findmnt_source, findmnt_fstype, findmnt_uuid = _run_findmnt_mountpoint(host_path)

    proc_autofs = proc_is_mp and _is_autofs_placeholder(proc_source, proc_fstype)
    findmnt_autofs = findmnt_source is not None and _is_autofs_placeholder(
        findmnt_source, findmnt_fstype
    )
    autofs_present = proc_autofs or findmnt_autofs

    backing_source: str | None = None
    backing_fstype: str | None = None
    backing_uuid: str | None = None

    if findmnt_source is not None and not findmnt_autofs:
        backing_source, backing_fstype, backing_uuid = (
            findmnt_source,
            findmnt_fstype,
            findmnt_uuid,
        )
    elif findmnt_source is None and proc_is_mp and not proc_autofs:
        backing_source, backing_fstype = proc_source, proc_fstype

    is_mountpoint = proc_is_mp or findmnt_source is not None
    return is_mountpoint, autofs_present, backing_source, backing_fstype, backing_uuid


def _stale_automount_message(*, automount_state: str | None, detail: str | None = None) -> str:
    base = "Automount exists, but backing device is unavailable."
    if detail and detail not in base:
        return f"{base} ({detail})"
    if automount_state in ("active", "waiting"):
        return base
    return base


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
    if status in YELLOW_STATUSES:
        if not required or legacy:
            return "warn"
        if status in (
            "AUTOMOUNT_WAITING",
            "STALE_AUTOMOUNT",
            "NOT_MOUNTED_PARENT_ROOT",
            "NOT_MOUNTED",
            "PATH_MISSING",
            "DEVICE_MISSING",
        ):
            return "warn"
    if status == "AUTOMOUNT_WAITING":
        return "warn"
    if status == "STALE_AUTOMOUNT" and not required:
        return "warn"
    if status == "NOT_MOUNTED_PARENT_ROOT" and not required:
        return "warn"
    if status == "PATH_MISSING" and not required:
        return "warn"
    return "bad"


def status_display_label(
    status: str,
    *,
    required: bool,
    legacy: bool = False,
    percent_used: float | None = None,
) -> str:
    """Human-facing storage state for Nest/dashboard UI."""
    if status in GREEN_STATUSES:
        if percent_used is not None and percent_used >= DISK_WARN_PERCENT:
            return "High usage"
        return "Mounted"
    if status == "AUTOMOUNT_WAITING":
        return "Automount pending"
    if status in ("STALE_AUTOMOUNT", "NOT_MOUNTED_PARENT_ROOT"):
        return "Automount pending"
    if status == "STALE_MOUNT":
        return "Error"
    if status in ("NOT_MOUNTED", "DEVICE_MISSING", "PATH_MISSING"):
        if not required or legacy:
            return "Optional missing"
        return "Error"
    if status in ("UUID_MISMATCH", "FSTYPE_MISMATCH", "LABEL_MISMATCH", "ERROR"):
        return "Error"
    if status == "LEGACY_PATH":
        return "Optional missing"
    return "Error"


def probe_expected_drive(drive: ExpectedDrive, *, root_source: str | None) -> StorageStatus:
    host_path = container_to_host_path(drive.path)
    path_obj = Path(drive.path)
    path_exists = path_obj.exists()

    if path_exists and drive.role == "storage":
        _trigger_automount(drive.path)

    is_mountpoint, autofs_present, actual_source, actual_fstype, actual_uuid = (
        _resolve_backing_mount(host_path)
    )

    automount_state, mount_state = _systemd_unit_state(drive.path)

    autofs_only = autofs_present and actual_source is None
    real_mount = actual_source is not None and not _is_autofs_placeholder(
        actual_source, actual_fstype
    )

    df_entry: DiskEntry | None = None
    df_error: str | None = None
    df_source: str | None = None
    access_ok = False
    access_error: str | None = None

    if path_exists:
        access_ok, access_error = _path_access_check(drive.path)
        df_entry, df_error = _df_for_path(drive.path)
        if df_entry:
            df_source = df_entry.filesystem

    if drive.role == "storage" and path_exists and not actual_source and df_source:
        if not root_source or df_source != root_source:
            actual_source = df_source
            is_mountpoint = True
            autofs_present = False

    if drive.role == "storage" and path_exists and autofs_present and not actual_source:
        _trigger_automount(drive.path)
        is_mountpoint, autofs_present, actual_source, actual_fstype, actual_uuid = (
            _resolve_backing_mount(host_path)
        )
        if not df_entry:
            df_entry, df_error = _df_for_path(drive.path)
            if df_entry:
                df_source = df_entry.filesystem
                if not actual_source and df_source and df_source != root_source:
                    actual_source = df_source
                    is_mountpoint = True
                    autofs_present = False

    io_failure = None
    if df_error in ("STALE_MOUNT", "STALE_AUTOMOUNT", "DF_TIMEOUT"):
        io_failure = df_error
    elif access_error:
        io_failure = _classify_io_failure(access_error)
    elif df_error:
        io_failure = df_error

    if io_failure == "STALE_MOUNT":
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

    if io_failure == "STALE_AUTOMOUNT" or (
        autofs_present and not real_mount and _text_indicates_enodev(access_error or df_error or "")
    ):
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
            actual_source=None,
            actual_fstype="autofs" if autofs_present else actual_fstype,
            status="STALE_AUTOMOUNT",
            message=_stale_automount_message(
                automount_state=automount_state,
                detail=access_error or df_error,
            ),
        )

    if io_failure == "DF_TIMEOUT":
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
            status="ERROR",
            message="Timed out reading disk usage (df).",
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
            actual_source=None,
            actual_fstype="autofs",
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

    if real_mount and not access_ok:
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
            actual_source=None if autofs_present else actual_source,
            actual_fstype=actual_fstype,
            status="STALE_AUTOMOUNT" if autofs_present else "ERROR",
            message=_stale_automount_message(
                automount_state=automount_state,
                detail=access_error,
            )
            if autofs_present
            else (access_error or "Mount path is not accessible."),
        )

    if df_error == "MOUNTPOINT_MISMATCH" or (
        df_source and root_source and df_source == root_source and drive.role != "root"
    ):
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
        # Inside a Docker container the host root filesystem appears as overlay because
        # the container itself runs on overlay. Skip the fstype check for root drives
        # when the observed type is an overlay variant — it cannot be accurate from here.
        if drive.role == "root" and actual_fstype.lower() in ("overlay", "overlayfs"):
            pass
        else:
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
                df_source = df_entry.filesystem
        if df_entry:
            size, used, available = df_entry.size, df_entry.used, df_entry.available
            percent_used = df_entry.percent_used
        elif df_error == "STALE_AUTOMOUNT":
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
                actual_source=None,
                actual_fstype=actual_fstype,
                status="STALE_AUTOMOUNT",
                message=_stale_automount_message(automount_state=automount_state),
            )
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
                is_mountpoint=is_mountpoint,
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
    if percent_used is not None and percent_used >= DISK_WARN_PERCENT:
        warning = f"Disk usage high ({percent_used:.0f}%)"
    if (
        drive.role == "root"
        and percent_used is not None
        and percent_used >= DISK_CRITICAL_PERCENT
    ):
        warning = f"Root disk usage critical ({percent_used:.0f}%)"

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
