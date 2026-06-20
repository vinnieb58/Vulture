"""SQLite online backup and integrity validation."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class SqliteBackupResult:
    ok: bool
    source: Path
    destination: Path
    integrity_result: str
    message: str


def backup_sqlite_database(source: Path, destination: Path) -> None:
    """Create a consistent SQLite backup using the online backup API."""
    if not source.is_file():
        raise FileNotFoundError(f"SQLite source not found: {source}")

    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        destination.unlink()

    src = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    try:
        dst = sqlite3.connect(destination)
        try:
            src.backup(dst)
            dst.commit()
        finally:
            dst.close()
    finally:
        src.close()


def run_integrity_check(db_path: Path) -> str:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        row = conn.execute("PRAGMA integrity_check").fetchone()
    finally:
        conn.close()
    return row[0] if row else "unknown"


def backup_and_verify_sqlite(source: Path, destination: Path) -> SqliteBackupResult:
    try:
        backup_sqlite_database(source, destination)
    except OSError as exc:
        return SqliteBackupResult(
            ok=False,
            source=source,
            destination=destination,
            integrity_result="not run",
            message=f"SQLite backup failed: {exc}",
        )
    except sqlite3.Error as exc:
        return SqliteBackupResult(
            ok=False,
            source=source,
            destination=destination,
            integrity_result="not run",
            message=f"SQLite backup failed: {exc}",
        )

    integrity = run_integrity_check(destination)
    ok = integrity.lower() == "ok"
    message = "SQLite backup integrity check passed" if ok else (
        f"SQLite integrity check failed: {integrity}"
    )
    return SqliteBackupResult(
        ok=ok,
        source=source,
        destination=destination,
        integrity_result=integrity,
        message=message,
    )
