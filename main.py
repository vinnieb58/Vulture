from adapters.craigslist import search_craigslist
from engine.database import init_db, save_listing
from engine.hunts import load_hunts
from engine.notifier import send_discord_alert


def run_hunt(hunt: dict) -> None:
    name = hunt["name"]
    source = hunt["source"]

    print(f"\n=== Running hunt: {name} ({source}) ===")

    if source == "craigslist":
        listings = search_craigslist(
            query=hunt["query"],
            city=hunt.get("city", "houston"),
            limit=hunt.get("limit", 10),
        )
    else:
        print(f"Skipping unsupported source: {source}")
        return

    new_count = 0
    old_count = 0

    for listing in listings:
        was_inserted = save_listing(listing)

        if was_inserted:
            new_count += 1
            print(f"NEW: {listing}")
            send_discord_alert(listing)
        else:
            old_count += 1
            print(f"OLD: {listing.link}")

    print(f"Done hunt '{name}'. New: {new_count}, Existing: {old_count}")


def main() -> None:
    init_db()
    hunts = load_hunts()

    for hunt in hunts:
        run_hunt(hunt)


if __name__ == "__main__":
    main()