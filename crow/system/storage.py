"""
Mounted storage visibility for Raven (read-only).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from crow.checks.system import get_disk_entries_for_paths, parse_df_output
from crow.config import EXPECTED_STORAGE_MOUNTS
from crow.system._status import StatusItem, StatusLevel


@dataclass(frozen=True)
class StorageMount:
    label: str
    path: str
    mounted: bool
    percent_used: float | None


def _is_mounted(path: str) -> bool:
    mount_path = Path(path)
    if not mount_path.exists():
        return False
    try:
        ok_entries = get_disk_entries_for_paths([path])
        return bool(ok_entries)
    except OSError:
        return False


def get_storage_summary(
    expected_mounts: list[tuple[str, str]] | None = None,
) -> list[StorageMount]:
    mounts = expected_mounts or EXPECTED_STORAGE_MOUNTS
    paths = [path for _, path in mounts]
    entries = {e.mount: e for e in get_disk_entries_for_paths(paths)}

    result: list[StorageMount] = []
    for label, path in mounts:
        entry = entries.get(path)
        mounted = entry is not None or _path_in_proc_mounts(path)
        pct = entry.percent_used if entry else None
        result.append(StorageMount(label=label, path=path, mounted=mounted, percent_used=pct))
    return result


def _path_in_proc_mounts(path: str) -> bool:
    mounts_file = Path("/proc/mounts")
    if not mounts_file.is_file():
        return Path(path).is_dir()
    try:
        text = mounts_file.read_text(encoding="utf-8")
    except OSError:
        return Path(path).is_dir()
    normalized = path.rstrip("/") or "/"
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[1].rstrip("/") == normalized:
            return True
    return False


def storage_level(mount: StorageMount, *, required: bool = True) -> StatusLevel:
    if mount.mounted:
        if mount.percent_used is not None and mount.percent_used >= 90:
            return "warn"
        return "ok"
    if required and mount.path != "/":
        return "warn"
    if not required:
        return "warn"
    return "fail"


def storage_to_status_item(mount: StorageMount) -> StatusItem:
    level = storage_level(mount)
    if mount.mounted and mount.percent_used is not None:
        detail = f"{mount.percent_used:.0f}% used"
    elif mount.mounted:
        detail = "mounted"
    else:
        detail = "MISSING"
    return StatusItem(label=mount.label, level=level, detail=detail)


def format_storage_line(mount: StorageMount) -> str:
    if mount.mounted and mount.percent_used is not None:
        return f"{mount.label:<16} {mount.percent_used:.0f}% used"
    if mount.mounted:
        return f"{mount.label:<16} mounted"
    return f"{mount.label:<16} MISSING"


def parse_df_human_output(text: str) -> dict[str, float]:
    """Parse `df -h` or `df -P` percent column keyed by mount point."""
    entries = parse_df_output(text.replace("K", "").replace("M", "").replace("G", ""))
    # Re-parse human df with percent in column 4
    result: dict[str, float] = {}
    lines = [ln for ln in text.splitlines() if ln.strip()]
    for line in lines[1:]:
        parts = line.split()
        if len(parts) < 6:
            continue
        mount = parts[5]
        pct_str = parts[4].rstrip("%")
        try:
            result[mount] = float(pct_str)
        except ValueError:
            continue
    for entry in entries:
        if entry.mount not in result and entry.percent_used is not None:
            result[entry.mount] = entry.percent_used
    return result
