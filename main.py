from adapters.craigslist import search_craigslist
from engine.database import init_db, save_listing


def main() -> None:
    init_db()

    listings = search_craigslist("graphics card", limit=10)

    new_count = 0
    old_count = 0

    for listing in listings:
        was_inserted = save_listing(listing)

        if was_inserted:
            new_count += 1
            print(f"NEW: {listing}")
        else:
            old_count += 1
            print(f"OLD: {listing.link}")

    print()
    print(f"Done. New: {new_count}, Existing: {old_count}")


if __name__ == "__main__":
    main()