"""
engine/source_selection.py

Vertical-aware source_sites selection for translated hunts.

Personal/self-hosted deployment: Craigslist, OfferUp, Mercari, Cars.com,
Micro Center, and experimental retail/computer sources (Newegg, Best Buy,
Swappa when registered) participate in vertical profiles where appropriate.
Capability metadata in the registry documents caveats (geoip_only,
requires_browser, etc.) without blocking runtime when
INCLUDE_EXPERIMENTAL_COMPUTER_RETAIL_DEFAULTS is enabled.
"""

from __future__ import annotations

from typing import Optional

from adapters.registry import get_capabilities, list_sources, normalize_source

_STABLE_DEFAULT = ["craigslist"]

# When True, registered experimental retail/computer adapters (Newegg, Best Buy,
# Swappa) are included in computer/electronics/gaming/laptop/retail vertical
# defaults. They remain experimental=True in registry metadata.
INCLUDE_EXPERIMENTAL_COMPUTER_RETAIL_DEFAULTS = True

_EXPERIMENTAL_COMPUTER_RETAIL_SOURCES = [
    "newegg",
    "bestbuy",
    "swappa",
]

_STABLE_COMPUTER_SOURCES = [
    "craigslist",
    "mercari",
    "offerup",
    "microcenter",
]


def _computer_electronics_profile() -> list[str]:
    """Shared profile for computer_parts, laptops_computers, gaming, electronics."""
    sources = list(_STABLE_COMPUTER_SOURCES)
    if INCLUDE_EXPERIMENTAL_COMPUTER_RETAIL_DEFAULTS:
        sources.extend(_EXPERIMENTAL_COMPUTER_RETAIL_SOURCES)
    return sources


def _retail_profile() -> list[str]:
    """Retail-first sources (no local marketplace defaults)."""
    sources = ["newegg", "bestbuy"]
    if INCLUDE_EXPERIMENTAL_COMPUTER_RETAIL_DEFAULTS:
        return sources
    return list(_STABLE_DEFAULT)


# Vertical keys match VERTICALS in llm_translator.py and v2 classify_vertical().
_VERTICAL_PROFILES: dict[str, list[str]] = {
    "computer_parts": _computer_electronics_profile(),
    "laptops_computers": _computer_electronics_profile(),
    "gaming": _computer_electronics_profile(),
    "electronics": _computer_electronics_profile(),
    "retail": _retail_profile(),
    "vehicles": ["craigslist", "carsdotcom", "offerup"],
    "tv_home_theater": ["craigslist", "offerup"],
    "home_theater": ["craigslist", "offerup"],
    "general": ["craigslist", "offerup", "mercari"],
    "general_marketplace": ["craigslist", "offerup", "mercari"],
    "furniture_home": ["craigslist", "offerup"],
}

_VERTICAL_ONLY_SOURCES: dict[str, frozenset[str]] = {
    "carsdotcom": frozenset({"vehicles"}),
    "microcenter": frozenset({
        "computer_parts",
        "laptops_computers",
        "gaming",
        "electronics",
        "retail",
    }),
    "mercari": frozenset({
        "computer_parts",
        "laptops_computers",
        "gaming",
        "electronics",
        "general",
        "general_marketplace",
    }),
}


def experimental_adapters_enabled() -> bool:
    """Deprecated: adapters are enabled by default. Always returns True."""
    return True


def _registered_sources() -> frozenset[str]:
    return frozenset(list_sources())


def _filter_registered(sources: list[str]) -> list[str]:
    registered = _registered_sources()
    out: list[str] = []
    seen: set[str] = set()
    for site in sources:
        key = normalize_source(site)
        if key not in registered or key in seen:
            continue
        seen.add(key)
        out.append(key)
    return out or list(_STABLE_DEFAULT)


def _source_allowed_for_vertical(source: str, vertical_key: str) -> bool:
    """Enforce vehicle-only / category-focused adapter policy."""
    restricted = _VERTICAL_ONLY_SOURCES.get(source)
    if restricted is not None:
        return vertical_key in restricted

    caps = get_capabilities(source)
    if not caps:
        return False
    verticals = caps.get("verticals") or []
    if not verticals:
        return True
    keys = {vertical_key}
    if vertical_key == "tv_home_theater":
        keys.add("home_theater")
    if vertical_key == "general":
        keys.add("general_marketplace")
    return bool(keys & set(verticals))


def resolve_source_sites(
    vertical_key: str,
    *,
    experimental: Optional[bool] = None,
    explicit_sources: Optional[list[str]] = None,
) -> list[str]:
    """
    Return source_sites for a translated or manual hunt.

    explicit_sources: when provided (non-empty), use after normalize/filter.
    experimental: ignored (kept for call-site compatibility).
    """
    del experimental
    if explicit_sources:
        return _filter_registered(
            [normalize_source(s) for s in explicit_sources if s and str(s).strip()]
        )

    profile = _VERTICAL_PROFILES.get(vertical_key, _STABLE_DEFAULT)
    filtered = [
        s for s in _filter_registered(list(profile))
        if _source_allowed_for_vertical(s, vertical_key)
    ]
    return filtered or list(_STABLE_DEFAULT)


def filter_explicit_source_sites(
    source_sites: list[str],
    vertical_key: str,
) -> list[str]:
    """Normalize manual source_sites and drop unknown sources."""
    del vertical_key
    return _filter_registered(
        [normalize_source(s) for s in source_sites if s and str(s).strip()]
    )
