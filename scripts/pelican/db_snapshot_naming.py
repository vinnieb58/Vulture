"""Pelican Raven database snapshot naming helpers."""

from __future__ import annotations

import re
from datetime import datetime, timezone

from .db_snapshot_config import INCOMPLETE_SUFFIX
from .naming import archive_suffix, format_backup_timestamp

DB_SNAPSHOT_BUNDLE_RE = re.compile(
    r"^raven-db-snapshot-(?P<stamp>\d{8}T\d{6}Z)\.tar\.(?:zst|gz)$"
)
INCOMPLETE_SNAPSHOT_DIR_RE = re.compile(
    r"^raven-db-snapshot-(?P<stamp>\d{8}T\d{6}Z)\.incomplete$"
)


def build_snapshot_basename(stamp: str) -> str:
    return f"raven-db-snapshot-{stamp}"


def complete_snapshot_name(stamp: str, *, prefer_zstd: bool = True) -> str:
    return f"{build_snapshot_basename(stamp)}{archive_suffix(prefer_zstd=prefer_zstd)}"


def incomplete_snapshot_dir_name(stamp: str) -> str:
    return f"{build_snapshot_basename(stamp)}{INCOMPLETE_SUFFIX}"


def is_completed_snapshot_filename(name: str) -> bool:
    return DB_SNAPSHOT_BUNDLE_RE.match(name) is not None


def is_incomplete_snapshot_dirname(name: str) -> bool:
    return INCOMPLETE_SNAPSHOT_DIR_RE.match(name) is not None


def is_db_snapshot_managed_name(name: str) -> bool:
    return is_completed_snapshot_filename(name) or is_incomplete_snapshot_dirname(name)


def parse_snapshot_stamp(name: str) -> str | None:
    match = DB_SNAPSHOT_BUNDLE_RE.match(name) or INCOMPLETE_SNAPSHOT_DIR_RE.match(name)
    if not match:
        return None
    return match.group("stamp")


def stamp_sort_key(stamp: str) -> datetime:
    return datetime.strptime(stamp, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)


__all__ = [
    "build_snapshot_basename",
    "complete_snapshot_name",
    "format_backup_timestamp",
    "incomplete_snapshot_dir_name",
    "is_completed_snapshot_filename",
    "is_db_snapshot_managed_name",
    "is_incomplete_snapshot_dirname",
    "parse_snapshot_stamp",
    "stamp_sort_key",
]
