"""
Vulture Dashboard v0.1 — read-only internal status page.

This is an observability surface only: no hunt mutations, no scheduler
controls, and no authentication yet. Intended for local / Tailscale access
on Raven while Vulture itself continues to run outside Docker.
"""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

# ---------------------------------------------------------------------------
# Configuration (env-driven so Docker bind mounts stay flexible)
# ---------------------------------------------------------------------------

DB_PATH = Path(os.environ.get("VULTURE_DB_PATH", "/app/data/vulture.db"))
LOG_PATH = Path(os.environ.get("VULTURE_LOG_PATH", "/app/logs/vulture.log"))
LOG_TAIL_LINES = int(os.environ.get("VULTURE_LOG_TAIL_LINES", "50"))

app = FastAPI(title="Vulture Dashboard", version="0.1")
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


# ---------------------------------------------------------------------------
# SQLite schema inspection helpers
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Database readers (defensive — schema may differ across versions)
# ---------------------------------------------------------------------------

def _read_db_snapshot() -> dict[str, Any]:
    snapshot: dict[str, Any] = {
        "available": False,
        "warning": None,
        "tables": [],
        "hunt_counts": {"total": 0, "active": 0, "paused": 0, "ended": 0, "other": 0},
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

        if "listings" in tables:
            snapshot["recent_listings"] = _fetch_recent_listings(conn)
            snapshot["adapter_sources"] = _fetch_adapter_sources(conn, tables)
        elif snapshot["warning"] is None:
            snapshot["warning"] = (
                "Expected table 'listings' not found. "
                f"Available tables: {', '.join(tables) or '(none)'}"
            )

        snapshot["available"] = snapshot["warning"] is None or bool(snapshot["recent_listings"])
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


def _fetch_adapter_sources(conn: sqlite3.Connection, tables: list[str]) -> list[dict[str, Any]]:
    """Build a simple adapter summary from hunt/listing source columns."""
    sources: dict[str, dict[str, int]] = {}

    def _bump(source: str, field: str) -> None:
        key = (source or "unknown").strip().lower() or "unknown"
        if key not in sources:
            sources[key] = {"listings": 0, "hunts": 0}
        sources[key][field] += 1

    if "listings" in tables:
        columns = _table_columns(conn, "listings")
        source_col = _pick_column(columns, ("source", "site", "adapter"))
        if source_col:
            for row in conn.execute(
                f"SELECT {source_col}, COUNT(*) FROM listings GROUP BY {source_col}"
            ):
                key = (row[0] or "unknown").strip().lower() or "unknown"
                if key not in sources:
                    sources[key] = {"listings": 0, "hunts": 0}
                sources[key]["listings"] = int(row[1])

    if "hunts" in tables:
        columns = _table_columns(conn, "hunts")
        source_col = _pick_column(columns, ("source_sites", "sources", "source"))
        if source_col:
            for row in conn.execute(f"SELECT {source_col} FROM hunts"):
                raw = row[0]
                if not raw:
                    continue
                # source_sites is JSON in Vulture; fall back to comma-separated text.
                parsed = _parse_source_list(raw)
                for source in parsed:
                    _bump(source, "hunts")

    return [
        {"name": name, **counts}
        for name, counts in sorted(sources.items())
    ]


def _parse_source_list(raw: str) -> list[str]:
    import json

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


# ---------------------------------------------------------------------------
# Log reader
# ---------------------------------------------------------------------------

def _read_log_tail() -> dict[str, Any]:
    result: dict[str, Any] = {"available": False, "warning": None, "lines": []}

    if not LOG_PATH.exists():
        result["warning"] = f"Log file not found at {LOG_PATH}"
        return result

    try:
        with LOG_PATH.open("r", encoding="utf-8", errors="replace") as handle:
            lines = handle.readlines()
        result["lines"] = [line.rstrip("\n") for line in lines[-LOG_TAIL_LINES:]]
        result["available"] = bool(result["lines"])
    except OSError as exc:
        result["warning"] = f"Could not read log file: {exc}"

    return result


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    db = _read_db_snapshot()
    logs = _read_log_tail()

    warnings: list[str] = []
    if db.get("warning"):
        warnings.append(db["warning"])
    if logs.get("warning"):
        warnings.append(logs["warning"])

    context = {
        "title": "Vulture Dashboard",
        "version": "0.1",
        "server_time": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        "db_path": str(DB_PATH),
        "log_path": str(LOG_PATH),
        "warnings": warnings,
        "db": db,
        "logs": logs,
    }
    return templates.TemplateResponse(request, "index.html", context)
