"""Finch activity log — local record of cart operations."""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from finch.config import DATA_DIR

ACTIVITY_DB_PATH = Path(
    os.getenv("FINCH_ACTIVITY_DB_PATH", str(DATA_DIR / "finch_activity.db"))
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS activity (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    requested_text TEXT NOT NULL,
    resolved_alias TEXT,
    upc TEXT,
    product_id TEXT,
    quantity INTEGER NOT NULL,
    action TEXT NOT NULL,
    result TEXT NOT NULL
);
"""


@dataclass(frozen=True)
class ActivityRecord:
    id: int
    timestamp: str
    requested_text: str
    resolved_alias: str | None
    upc: str | None
    product_id: str | None
    quantity: int
    action: str
    result: str


def _connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def init_activity_db(db_path: Path | None = None) -> None:
    path = db_path or ACTIVITY_DB_PATH
    with _connect(path) as conn:
        conn.executescript(_SCHEMA)


def _row_to_record(row: sqlite3.Row) -> ActivityRecord:
    return ActivityRecord(
        id=row["id"],
        timestamp=row["timestamp"],
        requested_text=row["requested_text"],
        resolved_alias=row["resolved_alias"],
        upc=row["upc"],
        product_id=row["product_id"],
        quantity=row["quantity"],
        action=row["action"],
        result=row["result"],
    )


def log_activity(
    *,
    requested_text: str,
    resolved_alias: str | None,
    upc: str | None,
    product_id: str | None,
    quantity: int,
    action: str,
    result: str,
    db_path: Path | None = None,
) -> ActivityRecord:
    path = db_path or ACTIVITY_DB_PATH
    init_activity_db(path)
    ts = datetime.now(timezone.utc).isoformat()
    with _connect(path) as conn:
        cur = conn.execute(
            """
            INSERT INTO activity (
                timestamp, requested_text, resolved_alias, upc, product_id,
                quantity, action, result
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                ts,
                requested_text,
                resolved_alias,
                upc,
                product_id,
                quantity,
                action,
                result,
            ),
        )
        row_id = cur.lastrowid
        row = conn.execute("SELECT * FROM activity WHERE id = ?", (row_id,)).fetchone()
    return _row_to_record(row)


def list_cart_activity(
    *,
    limit: int = 50,
    db_path: Path | None = None,
) -> list[ActivityRecord]:
    path = db_path or ACTIVITY_DB_PATH
    init_activity_db(path)
    with _connect(path) as conn:
        rows = conn.execute(
            """
            SELECT * FROM activity
            WHERE action LIKE 'cart_%'
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [_row_to_record(row) for row in rows]


def format_activity_line(record: ActivityRecord) -> str:
    alias = record.resolved_alias or "—"
    upc = record.upc or "—"
    pid = record.product_id or "—"
    return (
        f"{record.timestamp} | {record.action} | {record.requested_text!r} | "
        f"alias: {alias!r} | upc: {upc} | product_id: {pid} | "
        f"qty: {record.quantity} | result: {record.result}"
    )
