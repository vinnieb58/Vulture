"""SQLite online backup and integrity validation."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

CONCERT_TABLES = ("concert_watches", "concert_events", "concert_alerts")


@dataclass(frozen=True)
class ConcertTableVerifyResult:
    ok: bool
    db_path: Path
    tables_present: dict[str, bool]
    counts: dict[str, int]
    message: str


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


def verify_concert_tables(db_path: Path) -> ConcertTableVerifyResult:
    """Verify Vulture Concerts tables exist and are readable with row counts."""
    if not db_path.is_file():
        return ConcertTableVerifyResult(
            ok=False,
            db_path=db_path,
            tables_present={name: False for name in CONCERT_TABLES},
            counts={name: 0 for name in CONCERT_TABLES},
            message=f"SQLite source not found: {db_path}",
        )

    tables_present: dict[str, bool] = {name: False for name in CONCERT_TABLES}
    counts: dict[str, int] = {name: 0 for name in CONCERT_TABLES}

    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        return ConcertTableVerifyResult(
            ok=False,
            db_path=db_path,
            tables_present=tables_present,
            counts=counts,
            message=f"Could not open SQLite database: {exc}",
        )

    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        existing = {row[0] for row in rows}
        for table in CONCERT_TABLES:
            tables_present[table] = table in existing
            if table in existing:
                row = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
                counts[table] = int(row[0]) if row else 0
    except sqlite3.Error as exc:
        return ConcertTableVerifyResult(
            ok=False,
            db_path=db_path,
            tables_present=tables_present,
            counts=counts,
            message=f"Concert table verification failed: {exc}",
        )
    finally:
        conn.close()

    present_count = sum(1 for present in tables_present.values() if present)
    if present_count == 0:
        return ConcertTableVerifyResult(
            ok=True,
            db_path=db_path,
            tables_present=tables_present,
            counts=counts,
            message="Concert tables not initialized (skipped)",
        )

    missing = [name for name, present in tables_present.items() if not present]
    if missing:
        return ConcertTableVerifyResult(
            ok=False,
            db_path=db_path,
            tables_present=tables_present,
            counts=counts,
            message=f"Missing concert tables: {', '.join(missing)}",
        )

    summary = ", ".join(f"{name}={counts[name]}" for name in CONCERT_TABLES)
    return ConcertTableVerifyResult(
        ok=True,
        db_path=db_path,
        tables_present=tables_present,
        counts=counts,
        message=f"Concert tables verified ({summary})",
    )


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
