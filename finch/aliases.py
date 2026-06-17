"""SQLite-backed alias store with YAML seed import."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import yaml

from finch.config import DATA_DIR, DEFAULT_ALIASES_YAML
from finch.models import AliasEntry
from finch.preference_norm import normalize_preference_key

_SCHEMA = """
CREATE TABLE IF NOT EXISTS aliases (
    alias_key TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    kroger_product_id TEXT,
    upc TEXT,
    search_term TEXT,
    notes TEXT
);

CREATE TABLE IF NOT EXISTS preference_key_redirects (
    from_key TEXT PRIMARY KEY,
    to_key TEXT NOT NULL
);
"""


def _resolve_db_path(db_path: Path | None = None) -> Path:
    if db_path is not None:
        return db_path
    return Path(os.getenv("FINCH_ALIASES_DB_PATH", str(DATA_DIR / "finch_aliases.db")))


def _connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def init_db(db_path: Path | None = None) -> None:
    path = _resolve_db_path(db_path)
    with _connect(path) as conn:
        conn.executescript(_SCHEMA)
        for column in ("product_size", "product_price"):
            try:
                conn.execute(f"ALTER TABLE aliases ADD COLUMN {column} TEXT")
            except sqlite3.OperationalError:
                pass


def _row_to_entry(row: sqlite3.Row) -> AliasEntry:
    keys = set(row.keys())
    return AliasEntry(
        alias_key=row["alias_key"],
        display_name=row["display_name"],
        kroger_product_id=row["kroger_product_id"],
        upc=row["upc"],
        search_term=row["search_term"],
        notes=row["notes"],
        product_size=row["product_size"] if "product_size" in keys else None,
        product_price=row["product_price"] if "product_price" in keys else None,
    )


def _canonical_key(alias_key: str) -> str:
    return normalize_preference_key(alias_key)


def load_aliases_from_yaml(yaml_path: Path | None = None) -> list[AliasEntry]:
    path = yaml_path or DEFAULT_ALIASES_YAML
    if not path.exists():
        return []
    with path.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    entries: list[AliasEntry] = []
    for item in data.get("aliases", []):
        if not isinstance(item, dict) or not item.get("alias_key"):
            continue
        entries.append(
            AliasEntry(
                alias_key=_canonical_key(str(item["alias_key"])),
                display_name=str(item.get("display_name", item["alias_key"])),
                kroger_product_id=item.get("kroger_product_id"),
                upc=item.get("upc"),
                search_term=item.get("search_term"),
                notes=item.get("notes"),
            )
        )
    return entries


def seed_aliases_from_yaml(
    yaml_path: Path | None = None,
    db_path: Path | None = None,
    *,
    overwrite: bool = False,
) -> int:
    """Import YAML aliases into SQLite. Returns number of rows upserted."""
    entries = load_aliases_from_yaml(yaml_path)
    if not entries:
        return 0

    path = _resolve_db_path(db_path)
    init_db(path)
    inserted = 0
    with _connect(path) as conn:
        for entry in entries:
            if overwrite:
                conn.execute(
                    """
                    INSERT INTO aliases (
                        alias_key, display_name, kroger_product_id, upc, search_term, notes
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(alias_key) DO UPDATE SET
                        display_name=excluded.display_name,
                        kroger_product_id=excluded.kroger_product_id,
                        upc=excluded.upc,
                        search_term=excluded.search_term,
                        notes=excluded.notes
                    """,
                    (
                        entry.alias_key,
                        entry.display_name,
                        entry.kroger_product_id,
                        entry.upc,
                        entry.search_term,
                        entry.notes,
                    ),
                )
                inserted += 1
            else:
                cur = conn.execute(
                    """
                    INSERT OR IGNORE INTO aliases (
                        alias_key, display_name, kroger_product_id, upc, search_term, notes
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        entry.alias_key,
                        entry.display_name,
                        entry.kroger_product_id,
                        entry.upc,
                        entry.search_term,
                        entry.notes,
                    ),
                )
                inserted += cur.rowcount
    return inserted


def get_all_aliases(db_path: Path | None = None) -> list[AliasEntry]:
    path = _resolve_db_path(db_path)
    init_db(path)
    with _connect(path) as conn:
        rows = conn.execute("SELECT * FROM aliases ORDER BY alias_key").fetchall()
    return [_row_to_entry(row) for row in rows]


def _get_alias_row(alias_key: str, db_path: Path | None = None) -> AliasEntry | None:
    path = _resolve_db_path(db_path)
    init_db(path)
    with _connect(path) as conn:
        row = conn.execute(
            "SELECT * FROM aliases WHERE alias_key = ?", (alias_key,)
        ).fetchone()
    return _row_to_entry(row) if row else None


def get_preference_redirect(
    alias_key: str,
    db_path: Path | None = None,
) -> str | None:
    key = _canonical_key(alias_key)
    path = _resolve_db_path(db_path)
    init_db(path)
    with _connect(path) as conn:
        row = conn.execute(
            "SELECT to_key FROM preference_key_redirects WHERE from_key = ?",
            (key,),
        ).fetchone()
    return str(row["to_key"]) if row else None


def get_all_preference_redirects(db_path: Path | None = None) -> list[tuple[str, str]]:
    """Return saved preference key aliases as (from_key, to_key) pairs."""
    path = _resolve_db_path(db_path)
    init_db(path)
    with _connect(path) as conn:
        rows = conn.execute(
            "SELECT from_key, to_key FROM preference_key_redirects ORDER BY from_key"
        ).fetchall()
    return [(str(row["from_key"]), str(row["to_key"])) for row in rows]


def set_preference_redirect(
    from_key: str,
    to_key: str,
    *,
    db_path: Path | None = None,
) -> None:
    source = _canonical_key(from_key)
    target = _canonical_key(to_key)
    path = _resolve_db_path(db_path)
    init_db(path)
    with _connect(path) as conn:
        conn.execute(
            """
            INSERT INTO preference_key_redirects (from_key, to_key)
            VALUES (?, ?)
            ON CONFLICT(from_key) DO UPDATE SET to_key = excluded.to_key
            """,
            (source, target),
        )


def _resolve_redirected_key(alias_key: str, db_path: Path | None = None) -> str:
    key = _canonical_key(alias_key)
    seen: set[str] = set()
    while key not in seen:
        seen.add(key)
        redirect = get_preference_redirect(key, db_path)
        if not redirect:
            return key
        key = _canonical_key(redirect)
    return key


def lookup_alias(
    alias_key: str,
    db_path: Path | None = None,
) -> AliasEntry | None:
    """Resolve a preference by normalized key, redirects, and plural variants."""
    key = _resolve_redirected_key(alias_key, db_path)

    exact = _get_alias_row(key, db_path)
    if exact:
        return exact

    for entry in get_all_aliases(db_path):
        if _canonical_key(entry.alias_key) == key:
            return entry
    return None


def get_alias(alias_key: str, db_path: Path | None = None) -> AliasEntry | None:
    return lookup_alias(alias_key, db_path)


def find_alias_matches(
    normalized_name: str,
    db_path: Path | None = None,
) -> list[AliasEntry]:
    """Return alias entries that match a normalized grocery name."""
    name = _canonical_key(normalized_name)
    if not name:
        return []

    exact = lookup_alias(name, db_path)
    if exact:
        return [exact]

    aliases = get_all_aliases(db_path)
    partial = [
        entry
        for entry in aliases
        if name in entry.alias_key or entry.alias_key in name
    ]
    return partial


def ensure_seeded(db_path: Path | None = None, yaml_path: Path | None = None) -> None:
    """Initialize DB and seed from YAML when empty."""
    path = _resolve_db_path(db_path)
    init_db(path)
    if not get_all_aliases(path):
        seed_aliases_from_yaml(yaml_path, path)


def delete_alias_by_key(alias_key: str, db_path: Path | None = None) -> AliasEntry | None:
    key = alias_key.strip().lower()
    path = _resolve_db_path(db_path)
    init_db(path)
    existing = _get_alias_row(key, db_path)
    if not existing:
        return None
    with _connect(path) as conn:
        conn.execute("DELETE FROM aliases WHERE alias_key = ?", (key,))
    return existing


def delete_aliases_matching_normalized(
    normalized_key: str,
    *,
    db_path: Path | None = None,
) -> list[AliasEntry]:
    """Delete aliases whose normalized key matches, including plural variants."""
    target = _canonical_key(normalized_key)
    if not target:
        return []

    removed: list[AliasEntry] = []
    for entry in get_all_aliases(db_path):
        if _canonical_key(entry.alias_key) == target:
            deleted = delete_alias_by_key(entry.alias_key, db_path)
            if deleted:
                removed.append(deleted)
    return removed


def upsert_alias(entry: AliasEntry, db_path: Path | None = None) -> AliasEntry:
    """Insert or replace a single alias entry."""
    path = _resolve_db_path(db_path)
    init_db(path)
    canonical = _canonical_key(entry.alias_key)
    stored = AliasEntry(
        alias_key=canonical,
        display_name=entry.display_name,
        kroger_product_id=entry.kroger_product_id,
        upc=entry.upc,
        search_term=entry.search_term,
        notes=entry.notes,
        product_size=entry.product_size,
        product_price=entry.product_price,
    )
    with _connect(path) as conn:
        conn.execute(
            """
            INSERT INTO aliases (
                alias_key, display_name, kroger_product_id, upc, search_term, notes,
                product_size, product_price
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(alias_key) DO UPDATE SET
                display_name=excluded.display_name,
                kroger_product_id=excluded.kroger_product_id,
                upc=excluded.upc,
                search_term=excluded.search_term,
                notes=excluded.notes,
                product_size=excluded.product_size,
                product_price=excluded.product_price
            """,
            (
                stored.alias_key,
                stored.display_name,
                stored.kroger_product_id,
                stored.upc,
                stored.search_term,
                stored.notes,
                stored.product_size,
                stored.product_price,
            ),
        )
    return stored
