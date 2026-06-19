"""Pelican backup target mount validation."""

from __future__ import annotations

import errno
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .config import AUTOFS_SOURCES, DEFAULT_MOUNT_ACCESS_TIMEOUT


@dataclass(frozen=True)
class MountVerification:
    ok: bool
    path: str
    message: str
    backing_source: str | None = None
    backing_fstype: str | None = None
    root_source: str | None = None


def _is_autofs_placeholder(source: str | None, fstype: str | None) -> bool:
    if not source and not fstype:
        return False
    source_l = (source or "").lower()
    fstype_l = (fstype or "").lower()
    return source_l in AUTOFS_SOURCES or fstype_l == "autofs"


def _run_findmnt(mountpoint: str, timeout: float) -> tuple[str | None, str | None]:
    try:
        proc = subprocess.run(
            ["findmnt", "--mountpoint", mountpoint, "-n", "-o", "SOURCE,FSTYPE"],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return None, str(exc)

    if proc.returncode != 0 or not proc.stdout.strip():
        return None, proc.stderr.strip() or "findmnt returned no data"

    best_source: str | None = None
    best_fstype: str | None = None
    for line in proc.stdout.splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        source, fstype = parts[0], parts[1]
        if not _is_autofs_placeholder(source, fstype):
            return source, fstype
        best_source, best_fstype = source, fstype
    return best_source, best_fstype


def _root_backing_source(timeout: float) -> str | None:
    source, _ = _run_findmnt("/", timeout)
    return source


def _trigger_automount(path: Path, timeout: float) -> tuple[bool, str | None]:
    """Force automount by touching the directory; detect stale ENODEV mounts."""
    try:
        path.exists()
        if path.is_dir():
            next(os.scandir(path), None)
        else:
            path.stat()
    except OSError as exc:
        if exc.errno == errno.ENODEV:
            return False, "no such device (automount stale or drive missing)"
        return False, str(exc)
    except StopIteration:
        pass
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)

    # Give systemd/autofs a moment to attach a backing mount after directory access.
    _ = timeout  # reserved for future polling; access itself is the primary trigger.
    return True, None


def verify_backup_target(
    target: Path,
    *,
    timeout: float = DEFAULT_MOUNT_ACCESS_TIMEOUT,
    findmnt_runner=_run_findmnt,
    root_source_resolver=_root_backing_source,
    access_runner=_trigger_automount,
) -> MountVerification:
    """
    Verify the Pelican backup target has a real backing filesystem.

    Refuses empty autofs placeholders and targets that resolve to the root filesystem.
    """
    mountpoint = str(target)
    if not target.exists():
        return MountVerification(
            ok=False,
            path=mountpoint,
            message=f"Backup target path does not exist: {mountpoint}",
        )

    access_ok, access_error = access_runner(target, timeout)
    if not access_ok:
        return MountVerification(
            ok=False,
            path=mountpoint,
            message=f"Pelican drive unavailable: {access_error}",
        )

    source, fstype = findmnt_runner(mountpoint, timeout)
    if source is None:
        return MountVerification(
            ok=False,
            path=mountpoint,
            message="Backup target is not a mountpoint with a backing device",
        )

    if _is_autofs_placeholder(source, fstype):
        return MountVerification(
            ok=False,
            path=mountpoint,
            message=(
                "Automount placeholder detected; Pelican drive is not mounted "
                f"(source={source}, fstype={fstype})"
            ),
            backing_source=source,
            backing_fstype=fstype,
        )

    root_source = root_source_resolver(timeout)
    if root_source and source == root_source:
        return MountVerification(
            ok=False,
            path=mountpoint,
            message=(
                "Backup target resolves to the root filesystem; refusing to write "
                "recovery bundles into an empty autofs mountpoint directory"
            ),
            backing_source=source,
            backing_fstype=fstype,
            root_source=root_source,
        )

    return MountVerification(
        ok=True,
        path=mountpoint,
        message="Pelican backup target verified",
        backing_source=source,
        backing_fstype=fstype,
        root_source=root_source,
    )
