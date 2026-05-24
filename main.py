import logging
import os
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv()  # load .env before reading any os.getenv() calls below
except ModuleNotFoundError:
    pass  # dotenv not installed; rely on env vars already being set

from adapters.registry import get_adapter
from engine.database import init_db, save_listing
from engine.hunt_repository import init_hunts_table
from engine.hunt_service import HuntValidationError
from engine.hunt_service import hunt_to_execution_dict
from engine.hunt_service import list_hunts as list_db_hunts
from engine.hunts import load_hunts
from engine.notifier import send_discord_alert
from engine.rules import rejection_reason

# ---------------------------------------------------------------------------
# Hunt source selection
#
# Set VULTURE_HUNT_SOURCE in .env or the system environment:
#   yaml   — load from config/hunts.yaml only   (default; v1.0 behavior)
#   db     — load from the SQLite hunts table only
#   mixed  — load from both; YAML takes priority on name collision
# ---------------------------------------------------------------------------
_VALID_HUNT_SOURCES = frozenset({"yaml", "db", "mixed"})

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


def run_hunt(hunt: dict) -> int:
    name = hunt["name"]
    # Normalize to lowercase so "Craigslist" and "craigslist" both match
    source = hunt["source"].lower()
    rules = hunt.get("rules") or {}

    # hunt_id is present on DB-backed hunts; absent on YAML hunts
    hunt_id = hunt.get("hunt_id", "")
    id_tag = f" [{hunt_id}]" if hunt_id else ""

    log.info("Starting hunt: %s (%s)%s", name, source, id_tag)

    adapter_fn = get_adapter(source)
    if adapter_fn is None:
        # Warning already logged by get_adapter; skip this hunt gracefully.
        return 0

    listings = adapter_fn(
        query=hunt["query"],
        city=hunt.get("city", "houston"),
        limit=hunt.get("limit", 10),
    )

    new_count = 0
    old_count = 0
    filtered_count = 0

    for listing in listings:
        reason = rejection_reason(listing, rules)
        if reason is not None:
            filtered_count += 1
            log.info("FILTERED %r: %s", listing.title[:60], reason)
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
        "Done hunt '%s' [%s]%s. New: %d, Existing: %d, Filtered: %d",
        name, source, id_tag, new_count, old_count, filtered_count,
    )
    return new_count


def _expand_hunt_sources(hunt: dict) -> list[dict]:
    """
    Expand a hunt dict into one dict per source in source_sites.

    - DB hunts produced by hunt_to_execution_dict() carry a "source_sites"
      list.  Each site becomes its own execution dict with "source" set to
      that site.  This lets the main loop run every source independently and
      isolate failures per source.
    - YAML hunts carry only "source" (singular) and no "source_sites" key.
      They pass through unchanged, preserving v1.0 single-source behaviour.
    - A DB hunt with exactly one source_site is also returned unchanged (no
      extra allocation, identical behaviour to today).
    """
    source_sites = hunt.get("source_sites")
    if not source_sites or len(source_sites) <= 1:
        return [hunt]
    # Fan out: shallow-copy the shared dict, override "source" for each site.
    return [{**hunt, "source": site} for site in source_sites]


def _resolve_hunt_source() -> str:
    """
    Read VULTURE_HUNT_SOURCE from the environment and validate it.
    Falls back to 'yaml' (v1.0 behavior) on missing or unrecognised values.
    """
    raw = os.getenv("VULTURE_HUNT_SOURCE", "yaml").strip().lower()
    if raw not in _VALID_HUNT_SOURCES:
        log.warning(
            "Unknown VULTURE_HUNT_SOURCE '%s'; falling back to 'yaml'. "
            "Valid values: %s",
            raw, sorted(_VALID_HUNT_SOURCES),
        )
        return "yaml"
    return raw


def _load_db_hunts() -> list[dict]:
    """
    Load active hunts from the DB and convert them to execution dicts.

    Hunts that fail conversion (missing search_terms or source_sites) are
    logged as warnings and skipped; the cycle continues.
    """
    active = list_db_hunts(status="active")
    result = []
    for hunt in active:
        try:
            result.append(hunt_to_execution_dict(hunt))
        except HuntValidationError as exc:
            log.warning("Skipping DB hunt '%s': %s", hunt.name, exc)
    return result


def _merge_hunts(yaml_hunts: list[dict], db_hunts: list[dict]) -> list[dict]:
    """
    Combine YAML and DB hunt lists for mixed mode.

    YAML entries take priority: if both sources define a hunt with the same
    name the DB entry is skipped and a warning is logged.
    """
    seen = {h["name"] for h in yaml_hunts}
    merged = list(yaml_hunts)
    for hunt in db_hunts:
        if hunt["name"] in seen:
            log.warning(
                "Skipping duplicate DB hunt '%s' (already present in YAML)",
                hunt["name"],
            )
        else:
            seen.add(hunt["name"])
            merged.append(hunt)
    return merged


def main() -> None:
    setup_logging()
    log.info("Starting Vulture hunt cycle")
    init_db()
    init_hunts_table()

    source = _resolve_hunt_source()
    log.info("Hunt source: %s", source)

    if source == "yaml":
        hunts = load_hunts()
        log.info("Loaded %d YAML hunt(s)", len(hunts))
    elif source == "db":
        hunts = _load_db_hunts()
        log.info("Loaded %d DB hunt(s)", len(hunts))
    else:  # mixed
        yaml_hunts = load_hunts()
        db_hunts = _load_db_hunts()
        hunts = _merge_hunts(yaml_hunts, db_hunts)
        log.info(
            "Loaded %d YAML hunt(s) and %d DB hunt(s) -> %d after dedup",
            len(yaml_hunts), len(db_hunts), len(hunts),
        )

    # Expand multi-source hunts into one execution unit per source so that
    # each source runs independently and a failure in one does not prevent
    # the others from executing.  YAML hunts (single "source" key, no
    # "source_sites") pass through _expand_hunt_sources unchanged.
    expanded: list[dict] = []
    for hunt in hunts:
        expanded.extend(_expand_hunt_sources(hunt))
    if len(expanded) != len(hunts):
        log.info(
            "Expanded %d hunt(s) into %d source-run(s)",
            len(hunts), len(expanded),
        )

    total_new = 0
    for hunt in expanded:
        try:
            total_new += run_hunt(hunt) or 0
        except Exception:
            log.exception(
                "Hunt '%s' [%s] failed unexpectedly",
                hunt.get("name", "unknown"),
                hunt.get("source", "?"),
            )

    log.info("%d new listing(s) found", total_new)
    log.info("Hunt cycle completed")


if __name__ == "__main__":
    main()