"""SQLite persistence for Vulture Concerts."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from engine.concerts.dedupe import MergedConcertEvent
from engine.concerts.models import ConcertAlert, ConcertEvent, ConcertWatch
from engine.concerts.search import SearchCriteria
from engine.database import get_connection

log = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def init_concert_tables() -> None:
    """Create concert tables if they do not exist."""
    with get_connection() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS concert_watches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                artist_query TEXT,
                genre TEXT,
                area TEXT,
                city TEXT,
                state TEXT,
                radius_miles INTEGER,
                days_forward INTEGER NOT NULL DEFAULT 180,
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS concert_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source TEXT NOT NULL,
                provider_event_id TEXT NOT NULL,
                artist_or_title TEXT NOT NULL,
                venue TEXT,
                city TEXT,
                state TEXT,
                starts_at TEXT,
                ticket_url TEXT,
                genre_or_classification TEXT,
                event_dedupe_key TEXT NOT NULL,
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                UNIQUE(source, provider_event_id)
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_concert_events_dedupe
            ON concert_events(event_dedupe_key)
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS concert_alerts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                watch_id INTEGER NOT NULL,
                event_dedupe_key TEXT NOT NULL,
                alerted_at TEXT NOT NULL,
                UNIQUE(watch_id, event_dedupe_key),
                FOREIGN KEY(watch_id) REFERENCES concert_watches(id)
            )
            """
        )
        conn.commit()


def _row_to_watch(row) -> ConcertWatch:
    d = dict(row)
    return ConcertWatch(
        id=d["id"],
        artist_query=d["artist_query"],
        genre=d["genre"],
        area=d["area"],
        city=d["city"],
        state=d["state"],
        radius_miles=d["radius_miles"],
        days_forward=d["days_forward"],
        active=bool(d["active"]),
        created_at=d["created_at"],
    )


def create_watch(criteria: SearchCriteria) -> ConcertWatch:
    now = _now_iso()
    with get_connection() as conn:
        cur = conn.execute(
            """
            INSERT INTO concert_watches (
                artist_query, genre, area, city, state, radius_miles,
                days_forward, active, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?)
            """,
            (
                criteria.artist_query,
                criteria.genre,
                criteria.area,
                criteria.city,
                criteria.state,
                criteria.radius_miles,
                criteria.days_forward,
                now,
            ),
        )
        conn.commit()
        watch_id = cur.lastrowid
        row = conn.execute(
            "SELECT * FROM concert_watches WHERE id = ?",
            (watch_id,),
        ).fetchone()
    return _row_to_watch(row)


def list_watches(*, active_only: bool = True) -> list[ConcertWatch]:
    with get_connection() as conn:
        if active_only:
            rows = conn.execute(
                "SELECT * FROM concert_watches WHERE active = 1 ORDER BY id"
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM concert_watches ORDER BY id"
            ).fetchall()
    return [_row_to_watch(r) for r in rows]


def count_watches() -> tuple[int, int]:
    """Return (active_count, paused_count) for concert watches."""
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT
                SUM(CASE WHEN active = 1 THEN 1 ELSE 0 END) AS active_count,
                SUM(CASE WHEN active = 0 THEN 1 ELSE 0 END) AS paused_count
            FROM concert_watches
            """
        ).fetchone()
    if not row:
        return 0, 0
    return int(row[0] or 0), int(row[1] or 0)


def count_concert_events(*, days: int | None = None) -> int:
    """Count persisted concert events, optionally limited to recent days."""
    with get_connection() as conn:
        if days is None:
            row = conn.execute("SELECT COUNT(*) FROM concert_events").fetchone()
            return int(row[0]) if row else 0
        cutoff = datetime.now(timezone.utc).timestamp() - (days * 86400)
        cutoff_iso = datetime.fromtimestamp(cutoff, tz=timezone.utc).isoformat()
        row = conn.execute(
            """
            SELECT COUNT(*) FROM concert_events
            WHERE COALESCE(first_seen_at, '') >= ?
            """,
            (cutoff_iso,),
        ).fetchone()
    return int(row[0]) if row else 0


def count_concert_alerts(*, days: int | None = None) -> int:
    """Count concert alerts sent, optionally limited to recent days."""
    with get_connection() as conn:
        if days is None:
            row = conn.execute("SELECT COUNT(*) FROM concert_alerts").fetchone()
            return int(row[0]) if row else 0
        cutoff = datetime.now(timezone.utc).timestamp() - (days * 86400)
        cutoff_iso = datetime.fromtimestamp(cutoff, tz=timezone.utc).isoformat()
        row = conn.execute(
            """
            SELECT COUNT(*) FROM concert_alerts
            WHERE COALESCE(alerted_at, '') >= ?
            """,
            (cutoff_iso,),
        ).fetchone()
    return int(row[0]) if row else 0


def get_watch(watch_id: int) -> Optional[ConcertWatch]:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM concert_watches WHERE id = ?",
            (watch_id,),
        ).fetchone()
    return _row_to_watch(row) if row else None


def watch_to_criteria(watch: ConcertWatch) -> SearchCriteria:
    return SearchCriteria(
        artist_query=watch.artist_query,
        genre=watch.genre,
        area=watch.area,
        city=watch.city,
        state=watch.state,
        radius_miles=watch.radius_miles,
        days_forward=watch.days_forward,
    )


def upsert_provider_events(events: list[MergedConcertEvent]) -> None:
    """Persist provider-level rows from merged events."""
    now = _now_iso()
    with get_connection() as conn:
        for merged in events:
            for pe in merged.provider_events:
                conn.execute(
                    """
                    INSERT INTO concert_events (
                        source, provider_event_id, artist_or_title, venue, city, state,
                        starts_at, ticket_url, genre_or_classification,
                        event_dedupe_key, first_seen_at, last_seen_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(source, provider_event_id) DO UPDATE SET
                        artist_or_title = excluded.artist_or_title,
                        venue = excluded.venue,
                        city = excluded.city,
                        state = excluded.state,
                        starts_at = excluded.starts_at,
                        ticket_url = excluded.ticket_url,
                        genre_or_classification = excluded.genre_or_classification,
                        event_dedupe_key = excluded.event_dedupe_key,
                        last_seen_at = excluded.last_seen_at
                    """,
                    (
                        pe.source,
                        pe.provider_event_id,
                        pe.artist_or_title,
                        pe.venue,
                        pe.city,
                        pe.state,
                        pe.starts_at,
                        pe.ticket_url,
                        pe.genre_or_classification,
                        pe.event_dedupe_key,
                        now,
                        now,
                    ),
                )
        conn.commit()


def alert_exists(watch_id: int, event_dedupe_key: str) -> bool:
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT 1 FROM concert_alerts
            WHERE watch_id = ? AND event_dedupe_key = ?
            """,
            (watch_id, event_dedupe_key),
        ).fetchone()
    return row is not None


def record_alert(watch_id: int, event_dedupe_key: str) -> ConcertAlert:
    now = _now_iso()
    with get_connection() as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO concert_alerts (watch_id, event_dedupe_key, alerted_at)
            VALUES (?, ?, ?)
            """,
            (watch_id, event_dedupe_key, now),
        )
        conn.commit()
        row = conn.execute(
            """
            SELECT * FROM concert_alerts
            WHERE watch_id = ? AND event_dedupe_key = ?
            """,
            (watch_id, event_dedupe_key),
        ).fetchone()
    d = dict(row)
    return ConcertAlert(
        id=d["id"],
        watch_id=d["watch_id"],
        event_dedupe_key=d["event_dedupe_key"],
        alerted_at=d["alerted_at"],
    )


def seed_bootstrap_alerts(watch_id: int, events: list[MergedConcertEvent]) -> int:
    """
    Record alert-ledger entries for initial watch results without sending notifications.

    Prevents the first scheduled watch cycle from alerting on events that were
    already known when the watch was created.
    """
    seeded = 0
    for event in events:
        key = event.event_dedupe_key
        if not key:
            continue
        record_alert(watch_id, key)
        seeded += 1
    return seeded


def pause_watch(watch_id: int) -> Optional[ConcertWatch]:
    """Pause a watch (active=0). Returns updated watch or None if not found."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM concert_watches WHERE id = ?",
            (watch_id,),
        ).fetchone()
        if not row:
            return None
        conn.execute(
            "UPDATE concert_watches SET active = 0 WHERE id = ?",
            (watch_id,),
        )
        conn.commit()
        updated = conn.execute(
            "SELECT * FROM concert_watches WHERE id = ?",
            (watch_id,),
        ).fetchone()
    log.info("Paused concert watch #%s", watch_id)
    return _row_to_watch(updated)


def unwatch(watch_id: int) -> bool:
    """Permanently remove a watch and its alert ledger rows."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT 1 FROM concert_watches WHERE id = ?",
            (watch_id,),
        ).fetchone()
        if not row:
            return False
        conn.execute("DELETE FROM concert_alerts WHERE watch_id = ?", (watch_id,))
        conn.execute("DELETE FROM concert_watches WHERE id = ?", (watch_id,))
        conn.commit()
    log.info("Removed concert watch #%s", watch_id)
    return True
