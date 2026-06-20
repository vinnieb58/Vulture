"""Pelican backup bundle naming helpers."""

from __future__ import annotations

import re
import shutil
from datetime import datetime, timezone

from .config import BACKUP_BUNDLE_RE, INCOMPLETE_DIR_RE, INCOMPLETE_SUFFIX


def archive_suffix(prefer_zstd: bool = True) -> str:
    if prefer_zstd and shutil.which("zstd"):
        return ".tar.zst"
    return ".tar.gz"


def complete_bundle_name(stamp: str, *, prefer_zstd: bool = True) -> str:
    return f"{build_backup_basename(stamp)}{archive_suffix(prefer_zstd)}"


def format_backup_timestamp(when: datetime | None = None) -> str:
    """Return UTC stamp used in bundle names: YYYYMMDDTHHMMSSZ."""
    moment = when or datetime.now(timezone.utc)
    return moment.strftime("%Y%m%dT%H%M%SZ")


def build_backup_basename(stamp: str) -> str:
    return f"raven-recovery-{stamp}"


def incomplete_dir_name(stamp: str) -> str:
    return f"{build_backup_basename(stamp)}{INCOMPLETE_SUFFIX}"


def is_completed_backup_filename(name: str) -> bool:
    return BACKUP_BUNDLE_RE.match(name) is not None


def is_incomplete_backup_dirname(name: str) -> bool:
    return INCOMPLETE_DIR_RE.match(name) is not None


def is_pelican_managed_name(name: str) -> bool:
    return is_completed_backup_filename(name) or is_incomplete_backup_dirname(name)


def parse_backup_stamp(name: str) -> str | None:
    match = BACKUP_BUNDLE_RE.match(name) or INCOMPLETE_DIR_RE.match(name)
    if not match:
        return None
    return match.group("stamp")


def stamp_sort_key(stamp: str) -> datetime:
    return datetime.strptime(stamp, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
