"""Retention selection for completed Pelican recovery bundles."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .naming import is_completed_backup_filename, parse_backup_stamp, stamp_sort_key


@dataclass(frozen=True)
class RetentionPlan:
    keep: list[Path]
    delete: list[Path]
    newest: Path | None


def list_completed_backups(target_dir: Path) -> list[Path]:
    if not target_dir.is_dir():
        return []
    bundles = [
        entry
        for entry in target_dir.iterdir()
        if entry.is_file() and is_completed_backup_filename(entry.name)
    ]
    bundles.sort(key=lambda p: stamp_sort_key(parse_backup_stamp(p.name) or ""))
    return bundles


def plan_retention(
    target_dir: Path,
    *,
    retain_count: int,
    pending_new_bundle: Path | None = None,
) -> RetentionPlan:
    """
    Select older completed bundles for deletion.

    Never delete the newest successful bundle. Only names matching Pelican's
    completed-bundle pattern are eligible.
    """
    if retain_count < 1:
        raise ValueError("retain_count must be at least 1")

    existing = list_completed_backups(target_dir)
    combined = list(existing)
    if pending_new_bundle is not None and pending_new_bundle not in existing:
        combined.append(pending_new_bundle)
        combined.sort(key=lambda p: stamp_sort_key(parse_backup_stamp(p.name) or ""))

    if not combined:
        return RetentionPlan(keep=[], delete=[], newest=None)

    newest = combined[-1]
    keep = combined[-retain_count:]
    keep_names = {p.name for p in keep}
    delete = [p for p in existing if p.name not in keep_names]
    return RetentionPlan(keep=keep, delete=delete, newest=newest)
