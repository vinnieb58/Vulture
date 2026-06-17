#!/usr/bin/env python3
"""
Remove ended hunts from the Vulture SQLite database.

Default mode is dry-run (no mutations). Pass --apply to delete rows.

Listings are global dedup cache (no hunt_id FK) and are never modified.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB_PATH = REPO_ROOT / "data" / "vulture.db"

ENDED_STATUS = "ended"


@dataclass(frozen=True)
class EndedHunt:
    hunt_id: str
    name: str


@dataclass
class PruneReport:
    db_path: Path
    ended_hunts: list[EndedHunt]
    listings_affected: int = 0

    @property
    def count(self) -> int:
        return len(self.ended_hunts)


def connect_db(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (table_name,),
    ).fetchone()
    return row is not None


def fetch_ended_hunts(conn: sqlite3.Connection) -> list[EndedHunt]:
    if not table_exists(conn, "hunts"):
        return []
    rows = conn.execute(
        """
        SELECT hunt_id, name
        FROM hunts
        WHERE status = ?
        ORDER BY created_at
        """,
        (ENDED_STATUS,),
    ).fetchall()
    return [EndedHunt(hunt_id=row["hunt_id"], name=row["name"]) for row in rows]


def count_listings_for_hunts(conn: sqlite3.Connection, hunt_ids: list[str]) -> int:
    """
    Listings have no hunt_id column; nothing is linked at the DB level.
    """
    _ = conn, hunt_ids
    return 0


def build_report(db_path: Path) -> PruneReport:
    if not db_path.exists():
        return PruneReport(db_path=db_path, ended_hunts=[])

    conn = connect_db(db_path)
    try:
        ended = fetch_ended_hunts(conn)
        listings = count_listings_for_hunts(conn, [h.hunt_id for h in ended])
        return PruneReport(db_path=db_path, ended_hunts=ended, listings_affected=listings)
    finally:
        conn.close()


def apply_prune(db_path: Path, hunt_ids: list[str]) -> int:
    if not hunt_ids:
        return 0
    conn = connect_db(db_path)
    try:
        placeholders = ",".join("?" for _ in hunt_ids)
        cursor = conn.execute(
            f"DELETE FROM hunts WHERE hunt_id IN ({placeholders})",
            hunt_ids,
        )
        conn.commit()
        return cursor.rowcount
    finally:
        conn.close()


def format_report(report: PruneReport, *, apply: bool) -> str:
    mode = "APPLY" if apply else "DRY-RUN"
    lines = [
        f"Prune ended hunts [{mode}]",
        f"  Database: {report.db_path}",
        f"  Ended hunts found: {report.count}",
        f"  Related listing rows affected: {report.listings_affected}",
    ]
    if report.count == 0:
        lines.append("  (none)")
        return "\n".join(lines)

    lines.append("  Hunts to remove:")
    for hunt in report.ended_hunts:
        lines.append(f"    - {hunt.name}  ({hunt.hunt_id})")
    if not apply:
        lines.append("")
        lines.append("  No changes made. Re-run with --apply to delete these hunts.")
    return "\n".join(lines)


def run(db_path: Path, *, apply: bool) -> int:
    report = build_report(db_path)
    print(format_report(report, apply=apply))

    if not apply:
        return 0 if db_path.exists() or report.count == 0 else 0

    if not db_path.exists():
        print("Database file does not exist; nothing to apply.")
        return 1

    if report.count == 0:
        print("Nothing to delete.")
        return 0

    deleted = apply_prune(db_path, [h.hunt_id for h in report.ended_hunts])
    print(f"Deleted {deleted} ended hunt(s) from {db_path}.")
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Remove ended hunts from the Vulture SQLite database.",
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=DEFAULT_DB_PATH,
        help=f"SQLite database path (default: {DEFAULT_DB_PATH})",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually delete ended hunts (default is dry-run)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Explicit dry-run (default when --apply is omitted)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.apply and args.dry_run:
        print("Cannot use --apply and --dry-run together.", file=sys.stderr)
        return 2
    return run(args.db.resolve(), apply=args.apply)


if __name__ == "__main__":
    sys.exit(main())
