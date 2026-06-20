"""Pending Kroger search selection state — one active choice per chat key."""

from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from finch.config import DATA_DIR
from finch.preference_norm import normalize_preference_key

PENDING_SELECTION_DB_PATH = Path(
    os.getenv("FINCH_PENDING_SELECTION_DB_PATH", str(DATA_DIR / "finch_pending_selection.db"))
)

_DEFAULT_TTL_MINUTES = 15
_DEFAULT_PAGE_SIZE = 10


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


def search_page_size() -> int:
    raw = os.getenv("FINCH_SEARCH_RESULT_LIMIT", str(_DEFAULT_PAGE_SIZE))
    try:
        page_size = int(raw)
    except ValueError:
        page_size = _DEFAULT_PAGE_SIZE
    return max(1, min(page_size, 50))


_SCHEMA = """
CREATE TABLE IF NOT EXISTS pending_selections (
    chat_key TEXT PRIMARY KEY,
    requested_item TEXT NOT NULL,
    normalized_name TEXT NOT NULL,
    search_query TEXT NOT NULL,
    quantity INTEGER NOT NULL DEFAULT 1,
    results_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    cached_results_json TEXT,
    page_offset INTEGER NOT NULL DEFAULT 0,
    page_size INTEGER NOT NULL DEFAULT 10,
    total_count INTEGER
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
    cached_results: list[PendingSearchResult]
    page_offset: int
    page_size: int
    total_count: int | None
    created_at: str
    expires_at: str

    @property
    def results(self) -> list[PendingSearchResult]:
        start = self.page_offset * self.page_size
        return self.cached_results[start : start + self.page_size]

    @property
    def page_start(self) -> int:
        if not self.results:
            return 0
        return self.page_offset * self.page_size + 1

    @property
    def page_end(self) -> int:
        if not self.results:
            return 0
        return self.page_start + len(self.results) - 1

    @property
    def has_back(self) -> bool:
        return self.page_offset > 0

    @property
    def has_more(self) -> bool:
        next_page_start = (self.page_offset + 1) * self.page_size
        if next_page_start < len(self.cached_results):
            return True
        if self.total_count is not None:
            return len(self.cached_results) < self.total_count
        if not self.cached_results:
            return False
        return len(self.cached_results) % self.page_size == 0

    def to_dict(self) -> dict:
        return {
            "chat_key": self.chat_key,
            "requested_item": self.requested_item,
            "normalized_name": self.normalized_name,
            "search_query": self.search_query,
            "quantity": self.quantity,
            "results": [item.to_dict() for item in self.results],
            "cached_count": len(self.cached_results),
            "page_offset": self.page_offset,
            "page_size": self.page_size,
            "page_start": self.page_start,
            "page_end": self.page_end,
            "has_more": self.has_more,
            "has_back": self.has_back,
            "total_count": self.total_count,
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


def _ensure_schema_columns(conn: sqlite3.Connection) -> None:
    columns = {
        row[1] for row in conn.execute("PRAGMA table_info(pending_selections)").fetchall()
    }
    if "cached_results_json" not in columns:
        conn.execute("ALTER TABLE pending_selections ADD COLUMN cached_results_json TEXT")
    if "page_offset" not in columns:
        conn.execute(
            "ALTER TABLE pending_selections ADD COLUMN page_offset INTEGER NOT NULL DEFAULT 0"
        )
    if "page_size" not in columns:
        conn.execute(
            "ALTER TABLE pending_selections ADD COLUMN page_size INTEGER NOT NULL DEFAULT 10"
        )
    if "total_count" not in columns:
        conn.execute("ALTER TABLE pending_selections ADD COLUMN total_count INTEGER")


def init_pending_selection_db(db_path: Path | None = None) -> None:
    path = _resolve_db_path(db_path)
    with _connect(path) as conn:
        conn.executescript(_SCHEMA)
        _ensure_schema_columns(conn)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _utc_now_iso() -> str:
    return _utc_now().isoformat()


def _parse_iso(ts: str) -> datetime:
    return datetime.fromisoformat(ts)


def _row_to_pending(row: sqlite3.Row) -> PendingSelection:
    page_size = int(row["page_size"]) if row["page_size"] is not None else search_page_size()
    page_offset = int(row["page_offset"]) if row["page_offset"] is not None else 0
    total_count = row["total_count"]
    if total_count is not None:
        total_count = int(total_count)

    cached_raw = row["cached_results_json"]
    if cached_raw:
        cached_results = [
            PendingSearchResult.from_dict(item) for item in json.loads(cached_raw)
        ]
    else:
        cached_results = [
            PendingSearchResult.from_dict(item) for item in json.loads(row["results_json"])
        ]

    return PendingSelection(
        chat_key=row["chat_key"],
        requested_item=row["requested_item"],
        normalized_name=row["normalized_name"],
        search_query=row["search_query"],
        quantity=int(row["quantity"]),
        cached_results=cached_results,
        page_offset=page_offset,
        page_size=page_size,
        total_count=total_count,
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
    cached_results: list[PendingSearchResult],
    page_offset: int = 0,
    page_size: int | None = None,
    total_count: int | None = None,
    db_path: Path | None = None,
    ttl_minutes: int | None = None,
) -> PendingSelection:
    path = _resolve_db_path(db_path)
    init_pending_selection_db(path)
    created = _utc_now()
    ttl = ttl_minutes if ttl_minutes is not None else pending_ttl_minutes()
    expires = created + timedelta(minutes=ttl)
    resolved_page_size = page_size if page_size is not None else search_page_size()
    cached_payload = json.dumps([item.to_dict() for item in cached_results])
    page_results = cached_results[
        page_offset * resolved_page_size : page_offset * resolved_page_size + resolved_page_size
    ]
    results_payload = json.dumps([item.to_dict() for item in page_results])
    with _connect(path) as conn:
        conn.execute(
            """
            INSERT INTO pending_selections (
                chat_key, requested_item, normalized_name, search_query,
                quantity, results_json, created_at, expires_at,
                cached_results_json, page_offset, page_size, total_count
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(chat_key) DO UPDATE SET
                requested_item=excluded.requested_item,
                normalized_name=excluded.normalized_name,
                search_query=excluded.search_query,
                quantity=excluded.quantity,
                results_json=excluded.results_json,
                created_at=excluded.created_at,
                expires_at=excluded.expires_at,
                cached_results_json=excluded.cached_results_json,
                page_offset=excluded.page_offset,
                page_size=excluded.page_size,
                total_count=excluded.total_count
            """,
            (
                chat_key,
                requested_item,
                normalize_preference_key(normalized_name),
                search_query,
                max(1, int(quantity)),
                results_payload,
                created.isoformat(),
                expires.isoformat(),
                cached_payload,
                max(0, int(page_offset)),
                resolved_page_size,
                total_count,
            ),
        )
    return PendingSelection(
        chat_key=chat_key,
        requested_item=requested_item,
        normalized_name=normalize_preference_key(normalized_name),
        search_query=search_query,
        quantity=max(1, int(quantity)),
        cached_results=cached_results,
        page_offset=max(0, int(page_offset)),
        page_size=resolved_page_size,
        total_count=total_count,
        created_at=created.isoformat(),
        expires_at=expires.isoformat(),
    )


def update_pending_selection_page(
    pending: PendingSelection,
    *,
    page_offset: int,
    cached_results: list[PendingSearchResult] | None = None,
    total_count: int | None = None,
    db_path: Path | None = None,
    ttl_minutes: int | None = None,
) -> PendingSelection:
    return save_pending_selection(
        chat_key=pending.chat_key,
        requested_item=pending.requested_item,
        normalized_name=pending.normalized_name,
        search_query=pending.search_query,
        quantity=pending.quantity,
        cached_results=cached_results if cached_results is not None else pending.cached_results,
        page_offset=page_offset,
        page_size=pending.page_size,
        total_count=total_count if total_count is not None else pending.total_count,
        db_path=db_path,
        ttl_minutes=ttl_minutes,
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
