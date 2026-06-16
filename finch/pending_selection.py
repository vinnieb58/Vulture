"""Pending Kroger search selection state — one active choice per chat key."""

from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from finch.config import DATA_DIR

PENDING_SELECTION_DB_PATH = Path(
    os.getenv("FINCH_PENDING_SELECTION_DB_PATH", str(DATA_DIR / "finch_pending_selection.db"))
)

_DEFAULT_TTL_MINUTES = 15


def _resolve_db_path(db_path: Path | None = None) -> Path:
    if db_path is not None:
        return db_path
    return Path(
        os.getenv("FINCH_PENDING_SELECTION_DB_PATH", str(DATA_DIR / "finch_pending_selection.db"))
    )


def pending_ttl_minutes() -> int:
    raw = os.getenv("FINCH_PENDING_SELECTION_TTL_MINUTES", str(_DEFAULT_TTL_MINUTES))
    try:
        return max(1, int(raw))
    except ValueError:
        return _DEFAULT_TTL_MINUTES


_SCHEMA = """
CREATE TABLE IF NOT EXISTS pending_selections (
    chat_key TEXT PRIMARY KEY,
    requested_item TEXT NOT NULL,
    normalized_name TEXT NOT NULL,
    search_query TEXT NOT NULL,
    quantity INTEGER NOT NULL DEFAULT 1,
    results_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);
"""


@dataclass(frozen=True)
class PendingSearchResult:
    product_id: str
    upc: str | None
    description: str
    brand: str | None = None
    size: str | None = None
    price: str | None = None

    def to_dict(self) -> dict[str, str | None]:
        return {
            "product_id": self.product_id,
            "upc": self.upc,
            "description": self.description,
            "brand": self.brand,
            "size": self.size,
            "price": self.price,
        }

    @classmethod
    def from_dict(cls, data: dict) -> PendingSearchResult:
        return cls(
            product_id=str(data["product_id"]),
            upc=data.get("upc"),
            description=str(data.get("description") or ""),
            brand=data.get("brand"),
            size=data.get("size"),
            price=data.get("price"),
        )


@dataclass(frozen=True)
class PendingSelection:
    chat_key: str
    requested_item: str
    normalized_name: str
    search_query: str
    quantity: int
    results: list[PendingSearchResult]
    created_at: str
    expires_at: str

    def to_dict(self) -> dict:
        return {
            "chat_key": self.chat_key,
            "requested_item": self.requested_item,
            "normalized_name": self.normalized_name,
            "search_query": self.search_query,
            "quantity": self.quantity,
            "results": [item.to_dict() for item in self.results],
            "created_at": self.created_at,
            "expires_at": self.expires_at,
        }


def make_chat_key(source: str, chat_id: str) -> str:
    return f"{source.strip().lower()}:{str(chat_id).strip()}"


def _connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def init_pending_selection_db(db_path: Path | None = None) -> None:
    path = _resolve_db_path(db_path)
    with _connect(path) as conn:
        conn.executescript(_SCHEMA)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _utc_now_iso() -> str:
    return _utc_now().isoformat()


def _parse_iso(ts: str) -> datetime:
    return datetime.fromisoformat(ts)


def _row_to_pending(row: sqlite3.Row) -> PendingSelection:
    raw_results = json.loads(row["results_json"])
    results = [PendingSearchResult.from_dict(item) for item in raw_results]
    return PendingSelection(
        chat_key=row["chat_key"],
        requested_item=row["requested_item"],
        normalized_name=row["normalized_name"],
        search_query=row["search_query"],
        quantity=int(row["quantity"]),
        results=results,
        created_at=row["created_at"],
        expires_at=row["expires_at"],
    )


def save_pending_selection(
    *,
    chat_key: str,
    requested_item: str,
    normalized_name: str,
    search_query: str,
    quantity: int,
    results: list[PendingSearchResult],
    db_path: Path | None = None,
    ttl_minutes: int | None = None,
) -> PendingSelection:
    path = _resolve_db_path(db_path)
    init_pending_selection_db(path)
    created = _utc_now()
    ttl = ttl_minutes if ttl_minutes is not None else pending_ttl_minutes()
    expires = created + timedelta(minutes=ttl)
    payload = json.dumps([item.to_dict() for item in results])
    with _connect(path) as conn:
        conn.execute(
            """
            INSERT INTO pending_selections (
                chat_key, requested_item, normalized_name, search_query,
                quantity, results_json, created_at, expires_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(chat_key) DO UPDATE SET
                requested_item=excluded.requested_item,
                normalized_name=excluded.normalized_name,
                search_query=excluded.search_query,
                quantity=excluded.quantity,
                results_json=excluded.results_json,
                created_at=excluded.created_at,
                expires_at=excluded.expires_at
            """,
            (
                chat_key,
                requested_item,
                normalized_name.strip().lower(),
                search_query,
                max(1, int(quantity)),
                payload,
                created.isoformat(),
                expires.isoformat(),
            ),
        )
    return PendingSelection(
        chat_key=chat_key,
        requested_item=requested_item,
        normalized_name=normalized_name.strip().lower(),
        search_query=search_query,
        quantity=max(1, int(quantity)),
        results=results,
        created_at=created.isoformat(),
        expires_at=expires.isoformat(),
    )


def get_pending_selection(
    chat_key: str,
    *,
    db_path: Path | None = None,
) -> PendingSelection | None:
    path = _resolve_db_path(db_path)
    init_pending_selection_db(path)
    with _connect(path) as conn:
        row = conn.execute(
            "SELECT * FROM pending_selections WHERE chat_key = ?",
            (chat_key,),
        ).fetchone()
    if not row:
        return None
    pending = _row_to_pending(row)
    if _parse_iso(pending.expires_at) <= _utc_now():
        clear_pending_selection(chat_key, db_path=path)
        return None
    return pending


def clear_pending_selection(chat_key: str, *, db_path: Path | None = None) -> bool:
    path = _resolve_db_path(db_path)
    init_pending_selection_db(path)
    with _connect(path) as conn:
        cur = conn.execute(
            "DELETE FROM pending_selections WHERE chat_key = ?",
            (chat_key,),
        )
    return cur.rowcount > 0
