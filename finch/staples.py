"""Saved staple grocery lists and pending staple-batch preview state."""

from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from finch.aliases import lookup_alias
from finch.config import DATA_DIR
from finch.models import GroceryIntent
from finch.preference_norm import normalize_preference_key

STAPLES_DB_PATH = Path(
    os.getenv("FINCH_STAPLES_DB_PATH", str(DATA_DIR / "finch_staples.db"))
)

_DEFAULT_TTL_MINUTES = 60

_INITIAL_STAPLES: list[tuple[str, str, float]] = [
    ("milk", "Milk", 1),
    ("eggs", "Eggs", 1),
    ("blueberries", "Blueberries", 1),
    ("raspberries", "Raspberries", 1),
    ("strawberries", "Strawberries", 1),
    ("bananas", "Bananas", 1),
    ("ground beef", "Ground beef", 1),
    ("plantains", "Plantains", 5),
    ("bread", "Bread", 1),
    ("shredded cheese", "Shredded cheese", 1),
    ("cotija", "Cotija", 1),
    ("deli turkey", "Deli turkey", 1),
]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS staple_items (
    normalized_key TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    default_quantity REAL NOT NULL DEFAULT 1,
    enabled INTEGER NOT NULL DEFAULT 1,
    sort_order INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS pending_staple_batches (
    chat_key TEXT PRIMARY KEY,
    items_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);
"""

@dataclass(frozen=True)
class StapleItem:
    normalized_key: str
    display_name: str
    default_quantity: float
    enabled: bool
    sort_order: int

    def to_dict(self) -> dict:
        return {
            "normalized_key": self.normalized_key,
            "display_name": self.display_name,
            "default_quantity": self.default_quantity,
            "enabled": self.enabled,
            "sort_order": self.sort_order,
        }


@dataclass(frozen=True)
class StapleBatchItem:
    normalized_key: str
    display_name: str
    quantity: float

    def to_dict(self) -> dict:
        return {
            "normalized_key": self.normalized_key,
            "display_name": self.display_name,
            "quantity": self.quantity,
        }

    @classmethod
    def from_dict(cls, data: dict) -> StapleBatchItem:
        return cls(
            normalized_key=str(data["normalized_key"]),
            display_name=str(data["display_name"]),
            quantity=float(data.get("quantity") or 1),
        )


@dataclass(frozen=True)
class PendingStapleBatch:
    chat_key: str
    items: list[StapleBatchItem]
    created_at: str
    expires_at: str

    def to_dict(self) -> dict:
        return {
            "chat_key": self.chat_key,
            "items": [item.to_dict() for item in self.items],
            "created_at": self.created_at,
            "expires_at": self.expires_at,
        }


def _resolve_db_path(db_path: Path | None = None) -> Path:
    if db_path is not None:
        return db_path
    return Path(os.getenv("FINCH_STAPLES_DB_PATH", str(DATA_DIR / "finch_staples.db")))


def staples_ttl_minutes() -> int:
    raw = os.getenv("FINCH_STAPLES_BATCH_TTL_MINUTES", str(_DEFAULT_TTL_MINUTES))
    try:
        return max(1, int(raw))
    except ValueError:
        return _DEFAULT_TTL_MINUTES


def _connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _utc_now_iso() -> str:
    return _utc_now().isoformat()


def _parse_iso(ts: str) -> datetime:
    return datetime.fromisoformat(ts)


def init_staples_db(db_path: Path | None = None) -> None:
    path = _resolve_db_path(db_path)
    with _connect(path) as conn:
        conn.executescript(_SCHEMA)


def _row_to_staple(row: sqlite3.Row) -> StapleItem:
    return StapleItem(
        normalized_key=row["normalized_key"],
        display_name=row["display_name"],
        default_quantity=float(row["default_quantity"]),
        enabled=bool(row["enabled"]),
        sort_order=int(row["sort_order"]),
    )


def seed_initial_staples(db_path: Path | None = None) -> int:
    """Insert missing initial staple rows. Returns count of rows inserted."""
    path = _resolve_db_path(db_path)
    init_staples_db(path)
    inserted = 0
    with _connect(path) as conn:
        for sort_order, (raw_name, display_name, quantity) in enumerate(_INITIAL_STAPLES, start=1):
            key = normalize_preference_key(raw_name)
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO staple_items (
                    normalized_key, display_name, default_quantity, enabled, sort_order
                ) VALUES (?, ?, ?, 1, ?)
                """,
                (key, display_name, quantity, sort_order),
            )
            inserted += cur.rowcount
    return inserted


def list_staple_items(
    *,
    enabled_only: bool = False,
    db_path: Path | None = None,
) -> list[StapleItem]:
    path = _resolve_db_path(db_path)
    init_staples_db(path)
    seed_initial_staples(path)
    query = "SELECT * FROM staple_items"
    if enabled_only:
        query += " WHERE enabled = 1"
    query += " ORDER BY sort_order, normalized_key"
    with _connect(path) as conn:
        rows = conn.execute(query).fetchall()
    return [_row_to_staple(row) for row in rows]


def resolve_staple_preference(
    staple: StapleItem | StapleBatchItem,
    *,
    alias_db_path: Path | None = None,
) -> dict:
    """Return preference resolution status for a staple entry."""
    entry = lookup_alias(staple.normalized_key, db_path=alias_db_path)
    resolved = entry is not None and bool(entry.upc or entry.kroger_product_id)
    payload = {
        "normalized_key": staple.normalized_key,
        "display_name": staple.display_name,
        "preference_resolved": resolved,
        "preferred_product": None,
    }
    if entry is not None:
        payload["preferred_product"] = entry.display_name
        if not resolved:
            payload["preference_resolved"] = False
    return payload


def format_staples_list_text(
    *,
    db_path: Path | None = None,
    alias_db_path: Path | None = None,
) -> str:
    items = list_staple_items(enabled_only=True, db_path=db_path)
    if not items:
        return "Saved staples: (none enabled)"
    lines = ["Saved staples:"]
    for index, item in enumerate(items, start=1):
        qty = _format_quantity(item.default_quantity)
        lines.append(f"{index}. {item.display_name} — {qty}")
    return "\n".join(lines)


def format_staple_preview_text(batch: PendingStapleBatch) -> str:
    if not batch.items:
        return "Staples ready to add:\n\n(no items remaining)\n\nReply:\n• cancel"
    lines = ["Staples ready to add:", ""]
    for index, item in enumerate(batch.items, start=1):
        qty = _format_quantity(item.quantity)
        lines.append(f"{index}. {item.display_name} — {qty}")
    lines.extend(
        [
            "",
            "Reply:",
            "• confirm",
            "• remove 2, 5",
            "• remove eggs",
            "• cancel",
        ]
    )
    return "\n".join(lines)


def _format_quantity(quantity: float) -> str:
    if quantity == int(quantity):
        return str(int(quantity))
    return f"{quantity:g}"


def _staple_to_batch_item(staple: StapleItem) -> StapleBatchItem:
    return StapleBatchItem(
        normalized_key=staple.normalized_key,
        display_name=staple.display_name,
        quantity=staple.default_quantity,
    )


def start_staple_batch(
    chat_key: str,
    *,
    db_path: Path | None = None,
) -> PendingStapleBatch:
    path = _resolve_db_path(db_path)
    staples = list_staple_items(enabled_only=True, db_path=path)
    items = [_staple_to_batch_item(staple) for staple in staples]
    return save_pending_staple_batch(chat_key, items, db_path=path)


def save_pending_staple_batch(
    chat_key: str,
    items: list[StapleBatchItem],
    *,
    db_path: Path | None = None,
    ttl_minutes: int | None = None,
) -> PendingStapleBatch:
    path = _resolve_db_path(db_path)
    init_staples_db(path)
    created = _utc_now()
    ttl = ttl_minutes if ttl_minutes is not None else staples_ttl_minutes()
    expires = created + timedelta(minutes=ttl)
    payload = json.dumps([item.to_dict() for item in items])
    with _connect(path) as conn:
        conn.execute(
            """
            INSERT INTO pending_staple_batches (
                chat_key, items_json, created_at, expires_at
            ) VALUES (?, ?, ?, ?)
            ON CONFLICT(chat_key) DO UPDATE SET
                items_json=excluded.items_json,
                created_at=excluded.created_at,
                expires_at=excluded.expires_at
            """,
            (chat_key, payload, created.isoformat(), expires.isoformat()),
        )
    return PendingStapleBatch(
        chat_key=chat_key,
        items=items,
        created_at=created.isoformat(),
        expires_at=expires.isoformat(),
    )


def get_pending_staple_batch(
    chat_key: str,
    *,
    db_path: Path | None = None,
) -> PendingStapleBatch | None:
    path = _resolve_db_path(db_path)
    init_staples_db(path)
    with _connect(path) as conn:
        row = conn.execute(
            "SELECT * FROM pending_staple_batches WHERE chat_key = ?",
            (chat_key,),
        ).fetchone()
    if not row:
        return None
    batch = PendingStapleBatch(
        chat_key=row["chat_key"],
        items=[StapleBatchItem.from_dict(item) for item in json.loads(row["items_json"])],
        created_at=row["created_at"],
        expires_at=row["expires_at"],
    )
    if _parse_iso(batch.expires_at) <= _utc_now():
        clear_pending_staple_batch(chat_key, db_path=path)
        return None
    return batch


def clear_pending_staple_batch(chat_key: str, *, db_path: Path | None = None) -> bool:
    path = _resolve_db_path(db_path)
    init_staples_db(path)
    with _connect(path) as conn:
        cur = conn.execute(
            "DELETE FROM pending_staple_batches WHERE chat_key = ?",
            (chat_key,),
        )
    return cur.rowcount > 0


def parse_remove_targets(targets_text: str) -> tuple[list[int], list[str]]:
    """Parse remove targets into 1-based indices and normalized name keys."""
    numbers: list[int] = []
    names: list[str] = []
    for part in targets_text.split(","):
        token = part.strip()
        if not token:
            continue
        if token.isdigit():
            numbers.append(int(token))
        else:
            names.append(normalize_preference_key(token))
    return numbers, names


def remove_from_staple_batch(
    chat_key: str,
    targets_text: str,
    *,
    db_path: Path | None = None,
) -> PendingStapleBatch | None:
    batch = get_pending_staple_batch(chat_key, db_path=db_path)
    if batch is None:
        return None

    numbers, names = parse_remove_targets(targets_text)
    if not numbers and not names:
        return batch

    remove_indices = {number for number in numbers if number >= 1}
    remove_keys = set(names)
    remaining: list[StapleBatchItem] = []
    for index, item in enumerate(batch.items, start=1):
        if index in remove_indices:
            continue
        if item.normalized_key in remove_keys:
            continue
        remaining.append(item)

    if not remaining:
        clear_pending_staple_batch(chat_key, db_path=db_path)
        return PendingStapleBatch(
            chat_key=chat_key,
            items=[],
            created_at=batch.created_at,
            expires_at=batch.expires_at,
        )
    return save_pending_staple_batch(
        chat_key,
        remaining,
        db_path=db_path,
    )


def batch_to_grocery_intents(batch: PendingStapleBatch) -> list[GroceryIntent]:
    intents: list[GroceryIntent] = []
    for item in batch.items:
        qty = item.quantity
        if qty != 1:
            raw_text = f"{_format_quantity(qty)} {item.display_name}"
        else:
            raw_text = item.display_name
        intents.append(
            GroceryIntent(
                raw_text=raw_text,
                normalized_name=item.normalized_key,
                quantity=qty,
            )
        )
    return intents


def build_staples_status_report(
    *,
    staples_db_path: Path | None = None,
    alias_db_path: Path | None = None,
) -> list[dict]:
    items = list_staple_items(enabled_only=False, db_path=staples_db_path)
    report: list[dict] = []
    for item in items:
        resolution = resolve_staple_preference(item, alias_db_path=alias_db_path)
        report.append(
            {
                "display_name": item.display_name,
                "normalized_key": item.normalized_key,
                "default_quantity": item.default_quantity,
                "enabled": item.enabled,
                "preference_resolved": resolution["preference_resolved"],
                "preferred_product": resolution["preferred_product"],
            }
        )
    return report
