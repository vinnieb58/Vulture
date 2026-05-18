from pathlib import Path
import sqlite3
import sys

DB_PATH = Path("data") / "vulture.db"


def table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (table_name,),
    ).fetchone()
    return row is not None


def main() -> int:
    if not DB_PATH.exists():
        print(f"DB not found: {DB_PATH}")
        print("Nothing to clear.")
        return 0

    conn = sqlite3.connect(DB_PATH)
    try:
        hunts_exists = table_exists(conn, "hunts")
        listings_exists = table_exists(conn, "listings")

        if not hunts_exists and not listings_exists:
            print("No hunts or listings tables found. Nothing to clear.")
            return 0

        deleted_hunts = 0
        deleted_listings = 0

        if hunts_exists:
            deleted_hunts = conn.execute("SELECT COUNT(*) FROM hunts").fetchone()[0]
            conn.execute("DELETE FROM hunts")

        if listings_exists:
            deleted_listings = conn.execute("SELECT COUNT(*) FROM listings").fetchone()[0]
            conn.execute("DELETE FROM listings")

        conn.commit()

        print("Vulture dev DB reset complete.")
        print(f"  Hunts cleared:    {deleted_hunts}")
        print(f"  Listings cleared: {deleted_listings}")
        print(f"  Database kept:    {DB_PATH}")

        return 0
    except Exception as exc:
        conn.rollback()
        print(f"Reset failed: {exc}")
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())