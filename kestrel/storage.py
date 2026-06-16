"""SQLite storage for Kestrel energy interval readings."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from kestrel.models import EnergyInterval, utc_now_iso


SCHEMA = """
CREATE TABLE IF NOT EXISTS energy_intervals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider TEXT NOT NULL,
    meter_id_hash TEXT,
    account_id_hash TEXT,
    start_ts TEXT NOT NULL,
    end_ts TEXT NOT NULL,
    kwh REAL NOT NULL,
    raw_source TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(provider, start_ts, end_ts)
);
CREATE INDEX IF NOT EXISTS idx_energy_intervals_provider_start
    ON energy_intervals(provider, start_ts);
"""


def get_connection(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(db_path: Path) -> None:
    with get_connection(db_path) as conn:
        conn.executescript(SCHEMA)
        conn.commit()


def upsert_intervals(db_path: Path, intervals: list[EnergyInterval]) -> tuple[int, int]:
    """
    Insert interval rows, ignoring duplicates on (provider, start_ts, end_ts).

    Returns (inserted_count, skipped_count).
    """
    if not intervals:
        return 0, 0

    init_db(db_path)
    inserted = 0
    skipped = 0
    now = utc_now_iso()

    with get_connection(db_path) as conn:
        for row in intervals:
            created_at = row.created_at or now
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO energy_intervals (
                    provider, meter_id_hash, account_id_hash,
                    start_ts, end_ts, kwh, raw_source, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row.provider,
                    row.meter_id_hash,
                    row.account_id_hash,
                    row.start_ts,
                    row.end_ts,
                    row.kwh,
                    row.raw_source,
                    created_at,
                ),
            )
            if cursor.rowcount:
                inserted += 1
            else:
                skipped += 1
        conn.commit()

    return inserted, skipped


def fetch_intervals(
    db_path: Path,
    *,
    provider: str | None = None,
    start_ts: str | None = None,
    end_ts: str | None = None,
) -> list[EnergyInterval]:
    init_db(db_path)
    clauses: list[str] = []
    params: list[object] = []

    if provider:
        clauses.append("provider = ?")
        params.append(provider)
    if start_ts:
        clauses.append("start_ts >= ?")
        params.append(start_ts)
    if end_ts:
        clauses.append("end_ts <= ?")
        params.append(end_ts)

    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    query = f"""
        SELECT provider, meter_id_hash, account_id_hash, start_ts, end_ts, kwh, raw_source, created_at
        FROM energy_intervals
        {where}
        ORDER BY start_ts ASC
    """

    with get_connection(db_path) as conn:
        rows = conn.execute(query, params).fetchall()

    return [
        EnergyInterval(
            provider=row["provider"],
            meter_id_hash=row["meter_id_hash"],
            account_id_hash=row["account_id_hash"],
            start_ts=row["start_ts"],
            end_ts=row["end_ts"],
            kwh=float(row["kwh"]),
            raw_source=row["raw_source"],
            created_at=row["created_at"],
        )
        for row in rows
    ]


def count_intervals(db_path: Path, *, provider: str | None = None) -> int:
    init_db(db_path)
    if provider:
        with get_connection(db_path) as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM energy_intervals WHERE provider = ?",
                (provider,),
            ).fetchone()
    else:
        with get_connection(db_path) as conn:
            row = conn.execute("SELECT COUNT(*) AS n FROM energy_intervals").fetchone()
    return int(row["n"]) if row else 0
