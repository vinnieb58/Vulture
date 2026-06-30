"""Age-based retention for Pelican Raven database snapshots."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .db_snapshot_naming import (
    is_completed_snapshot_filename,
    parse_snapshot_stamp,
    stamp_sort_key,
)


@dataclass(frozen=True)
class SnapshotRetentionPlan:
    keep: list[Path]
    delete: list[Path]
    cutoff: datetime


def list_completed_snapshots(target_dir: Path) -> list[Path]:
    if not target_dir.is_dir():
        return []
    snapshots = [
        entry
        for entry in target_dir.iterdir()
        if entry.is_file() and is_completed_snapshot_filename(entry.name)
    ]
    snapshots.sort(key=lambda p: stamp_sort_key(parse_snapshot_stamp(p.name) or ""))
    return snapshots


def plan_snapshot_retention(
    target_dir: Path,
    *,
    retention_days: int,
    now: datetime | None = None,
    pending_new_snapshot: Path | None = None,
) -> SnapshotRetentionPlan:
    """
    Select completed snapshots older than retention_days for deletion.

    Only names matching the Pelican DB snapshot pattern are eligible.
    """
    if retention_days < 1:
        raise ValueError("retention_days must be at least 1")

    moment = now or datetime.now(timezone.utc)
    cutoff = moment - timedelta(days=retention_days)

    existing = list_completed_snapshots(target_dir)
    combined = list(existing)
    if pending_new_snapshot is not None and pending_new_snapshot not in existing:
        combined.append(pending_new_snapshot)
        combined.sort(key=lambda p: stamp_sort_key(parse_snapshot_stamp(p.name) or ""))

    keep: list[Path] = []
    delete: list[Path] = []
    for path in combined:
        stamp = parse_snapshot_stamp(path.name)
        if not stamp:
            continue
        captured_at = stamp_sort_key(stamp)
        if captured_at >= cutoff:
            keep.append(path)
        elif path in existing:
            delete.append(path)

    return SnapshotRetentionPlan(keep=keep, delete=delete, cutoff=cutoff)
