"""Local Finch grocery trip ledger — tracks what Finch added this trip."""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from finch.config import DATA_DIR
from finch.preference_norm import normalize_preference_key

TRIP_LEDGER_DB_PATH = Path(
    os.getenv("FINCH_TRIP_LEDGER_DB_PATH", str(DATA_DIR / "finch_trip_ledger.db"))
)


def _resolve_db_path(db_path: Path | None = None) -> Path:
    if db_path is not None:
        return db_path
    return Path(os.getenv("FINCH_TRIP_LEDGER_DB_PATH", str(DATA_DIR / "finch_trip_ledger.db")))

_SCHEMA = """
CREATE TABLE IF NOT EXISTS trips (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    opened_at TEXT NOT NULL,
    closed_at TEXT,
    status TEXT NOT NULL DEFAULT 'open'
);

CREATE TABLE IF NOT EXISTS trip_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trip_id INTEGER NOT NULL,
    normalized_name TEXT NOT NULL,
    display_name TEXT,
    product_id TEXT,
    upc TEXT,
    quantity INTEGER NOT NULL,
    requested_text TEXT NOT NULL,
    source TEXT,
    added_at TEXT NOT NULL,
    undone INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (trip_id) REFERENCES trips(id)
);

CREATE TABLE IF NOT EXISTS trip_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

_CURRENT_TRIP_KEY = "current_trip_id"


@dataclass(frozen=True)
class TripItemRecord:
    id: int
    trip_id: int
    normalized_name: str
    display_name: str | None
    product_id: str | None
    upc: str | None
    quantity: int
    requested_text: str
    source: str | None
    added_at: str
    undone: bool


class TripDuplicateError(Exception):
    """Item already added in the current Finch grocery trip."""

    def __init__(self, normalized_name: str) -> None:
        self.normalized_name = normalized_name
        super().__init__(format_duplicate_message(normalized_name))


def _connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def init_trip_ledger_db(db_path: Path | None = None) -> None:
    path = _resolve_db_path(db_path)
    with _connect(path) as conn:
        conn.executescript(_SCHEMA)


def _row_to_item(row: sqlite3.Row) -> TripItemRecord:
    return TripItemRecord(
        id=row["id"],
        trip_id=row["trip_id"],
        normalized_name=row["normalized_name"],
        display_name=row["display_name"],
        product_id=row["product_id"],
        upc=row["upc"],
        quantity=row["quantity"],
        requested_text=row["requested_text"],
        source=row["source"],
        added_at=row["added_at"],
        undone=bool(row["undone"]),
    )


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _get_current_trip_id(conn: sqlite3.Connection) -> int | None:
    row = conn.execute(
        "SELECT value FROM trip_state WHERE key = ?",
        (_CURRENT_TRIP_KEY,),
    ).fetchone()
    if not row:
        return None
    try:
        return int(row["value"])
    except (TypeError, ValueError):
        return None


def _set_current_trip_id(conn: sqlite3.Connection, trip_id: int) -> None:
    conn.execute(
        """
        INSERT INTO trip_state (key, value) VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """,
        (_CURRENT_TRIP_KEY, str(trip_id)),
    )


def _open_trip(conn: sqlite3.Connection) -> int:
    ts = _utc_now_iso()
    cur = conn.execute(
        "INSERT INTO trips (opened_at, status) VALUES (?, 'open')",
        (ts,),
    )
    trip_id = int(cur.lastrowid)
    _set_current_trip_id(conn, trip_id)
    return trip_id


def get_or_create_open_trip(*, db_path: Path | None = None) -> int:
    path = _resolve_db_path(db_path)
    init_trip_ledger_db(path)
    with _connect(path) as conn:
        trip_id = _get_current_trip_id(conn)
        if trip_id is not None:
            row = conn.execute(
                "SELECT status FROM trips WHERE id = ?",
                (trip_id,),
            ).fetchone()
            if row and row["status"] == "open":
                return trip_id
        return _open_trip(conn)


def reset_trip(*, db_path: Path | None = None) -> int:
    """Close the current trip (if any) and start a new one."""
    path = _resolve_db_path(db_path)
    init_trip_ledger_db(path)
    ts = _utc_now_iso()
    with _connect(path) as conn:
        trip_id = _get_current_trip_id(conn)
        if trip_id is not None:
            conn.execute(
                """
                UPDATE trips
                SET status = 'closed', closed_at = ?
                WHERE id = ? AND status = 'open'
                """,
                (ts, trip_id),
            )
        return _open_trip(conn)


def find_trip_duplicate(
    *,
    trip_id: int,
    normalized_name: str,
    product_id: str | None,
    upc: str | None,
    db_path: Path | None = None,
) -> TripItemRecord | None:
    path = _resolve_db_path(db_path)
    init_trip_ledger_db(path)
    with _connect(path) as conn:
        rows = conn.execute(
            """
            SELECT * FROM trip_items
            WHERE trip_id = ? AND undone = 0
            ORDER BY id ASC
            """,
            (trip_id,),
        ).fetchall()
    norm = normalize_preference_key(normalized_name)
    for row in rows:
        item = _row_to_item(row)
        if normalize_preference_key(item.normalized_name) == norm:
            return item
        if product_id and item.product_id and item.product_id == product_id:
            return item
        if upc and item.upc and item.upc == upc:
            return item
    return None


def record_trip_add(
    *,
    trip_id: int,
    normalized_name: str,
    display_name: str | None,
    product_id: str | None,
    upc: str | None,
    quantity: int,
    requested_text: str,
    source: str | None = None,
    db_path: Path | None = None,
) -> TripItemRecord:
    path = _resolve_db_path(db_path)
    init_trip_ledger_db(path)
    ts = _utc_now_iso()
    with _connect(path) as conn:
        cur = conn.execute(
            """
            INSERT INTO trip_items (
                trip_id, normalized_name, display_name, product_id, upc,
                quantity, requested_text, source, added_at, undone
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
            """,
            (
                trip_id,
                normalize_preference_key(normalized_name),
                display_name,
                product_id,
                upc,
                quantity,
                requested_text,
                source,
                ts,
            ),
        )
        row = conn.execute(
            "SELECT * FROM trip_items WHERE id = ?",
            (cur.lastrowid,),
        ).fetchone()
    return _row_to_item(row)


def list_trip_items(
    trip_id: int,
    *,
    include_undone: bool = False,
    db_path: Path | None = None,
) -> list[TripItemRecord]:
    path = _resolve_db_path(db_path)
    init_trip_ledger_db(path)
    with _connect(path) as conn:
        if include_undone:
            rows = conn.execute(
                """
                SELECT * FROM trip_items
                WHERE trip_id = ?
                ORDER BY id ASC
                """,
                (trip_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT * FROM trip_items
                WHERE trip_id = ? AND undone = 0
                ORDER BY id ASC
                """,
                (trip_id,),
            ).fetchall()
    return [_row_to_item(row) for row in rows]


def list_added_today(*, db_path: Path | None = None) -> list[TripItemRecord]:
    path = _resolve_db_path(db_path)
    init_trip_ledger_db(path)
    today_utc = datetime.now(timezone.utc).date().isoformat()
    with _connect(path) as conn:
        rows = conn.execute(
            """
            SELECT * FROM trip_items
            WHERE undone = 0 AND substr(added_at, 1, 10) = ?
            ORDER BY id ASC
            """,
            (today_utc,),
        ).fetchall()
    return [_row_to_item(row) for row in rows]


def undo_last_trip_item(
    trip_id: int,
    *,
    db_path: Path | None = None,
) -> TripItemRecord | None:
    """Mark the most recent active item as undone (local-only; no Kroger cart remove)."""
    path = _resolve_db_path(db_path)
    init_trip_ledger_db(path)
    with _connect(path) as conn:
        row = conn.execute(
            """
            SELECT * FROM trip_items
            WHERE trip_id = ? AND undone = 0
            ORDER BY id DESC
            LIMIT 1
            """,
            (trip_id,),
        ).fetchone()
        if not row:
            return None
        conn.execute(
            "UPDATE trip_items SET undone = 1 WHERE id = ?",
            (row["id"],),
        )
        updated = conn.execute(
            "SELECT * FROM trip_items WHERE id = ?",
            (row["id"],),
        ).fetchone()
    return _row_to_item(updated)


def format_duplicate_message(normalized_name: str) -> str:
    name = normalized_name.strip().lower()
    return (
        f"I already added {name} this trip. "
        f"Reply 'add {name} again' or 'force add {name}' if you want another."
    )


def format_trip_item_line(item: TripItemRecord) -> str:
    label = item.display_name or item.normalized_name
    qty = f" x{item.quantity}" if item.quantity != 1 else ""
    source = f" ({item.source})" if item.source else ""
    return f"  • {label}{qty}{source} — {item.added_at}"


def format_added_list(
    items: list[TripItemRecord],
    *,
    trip_id: int | None = None,
    title: str = "Finch added list",
) -> str:
    if not items:
        if trip_id is not None:
            return f"{title} (trip {trip_id}): empty.\n(Kroger app is the source of truth for your live cart.)"
        return f"{title}: empty.\n(Kroger app is the source of truth for your live cart.)"
    lines = [title + ":"]
    if trip_id is not None:
        lines[0] = f"{title} (trip {trip_id}):"
    lines.append("(This is what Finch added — not your live Kroger cart.)")
    for item in items:
        lines.append(format_trip_item_line(item))
    return "\n".join(lines)
