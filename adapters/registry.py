"""
adapters/registry.py

Central adapter registry for Vulture.

Responsibilities:
- Normalize source names (lowercase, strip whitespace)
- Return the search function for a given source name
- Expose capability metadata for each registered source
- Provide a single place to register future adapters

Usage:
    from adapters.registry import get_adapter, get_capabilities, list_sources

    fn = get_adapter("craigslist")   # returns search_craigslist, or None if unknown
    caps = get_capabilities("craigslist")   # returns metadata dict, or None
    sources = list_sources()         # ["craigslist", ...]
"""

import logging
from typing import Callable, Optional

from adapters.craigslist import search_craigslist
from adapters.offerup import search_offerup

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Capability metadata
# Describes what each source supports so callers can make informed decisions
# without probing the adapter directly.
# ---------------------------------------------------------------------------

_CAPABILITIES: dict[str, dict] = {
    "craigslist": {
        "stable": True,
        "experimental": False,
        "requires_browser": False,
        "requires_login": False,
        "supports_location": True,
        "location_control": "verified",
        "supports_radius": False,
        "supports_price_filter_in_url": False,
        "verticals": [
            "general_marketplace",
            "computer_parts",
            "vehicles",
            "home_theater",
        ],
    },
    # -------------------------------------------------------------------------
    # OfferUp — experimental (usable on residential IP hosts like Raven)
    #
    # Parsing: requests + BeautifulSoup + __NEXT_DATA__ JSON (Next.js SSR).
    # No browser automation required.  No login required for basic search.
    #
    # Location: GeoIP-only — results are determined by the requesting IP's
    # geographic location, not by any URL parameter or cookie.
    # A systematic probe (experiments/adapters/offerup_location_probe.py,
    # May 2026) tested 9 strategies (lat/lng, zip, location string,
    # location_slug, path slugs, multiple cookie injections) for Houston TX,
    # Dallas TX, and Arlington VA — every strategy returned identical results.
    #
    # Recommended runtime: a residential IP in the target city (e.g. Raven
    # running from the user's home Houston-area connection).  Do NOT rely on
    # OfferUp for city-targeted hunts from cloud/datacenter IPs.
    #
    # The city argument accepted by search_offerup() is advisory only — it is
    # logged for observability but does not affect which listings are returned.
    # -------------------------------------------------------------------------
    "offerup": {
        "stable": False,
        "experimental": True,
        "requires_browser": False,
        "requires_login": False,
        "supports_location": False,
        "location_control": "geoip_only",
        "recommended_runtime": "residential_ip",
        "supports_radius": False,
        "supports_price_filter_in_url": False,
        "verticals": [
            "general_marketplace",
            "computer_parts",
            "gaming",
            "home_theater",
            "vehicles",
        ],
    },
}

# ---------------------------------------------------------------------------
# Adapter registry
# Maps normalized source name -> callable that returns list[Listing].
#
# CURRENT CALLABLE CONTRACT (Craigslist-shaped):
#   adapter_fn(query=str, city=str, limit=int) -> list[Listing]
#
# All registered adapters must match this signature for the time being.
# run_hunt() in main.py calls every adapter with these three keyword args.
#
# TODO: When a second adapter is added, evaluate one of:
#   1. A shared AdapterContext / execution-dict approach so run_hunt() passes
#      a single structured object instead of unpacked kwargs.
#   2. Per-adapter thin wrappers registered here that translate from the
#      common (query, city, limit) call into whatever the adapter needs.
# Do not change this contract in the current PR.
# ---------------------------------------------------------------------------

_REGISTRY: dict[str, Callable] = {
    "craigslist": search_craigslist,
    "offerup": search_offerup,
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def normalize_source(source: str) -> str:
    """Return source name lowercased and stripped of whitespace."""
    return source.strip().lower()


def get_adapter(source: str) -> Optional[Callable]:
    """
    Return the search function for *source*, or None if unregistered.

    Logs a warning on miss so the caller can skip gracefully rather than crash.
    """
    key = normalize_source(source)
    fn = _REGISTRY.get(key)
    if fn is None:
        log.warning(
            "No adapter registered for source '%s'. "
            "Registered sources: %s",
            source,
            sorted(_REGISTRY.keys()),
        )
    return fn


def get_capabilities(source: str) -> Optional[dict]:
    """Return the capability metadata dict for *source*, or None if unknown."""
    return _CAPABILITIES.get(normalize_source(source))


def list_sources() -> list[str]:
    """Return a sorted list of all registered source names."""
    return sorted(_REGISTRY.keys())
