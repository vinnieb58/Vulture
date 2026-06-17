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

from adapters.bestbuy import search_bestbuy
from adapters.carsdotcom import search_carsdotcom
from adapters.craigslist import search_craigslist
from adapters.facebook_marketplace import search_facebook_marketplace
from adapters.mercari import search_mercari
from adapters.microcenter import search_microcenter
from adapters.newegg import search_newegg
from adapters.offerup import search_offerup
from adapters.swappa import search_swappa

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Capability metadata
# Describes what each source supports so callers can make informed decisions
# without probing the adapter directly.
# ---------------------------------------------------------------------------

_CAPABILITIES: dict[str, dict] = {
    "craigslist": {
        "status": "stable",
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
            "gaming",
            "electronics",
            "phones_tablets",
            "vehicles",
            "home_theater",
            "furniture_home",
        ],
    },
    # -------------------------------------------------------------------------
    # OfferUp — production-usable (GeoIP-only location; residential IP recommended)
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
        "status": "experimental",
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
            "electronics",
            "phones_tablets",
            "home_theater",
            "vehicles",
            "furniture_home",
        ],
    },
    "mercari": {
        "status": "beta",
        "stable": True,
        "experimental": False,
        "requires_browser": False,
        "requires_login": False,
        "supports_location": False,
        "supports_radius": False,
        "supports_price_filter_in_url": False,
        "verticals": [
            "general_marketplace",
            "computer_parts",
            "gaming",
            "electronics",
            "phones_tablets",
            "laptops_computers",
            "home_theater",
        ],
    },
    # -------------------------------------------------------------------------
    # Cars.com — production-usable on Raven but browser/blocking-sensitive
    #
    # Parsing: Playwright Chromium → data-vehicle-details JSON attr + DOM selectors.
    # Each <fuse-card> custom element embeds the full vehicle payload as a JSON
    # attribute (year, make, model, trim, vin, price, mileage, listingId).
    #
    # Anti-bot: Cloudflare Bot Management + Akamai Bot Manager are present.
    # Intermittent ERR_HTTP2_PROTOCOL_ERROR / Cloudflare RST-stream blocks are
    # possible even on residential IPs.  search_carsdotcom() logs and returns []
    # on failure — it never raises, so a failed Cars.com fetch does not crash
    # the hunt cycle.
    #
    # Location: zip-code targeted. The search URL accepts &zip=XXXXX and
    # Cars.com correctly limits/ranks results to that geography.  Pass a
    # 5-digit zip as the city argument (e.g. city="77002").
    # Falls back to 77471 (Rosenberg, TX — Raven's GeoIP area) if city
    # is not a zip code.
    #
    # Vertical: vehicles only. Not a general marketplace.
    # -------------------------------------------------------------------------
    "carsdotcom": {
        "status": "experimental",
        "stable": False,
        "experimental": True,
        "flaky": True,
        "browser_sensitive": True,
        "blocking_risk": "cloudflare_akamai",
        "failure_mode": "returns_empty_list",
        "requires_browser": True,
        "requires_login": False,
        "supports_location": True,
        "location_control": "zip",
        "recommended_runtime": "residential_ip",
        "supports_radius": False,
        "supports_price_filter_in_url": False,
        "verticals": ["vehicles"],
    },
    # -------------------------------------------------------------------------
    # Micro Center — production-usable on Raven; Playwright required (requests blocked)
    #
    # Parsing: Playwright Chromium → #productGrid li.product_wrapper,
    # data-name / data-price on product anchors, .price_wrapper for stock text.
    #
    # Plain HTTP returns 403 "Just a moment..." from datacenter IPs.
    # Validated on Raven (residential) May 2026 — smoke + in-stock store compare.
    #
    # Location: storeid query param (e.g. 115 Brooklyn, 141 Columbus). Pass via
    # hunt adapter_options["storeid"] or city names/ids (see search_microcenter).
    # Included in computer_parts / laptops_computers vertical source profiles.
    # -------------------------------------------------------------------------
    "microcenter": {
        "status": "beta",
        "stable": True,
        "experimental": False,
        "flaky": True,
        "browser_sensitive": True,
        "blocking_risk": "cloudflare",
        "failure_mode": "returns_empty_list",
        "requires_browser": True,
        "requires_login": False,
        "supports_location": True,
        "location_control": "storeid",
        "recommended_runtime": "residential_ip",
        "supports_radius": False,
        "supports_price_filter_in_url": False,
        "verticals": [
            "retail",
            "computer_parts",
            "pc_components",
            "gaming",
            "electronics",
            "laptops_computers",
        ],
    },
    # -------------------------------------------------------------------------
    # Swappa — experimental (electronics / gaming / computer hunts)
    #
    # Parsing: requests + BeautifulSoup on server-rendered HTML.
    # Flow: /search?q=... → /listings/{slug} → .xui_card_wrapper cards.
    # No browser automation or login required for basic search (May 2026 probe).
    #
    # Location: not targetable via URL/cookies. Per-listing ship-from city/state
    # may appear in .ships_from when the seller exposes it.
    # -------------------------------------------------------------------------
    "swappa": {
        "status": "experimental",
        "stable": False,
        "experimental": True,
        "requires_browser": False,
        "requires_login": False,
        "supports_location": False,
        "location_control": "not_supported",
        "supports_radius": False,
        "supports_price_filter_in_url": False,
        "verticals": [
            "general_marketplace",
            "computer_parts",
            "pc_components",
            "gaming",
            "electronics",
            "phones_tablets",
            "laptops_computers",
            "laptops",
        ],
    },
    # -------------------------------------------------------------------------
    # Best Buy — experimental (Playwright required; Raven-validated May 2026)
    #
    # Parsing: Playwright Chromium → BeautifulSoup on rendered SRP HTML.
    # Anti-bot: Akamai blocks plain requests; Playwright loads full pages.
    # Location: pickup/fulfillment text when visible; ``city`` arg is ignored.
    # -------------------------------------------------------------------------
    "bestbuy": {
        "status": "experimental",
        "stable": False,
        "experimental": True,
        "flaky": True,
        "browser_sensitive": True,
        "blocking_risk": "akamai",
        "failure_mode": "returns_empty_list",
        "requires_browser": True,
        "requires_login": False,
        "supports_location": False,
        "location_control": "not_supported",
        "recommended_runtime": "residential_ip",
        "supports_radius": False,
        "supports_price_filter_in_url": False,
        "verticals": [
            "retail",
            "computer_parts",
            "pc_components",
            "gaming",
            "electronics",
            "laptops_computers",
            "laptops",
            "general_marketplace",
        ],
    },
    # -------------------------------------------------------------------------
    # Newegg — experimental (retail / computer parts / gaming / electronics)
    #
    # Parsing: requests + BeautifulSoup on server-rendered search HTML.
    # Accept-Encoding must omit Brotli unless brotlicffi is installed.
    # Location: not targetable; ``city`` arg is advisory only.
    # -------------------------------------------------------------------------
    "newegg": {
        "status": "experimental",
        "stable": False,
        "experimental": True,
        "requires_browser": False,
        "requires_login": False,
        "supports_location": False,
        "location_control": "not_supported",
        "supports_radius": False,
        "supports_price_filter_in_url": False,
        "failure_mode": "returns_empty_list",
        "verticals": [
            "retail",
            "computer_parts",
            "pc_components",
            "gaming",
            "electronics",
            "laptops_computers",
            "laptops",
            "general_marketplace",
        ],
    },
    # -------------------------------------------------------------------------
    # Facebook Marketplace — experimental; enabled in default vertical profiles
    # for this single-user Raven deployment (not retail-only profiles).
    #
    # Parsing: Playwright Chromium → SSR JSON / feed_units / DOM item links.
    # Raven residential smoke tests (May 2026) returned listings for steam deck,
    # rtx 4070, 65 inch tv, and mercedes e550 but every run also reported
    # login_wall and captcha_checkpoint blockers. Public access is fragile.
    #
    # No credentials, cookies, sessions, or CAPTCHA/login bypass implemented.
    # Remove from default profiles if noisy or blocked; adapter stays registered.
    # -------------------------------------------------------------------------
    "facebook_marketplace": {
        "status": "experimental",
        "stable": False,
        "experimental": True,
        "flaky": True,
        "browser_sensitive": True,
        "blocking_risk": "login_captcha_checkpoint",
        "failure_mode": "returns_empty_list",
        "requires_browser": True,
        "requires_login": False,
        "supports_location": True,
        "location_control": "city_slug",
        "recommended_runtime": "residential_ip",
        "supports_radius": False,
        "supports_price_filter_in_url": False,
        "default_profile_allowed": True,
        "verticals": [
            "general",
            "general_marketplace",
            "computer_parts",
            "laptops_computers",
            "gaming",
            "electronics",
            "phones_tablets",
            "vehicles",
            "home_theater",
            "furniture_home",
        ],
    },
}

# Reserved for future probe-only sources (no runtime adapter yet).
_PROBE_CAPABILITIES: dict[str, dict] = {}

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
    "bestbuy": search_bestbuy,
    "carsdotcom": search_carsdotcom,
    "craigslist": search_craigslist,
    "facebook_marketplace": search_facebook_marketplace,
    "microcenter": search_microcenter,
    "newegg": search_newegg,
    "offerup": search_offerup,
    "mercari": search_mercari,
    "swappa": search_swappa,
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
    """Return runtime adapter capability metadata, or None if unknown."""
    return _CAPABILITIES.get(normalize_source(source))


def get_probe_capabilities(source: str) -> Optional[dict]:
    """Return probe-only / future source metadata, or None if unknown."""
    return _PROBE_CAPABILITIES.get(normalize_source(source))


def get_source_metadata(source: str) -> Optional[dict]:
    """Return runtime or probe-only metadata for *source*."""
    key = normalize_source(source)
    if key in _CAPABILITIES:
        return _CAPABILITIES[key]
    return _PROBE_CAPABILITIES.get(key)


def is_registered_source(source: str) -> bool:
    """True when *source* has a runtime search adapter in _REGISTRY."""
    return normalize_source(source) in _REGISTRY


def list_sources() -> list[str]:
    """Return a sorted list of all registered source names."""
    return sorted(_REGISTRY.keys())


def list_probe_sources() -> list[str]:
    """Return probe-only / future source names (no runtime adapter)."""
    return sorted(_PROBE_CAPABILITIES.keys())
