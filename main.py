import logging
from pathlib import Path

from adapters.craigslist import search_craigslist
from engine.database import init_db, save_listing
from engine.hunts import load_hunts
from engine.notifier import send_discord_alert
from engine.rules import matches_rules

log = logging.getLogger(__name__)


def setup_logging() -> None:
    Path("logs").mkdir(exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.FileHandler("logs/vulture.log", encoding="utf-8"),
            logging.StreamHandler(),
        ],
    )


def run_hunt(hunt: dict) -> None:
    name = hunt["name"]
    source = hunt["source"]
    rules = hunt.get("rules") or {}

    log.info("Starting hunt: %s (%s)", name, source)

    if source == "craigslist":
        listings = search_craigslist(
            query=hunt["query"],
            city=hunt.get("city", "houston"),
            limit=hunt.get("limit", 10),
        )
    else:
        log.warning("Skipping unsupported source: %s", source)
        return

    new_count = 0
    old_count = 0
    filtered_count = 0

    for listing in listings:
        if not matches_rules(listing, rules):
            filtered_count += 1
            log.info("FILTERED: %s", listing.link)
            continue

        was_inserted = save_listing(listing)

        if was_inserted:
            new_count += 1
            log.info("NEW: %s", listing)
            send_discord_alert(listing)
        else:
            old_count += 1
            log.info("OLD: %s", listing.link)

    log.info(
        "Done hunt '%s'. New: %d, Existing: %d, Filtered: %d",
        name, new_count, old_count, filtered_count,
    )


def main() -> None:
    setup_logging()
    log.info("Vulture starting")
    init_db()
    hunts = load_hunts()

    for hunt in hunts:
        try:
            run_hunt(hunt)
        except Exception:
            log.exception("Hunt '%s' failed", hunt.get("name", "unknown"))


if __name__ == "__main__":
    main()