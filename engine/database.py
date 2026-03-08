import sqlite3
from pathlib import Path

from models.listing import Listing


DB_PATH = Path("data/vulture.db")


def get_connection() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with get_connection() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS listings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source TEXT NOT NULL,
                title TEXT NOT NULL,
                price INTEGER,
                location TEXT,
                link TEXT NOT NULL UNIQUE,
                first_seen TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.commit()


def listing_exists(link: str) -> bool:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT 1 FROM listings WHERE link = ?",
            (link,),
        ).fetchone()
        return row is not None


def save_listing(listing: Listing) -> bool:
    """
    Returns True if a new listing was inserted.
    Returns False if it already existed.
    """
    if listing_exists(listing.link):
        return False

    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO listings (source, title, price, location, link)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                listing.source,
                listing.title,
                listing.price,
                listing.location,
                listing.link,
            ),
        )
        conn.commit()

    return True