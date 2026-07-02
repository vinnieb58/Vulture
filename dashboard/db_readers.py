"""Defensive SQLite readers for the Vulture Dashboard."""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any

from log_readers import recent_errors_for_source

DB_PATH = Path(os.environ.get("VULTURE_DB_PATH", "/app/data/vulture.db"))


def _list_tables(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    ).fetchall()
    return [row[0] for row in rows]


def _table_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return [row[1] for row in rows]


def _pick_column(columns: list[str], candidates: tuple[str, ...]) -> str | None:
    lower_map = {col.lower(): col for col in columns}
    for candidate in candidates:
        if candidate.lower() in lower_map:
            return lower_map[candidate.lower()]
    return None


def _parse_source_list(raw: str) -> list[str]:
    raw = raw.strip()
    if not raw:
        return []
    if raw.startswith("["):
        try:
            data = json.loads(raw)
            if isinstance(data, list):
                return [str(item) for item in data]
        except json.JSONDecodeError:
            pass
    if "," in raw:
        return [part.strip() for part in raw.split(",") if part.strip()]
    return [raw]


def read_db_snapshot(log_lines: list[str] | None = None) -> dict[str, Any]:
    snapshot: dict[str, Any] = {
        "available": False,
        "warning": None,
        "tables": [],
        "hunt_counts": {"total": 0, "active": 0, "paused": 0, "ended": 0, "other": 0},
        "concert_counts": {
            "active": 0,
            "paused": 0,
            "total_events": 0,
            "total_alerts": 0,
            "recent_events": 0,
            "recent_alerts": 0,
        },
        "hunts": [],
        "recent_listings": [],
        "adapter_sources": [],
    }

    if not DB_PATH.exists():
        snapshot["warning"] = f"Database not found at {DB_PATH}"
        return snapshot

    try:
        conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
    except sqlite3.Error as exc:
        snapshot["warning"] = f"Could not open database: {exc}"
        return snapshot

    try:
        tables = _list_tables(conn)
        snapshot["tables"] = tables

        if "hunts" not in tables:
            snapshot["warning"] = (
                "Expected table 'hunts' not found. "
                f"Available tables: {', '.join(tables) or '(none)'}"
            )
        else:
            snapshot["hunt_counts"] = _count_hunts(conn)
            snapshot["hunts"] = _fetch_hunts(conn)

        if "listings" in tables:
            snapshot["recent_listings"] = _fetch_recent_listings(conn)
            snapshot["adapter_sources"] = _fetch_adapter_sources(
                conn, tables, log_lines or []
            )
        elif snapshot["warning"] is None:
            snapshot["warning"] = (
                "Expected table 'listings' not found. "
                f"Available tables: {', '.join(tables) or '(none)'}"
            )

        if any(t in tables for t in ("concert_watches", "concert_events", "concert_alerts")):
            snapshot["concert_counts"] = _count_concerts(conn, tables)

        snapshot["available"] = snapshot["warning"] is None or bool(
            snapshot["recent_listings"] or snapshot["hunts"]
        )
    except sqlite3.Error as exc:
        snapshot["warning"] = f"Database query failed: {exc}"
    finally:
        conn.close()

    return snapshot


def _count_hunts(conn: sqlite3.Connection) -> dict[str, int]:
    columns = _table_columns(conn, "hunts")
    status_col = _pick_column(columns, ("status",))

    counts = {"total": 0, "active": 0, "paused": 0, "ended": 0, "other": 0}

    if status_col:
        rows = conn.execute(
            f"SELECT {status_col}, COUNT(*) AS n FROM hunts GROUP BY {status_col}"
        ).fetchall()
        for row in rows:
            status = (row[0] or "").strip().lower()
            n = int(row[1])
            counts["total"] += n
            if status in counts:
                counts[status] += n
            else:
                counts["other"] += n
    else:
        row = conn.execute("SELECT COUNT(*) FROM hunts").fetchone()
        counts["total"] = int(row[0]) if row else 0
        counts["active"] = counts["total"]

    return counts


def _count_concerts(conn: sqlite3.Connection, tables: list[str]) -> dict[str, int]:
    """Count concert watches/events/alerts when tables exist."""
    from datetime import datetime, timedelta, timezone

    counts = {
        "active": 0,
        "paused": 0,
        "total_events": 0,
        "total_alerts": 0,
        "recent_events": 0,
        "recent_alerts": 0,
    }
    recent_days = int(os.environ.get("DASHBOARD_CONCERT_RECENT_DAYS", "7"))
    cutoff = (datetime.now(timezone.utc) - timedelta(days=recent_days)).isoformat()

    if "concert_watches" in tables:
        row = conn.execute(
            """
            SELECT
                SUM(CASE WHEN active = 1 THEN 1 ELSE 0 END) AS active_count,
                SUM(CASE WHEN active = 0 THEN 1 ELSE 0 END) AS paused_count
            FROM concert_watches
            """
        ).fetchone()
        if row:
            counts["active"] = int(row[0] or 0)
            counts["paused"] = int(row[1] or 0)

    if "concert_events" in tables:
        row = conn.execute("SELECT COUNT(*) FROM concert_events").fetchone()
        counts["total_events"] = int(row[0]) if row else 0
        row = conn.execute(
            """
            SELECT COUNT(*) FROM concert_events
            WHERE COALESCE(first_seen_at, '') >= ?
            """,
            (cutoff,),
        ).fetchone()
        counts["recent_events"] = int(row[0]) if row else 0

    if "concert_alerts" in tables:
        row = conn.execute("SELECT COUNT(*) FROM concert_alerts").fetchone()
        counts["total_alerts"] = int(row[0]) if row else 0
        row = conn.execute(
            """
            SELECT COUNT(*) FROM concert_alerts
            WHERE COALESCE(alerted_at, '') >= ?
            """,
            (cutoff,),
        ).fetchone()
        counts["recent_alerts"] = int(row[0]) if row else 0

    return counts


def _fetch_hunts(conn: sqlite3.Connection, limit: int = 50) -> list[dict[str, Any]]:
    columns = _table_columns(conn, "hunts")
    if not columns:
        return []

    aliases = {
        "name": _pick_column(columns, ("name", "title")),
        "status": _pick_column(columns, ("status",)),
        "source_sites": _pick_column(columns, ("source_sites", "sources", "source")),
        "created_at": _pick_column(columns, ("created_at", "created")),
        "updated_at": _pick_column(columns, ("updated_at", "updated")),
        "last_run": _pick_column(columns, ("last_run", "last_run_at", "last_executed")),
        "max_price": _pick_column(columns, ("max_price",)),
        "query": _pick_column(
            columns, ("search_terms", "query", "search_query", "keywords")
        ),
        "vertical": _pick_column(columns, ("category", "vertical", "type")),
    }

    select_parts: list[str] = []
    for alias, col in aliases.items():
        if col:
            select_parts.append(f"{col} AS {alias}")

    if not select_parts:
        return []

    order = (
        aliases["updated_at"]
        or aliases["last_run"]
        or aliases["created_at"]
        or _pick_column(columns, ("name",))
        or columns[0]
    )
    sql = f"SELECT {', '.join(select_parts)} FROM hunts ORDER BY {order} DESC LIMIT ?"
    rows = conn.execute(sql, (limit,)).fetchall()

    hunts: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        if item.get("source_sites"):
            item["source_sites_display"] = ", ".join(
                _parse_source_list(str(item["source_sites"]))
            )
        if item.get("query") and str(item["query"]).startswith("["):
            try:
                parsed = json.loads(str(item["query"]))
                if isinstance(parsed, list):
                    item["query_display"] = ", ".join(str(x) for x in parsed)
                else:
                    item["query_display"] = str(item["query"])
            except json.JSONDecodeError:
                item["query_display"] = str(item["query"])
        else:
            item["query_display"] = item.get("query")
        hunts.append(item)
    return hunts


def _fetch_recent_listings(conn: sqlite3.Connection, limit: int = 20) -> list[dict[str, Any]]:
    columns = _table_columns(conn, "listings")
    if not columns:
        return []

    title_col = _pick_column(columns, ("title", "name"))
    source_col = _pick_column(columns, ("source", "site", "adapter"))
    price_col = _pick_column(columns, ("price",))
    location_col = _pick_column(columns, ("location", "city"))
    link_col = _pick_column(columns, ("link", "url"))
    seen_col = _pick_column(columns, ("first_seen", "created_at", "seen_at", "timestamp"))

    select_parts: list[str] = []
    aliases: dict[str, str | None] = {
        "title": title_col,
        "source": source_col,
        "price": price_col,
        "location": location_col,
        "link": link_col,
        "first_seen": seen_col,
    }
    for alias, col in aliases.items():
        if col:
            select_parts.append(f"{col} AS {alias}")

    if not select_parts:
        return []

    order = seen_col or _pick_column(columns, ("id",)) or columns[0]
    sql = f"SELECT {', '.join(select_parts)} FROM listings ORDER BY {order} DESC LIMIT ?"
    rows = conn.execute(sql, (limit,)).fetchall()
    return [dict(row) for row in rows]


def _fetch_adapter_sources(
    conn: sqlite3.Connection,
    tables: list[str],
    log_lines: list[str],
) -> list[dict[str, Any]]:
    sources: dict[str, dict[str, Any]] = {}

    def _ensure(source: str) -> dict[str, Any]:
        key = (source or "unknown").strip().lower() or "unknown"
        if key not in sources:
            sources[key] = {
                "name": key,
                "listings": 0,
                "hunts": 0,
                "latest_listing": None,
                "recent_errors": [],
            }
        return sources[key]

    if "listings" in tables:
        columns = _table_columns(conn, "listings")
        source_col = _pick_column(columns, ("source", "site", "adapter"))
        seen_col = _pick_column(columns, ("first_seen", "created_at", "seen_at", "timestamp"))
        if source_col:
            for row in conn.execute(
                f"SELECT {source_col}, COUNT(*) FROM listings GROUP BY {source_col}"
            ):
                entry = _ensure(row[0] or "unknown")
                entry["listings"] = int(row[1])

            if seen_col:
                for row in conn.execute(
                    f"""
                    SELECT {source_col}, MAX({seen_col})
                    FROM listings
                    GROUP BY {source_col}
                    """
                ):
                    entry = _ensure(row[0] or "unknown")
                    entry["latest_listing"] = row[1]

    if "hunts" in tables:
        columns = _table_columns(conn, "hunts")
        source_col = _pick_column(columns, ("source_sites", "sources", "source"))
        if source_col:
            for row in conn.execute(f"SELECT {source_col} FROM hunts"):
                raw = row[0]
                if not raw:
                    continue
                for source in _parse_source_list(str(raw)):
                    _ensure(source)["hunts"] += 1

    for name, entry in sources.items():
        entry["recent_errors"] = recent_errors_for_source(log_lines, name)

    return [sources[k] for k in sorted(sources)]
