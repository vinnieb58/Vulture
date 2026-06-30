"""Pelican Raven database snapshot configuration."""

from __future__ import annotations

import os
from pathlib import Path

from .config import DEFAULT_BACKUP_TARGET, DEFAULT_REPO_ROOT

__all__ = [
    "DB_SNAPSHOT_SCRIPT_VERSION",
    "DEFAULT_MAX_JSON_BYTES",
    "DEFAULT_REPO_ROOT",
    "DEFAULT_RETENTION_DAYS",
    "DEFAULT_SNAPSHOT_SUBDIR",
    "DEFAULT_SNAPSHOT_TARGET",
    "INCOMPLETE_SUFFIX",
    "OPTIONAL_JSON_STATE_FILES",
    "STAGING_DIR_NAME",
]

DB_SNAPSHOT_SCRIPT_VERSION = "1.0.0"

DEFAULT_SNAPSHOT_SUBDIR = "raven-db-snapshots"
DEFAULT_SNAPSHOT_TARGET = Path(
    os.environ.get(
        "PELICAN_DB_SNAPSHOT_TARGET",
        str(DEFAULT_BACKUP_TARGET / DEFAULT_SNAPSHOT_SUBDIR),
    )
)
DEFAULT_RETENTION_DAYS = int(os.environ.get("PELICAN_DB_SNAPSHOT_RETENTION_DAYS", "14"))

# Nest JSON state files are included only when present and below this size.
DEFAULT_MAX_JSON_BYTES = int(os.environ.get("PELICAN_DB_SNAPSHOT_MAX_JSON_BYTES", str(5 * 1024 * 1024)))

OPTIONAL_JSON_STATE_FILES = (
    "data/kestrel_nest_history.jsonl",
    "data/kestrel_nest_status.json",
)

STAGING_DIR_NAME = ".pelican-staging"
INCOMPLETE_SUFFIX = ".incomplete"
