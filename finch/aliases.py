"""SQLite-backed alias store with YAML seed import."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import yaml

from finch.config import ALIASES_DB_PATH, DEFAULT_ALIASES_YAML
from finch.models import AliasEntry

_SCHEMA = """
CREATE TABLE IF NOT EXISTS aliases (
    alias_key TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    kroger_product_id TEXT,
    upc TEXT,
    search_term TEXT,
    notes TEXT
);
"""


def _connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def init_db(db_path: Path | None = None) -> None:
    path = db_path or ALIASES_DB_PATH
    with _connect(path) as conn:
        conn.executescript(_SCHEMA)


def _row_to_entry(row: sqlite3.Row) -> AliasEntry:
    return AliasEntry(
        alias_key=row["alias_key"],
        display_name=row["display_name"],
        kroger_product_id=row["kroger_product_id"],
        upc=row["upc"],
        search_term=row["search_term"],
        notes=row["notes"],
    )


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
                alias_key=str(item["alias_key"]).strip().lower(),
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

    path = db_path or ALIASES_DB_PATH
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
    path = db_path or ALIASES_DB_PATH
    init_db(path)
    with _connect(path) as conn:
        rows = conn.execute("SELECT * FROM aliases ORDER BY alias_key").fetchall()
    return [_row_to_entry(row) for row in rows]


def get_alias(alias_key: str, db_path: Path | None = None) -> AliasEntry | None:
    key = alias_key.strip().lower()
    path = db_path or ALIASES_DB_PATH
    init_db(path)
    with _connect(path) as conn:
        row = conn.execute(
            "SELECT * FROM aliases WHERE alias_key = ?", (key,)
        ).fetchone()
    return _row_to_entry(row) if row else None


def find_alias_matches(
    normalized_name: str,
    db_path: Path | None = None,
) -> list[AliasEntry]:
    """Return alias entries that match a normalized grocery name."""
    name = normalized_name.strip().lower()
    if not name:
        return []

    exact = get_alias(name, db_path)
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
    path = db_path or ALIASES_DB_PATH
    init_db(path)
    if not get_all_aliases(path):
        seed_aliases_from_yaml(yaml_path, path)
