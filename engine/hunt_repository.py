import json
import logging
from datetime import datetime, timezone
from typing import Optional

from engine.database import get_connection
from models.hunt import Hunt

log = logging.getLogger(__name__)

# Fields that are stored as JSON text in SQLite and must be round-tripped
_JSON_FIELDS = (
    "source_sites",
    "search_terms",
    "include_keywords",
    "exclude_keywords",
    "adapter_options",
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

def init_hunts_table() -> None:
    """Create the hunts table if it does not exist. Safe to call on every run."""
    with get_connection() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS hunts (
                hunt_id          TEXT PRIMARY KEY,
                name             TEXT NOT NULL,
                category         TEXT,
                source_sites     TEXT NOT NULL DEFAULT '[]',
                search_terms     TEXT NOT NULL DEFAULT '[]',
                include_keywords TEXT NOT NULL DEFAULT '[]',
                exclude_keywords TEXT NOT NULL DEFAULT '[]',
                max_price        INTEGER,
                location         TEXT,
                radius           INTEGER,
                status           TEXT NOT NULL DEFAULT 'active',
                created_by       TEXT,
                created_at       TEXT NOT NULL,
                updated_at       TEXT NOT NULL,
                notes            TEXT,
                adapter_options  TEXT NOT NULL DEFAULT '{}'
            )
            """
        )
        conn.commit()


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _hunt_to_row(hunt: Hunt) -> tuple:
    """Serialize a Hunt into a flat tuple matching the INSERT column order."""
    return (
        hunt.hunt_id,
        hunt.name,
        hunt.category,
        json.dumps(hunt.source_sites),
        json.dumps(hunt.search_terms),
        json.dumps(hunt.include_keywords),
        json.dumps(hunt.exclude_keywords),
        hunt.max_price,
        hunt.location,
        hunt.radius,
        hunt.status,
        hunt.created_by,
        hunt.created_at,
        hunt.updated_at,
        hunt.notes,
        json.dumps(hunt.adapter_options),
    )


def _row_to_hunt(row) -> Hunt:
    """Deserialize a sqlite3.Row into a Hunt dataclass."""
    d = dict(row)
    for f in _JSON_FIELDS:
        d[f] = json.loads(d[f])
    return Hunt(**d)


# ---------------------------------------------------------------------------
# Repository methods
# ---------------------------------------------------------------------------

def create_hunt(hunt: Hunt) -> Hunt:
    """
    Insert a new hunt record.

    Returns the hunt as stored. Raises sqlite3.IntegrityError if a hunt with
    the same hunt_id already exists.
    """
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO hunts (
                hunt_id, name, category,
                source_sites, search_terms,
                include_keywords, exclude_keywords,
                max_price, location, radius,
                status, created_by, created_at, updated_at,
                notes, adapter_options
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            _hunt_to_row(hunt),
        )
        conn.commit()
    log.debug("Created hunt '%s' (%s)", hunt.name, hunt.hunt_id)
    return hunt


def get_hunt_by_id(hunt_id: str) -> Optional[Hunt]:
    """Return a Hunt by its ID, or None if not found."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM hunts WHERE hunt_id = ?",
            (hunt_id,),
        ).fetchone()
    if row is None:
        return None
    return _row_to_hunt(row)


def list_hunts(status: Optional[str] = None) -> list[Hunt]:
    """
    Return all hunts ordered by creation time, optionally filtered by status.

    Common status values: "active", "paused", "archived"
    Pass status=None to return every hunt regardless of status.
    """
    with get_connection() as conn:
        if status is not None:
            rows = conn.execute(
                "SELECT * FROM hunts WHERE status = ? ORDER BY created_at",
                (status,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM hunts ORDER BY created_at",
            ).fetchall()
    return [_row_to_hunt(row) for row in rows]


def update_hunt_status(hunt_id: str, status: str) -> bool:
    """
    Update only the status field of a hunt and refresh updated_at.

    Returns True if the hunt was found and updated, False if the hunt_id
    does not exist.
    """
    with get_connection() as conn:
        cursor = conn.execute(
            "UPDATE hunts SET status = ?, updated_at = ? WHERE hunt_id = ?",
            (status, _now_iso(), hunt_id),
        )
        conn.commit()
    updated = cursor.rowcount > 0
    if updated:
        log.info("Hunt %s status -> %s", hunt_id, status)
    return updated


def update_hunt(hunt: Hunt) -> bool:
    """
    Replace all mutable fields on an existing hunt and refresh updated_at.

    hunt_id and created_at are never modified by this method.
    Returns True if the hunt was found and updated, False if the hunt_id
    does not exist.
    """
    with get_connection() as conn:
        cursor = conn.execute(
            """
            UPDATE hunts SET
                name             = ?,
                category         = ?,
                source_sites     = ?,
                search_terms     = ?,
                include_keywords = ?,
                exclude_keywords = ?,
                max_price        = ?,
                location         = ?,
                radius           = ?,
                status           = ?,
                created_by       = ?,
                updated_at       = ?,
                notes            = ?,
                adapter_options  = ?
            WHERE hunt_id = ?
            """,
            (
                hunt.name,
                hunt.category,
                json.dumps(hunt.source_sites),
                json.dumps(hunt.search_terms),
                json.dumps(hunt.include_keywords),
                json.dumps(hunt.exclude_keywords),
                hunt.max_price,
                hunt.location,
                hunt.radius,
                hunt.status,
                hunt.created_by,
                _now_iso(),
                hunt.notes,
                json.dumps(hunt.adapter_options),
                hunt.hunt_id,
            ),
        )
        conn.commit()
    updated = cursor.rowcount > 0
    if updated:
        log.debug("Updated hunt '%s' (%s)", hunt.name, hunt.hunt_id)
    return updated
