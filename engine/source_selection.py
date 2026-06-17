"""
engine/source_selection.py

Vertical-aware source_sites selection for translated hunts.

All registered adapters in a vertical profile are selected automatically.
Experimental retail/computer sources (Swappa, Best Buy, Newegg) participate
in defaults for their verticals alongside stable marketplace adapters.
"""

from __future__ import annotations

from typing import Optional

from adapters.registry import get_capabilities, list_sources, normalize_source

_STABLE_DEFAULT = ["craigslist"]

# Normalize category/routing keys to canonical vertical profile keys.
_VERTICAL_ALIASES: dict[str, str] = {
    "pc_components": "computer_parts",
    "laptops": "laptops_computers",
    "home_theater": "tv_home_theater",
}

_COMPUTER_ELECTRONICS_SOURCES = [
    "craigslist",
    "facebook_marketplace",
    "mercari",
    "offerup",
    "microcenter",
    "swappa",
    "bestbuy",
    "newegg",
]

_GAMING_SOURCES = [
    "craigslist",
    "facebook_marketplace",
    "mercari",
    "offerup",
    "swappa",
    "bestbuy",
    "newegg",
]

_PHONES_TABLETS_SOURCES = [
    "craigslist",
    "facebook_marketplace",
    "offerup",
    "swappa",
]

_RETAIL_SOURCES = [
    "bestbuy",
    "microcenter",
    "newegg",
]

# Vertical profiles — every source listed is auto-selected when registered.
_VERTICAL_PROFILES: dict[str, list[str]] = {
    "computer_parts": list(_COMPUTER_ELECTRONICS_SOURCES),
    "pc_components": list(_COMPUTER_ELECTRONICS_SOURCES),
    "laptops_computers": list(_COMPUTER_ELECTRONICS_SOURCES),
    "laptops": list(_COMPUTER_ELECTRONICS_SOURCES),
    "gaming": list(_GAMING_SOURCES),
    "electronics": list(_COMPUTER_ELECTRONICS_SOURCES),
    "phones_tablets": list(_PHONES_TABLETS_SOURCES),
    "retail": list(_RETAIL_SOURCES),
    "vehicles": ["craigslist", "facebook_marketplace", "carsdotcom", "offerup"],
    "tv_home_theater": ["craigslist", "facebook_marketplace", "offerup"],
    "home_theater": ["craigslist", "facebook_marketplace", "offerup"],
    "general": ["craigslist", "facebook_marketplace", "offerup", "mercari"],
    "general_marketplace": ["craigslist", "facebook_marketplace", "offerup", "mercari"],
    "furniture_home": ["craigslist", "facebook_marketplace", "offerup"],
}

# Alias for callers that want the full vertical map (same as runtime profiles).
_VERTICAL_CANDIDATES: dict[str, list[str]] = _VERTICAL_PROFILES

_VERTICAL_ONLY_SOURCES: dict[str, frozenset[str]] = {
    "carsdotcom": frozenset({"vehicles"}),
    "microcenter": frozenset({
        "computer_parts",
        "pc_components",
        "laptops_computers",
        "laptops",
        "gaming",
        "electronics",
        "retail",
    }),
    "bestbuy": frozenset({
        "computer_parts",
        "pc_components",
        "laptops_computers",
        "laptops",
        "gaming",
        "electronics",
        "retail",
        "general",
        "general_marketplace",
    }),
    "newegg": frozenset({
        "computer_parts",
        "pc_components",
        "laptops_computers",
        "laptops",
        "gaming",
        "electronics",
        "retail",
        "general",
        "general_marketplace",
    }),
    "mercari": frozenset({
        "computer_parts",
        "pc_components",
        "laptops_computers",
        "laptops",
        "gaming",
        "electronics",
        "general",
        "general_marketplace",
    }),
    "swappa": frozenset({
        "computer_parts",
        "pc_components",
        "laptops_computers",
        "laptops",
        "gaming",
        "electronics",
        "phones_tablets",
        "general",
        "general_marketplace",
    }),
}

# Map routing keys to capability tags declared on adapters.
_CAPABILITY_KEYS: dict[str, frozenset[str]] = {
    "pc_components": frozenset({"pc_components", "computer_parts"}),
    "laptops": frozenset({"laptops", "laptops_computers", "computer_parts"}),
    "gaming": frozenset({"gaming", "computer_parts", "general_marketplace"}),
    "electronics": frozenset({
        "electronics", "computer_parts", "gaming", "general_marketplace",
    }),
    "phones_tablets": frozenset({"phones_tablets", "general_marketplace"}),
    "retail": frozenset({"retail"}),
}


def experimental_adapters_enabled() -> bool:
    """Deprecated: adapters are enabled by default. Always returns True."""
    return True


def _canonical_vertical(vertical_key: str) -> str:
    return _VERTICAL_ALIASES.get(vertical_key, vertical_key)


def _registered_sources() -> frozenset[str]:
    return frozenset(list_sources())


def _dedupe_sources(sources: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for site in sources:
        key = normalize_source(site)
        if key in seen:
            continue
        seen.add(key)
        out.append(key)
    return out


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


def _vertical_capability_keys(vertical_key: str) -> set[str]:
    keys = {vertical_key, _canonical_vertical(vertical_key)}
    keys |= set(_CAPABILITY_KEYS.get(vertical_key, ()))
    if vertical_key == "tv_home_theater":
        keys.add("home_theater")
    if vertical_key == "general":
        keys.add("general_marketplace")
    return keys


def _source_allowed_for_vertical(source: str, vertical_key: str) -> bool:
    """Enforce vehicle-only / category-focused adapter policy."""
    keys = _vertical_capability_keys(vertical_key)
    restricted = _VERTICAL_ONLY_SOURCES.get(source)
    if restricted is not None:
        return bool(keys & restricted)

    caps = get_capabilities(source)
    if not caps:
        return False
    verticals = caps.get("verticals") or []
    if not verticals:
        return True
    return bool(keys & set(verticals))


def resolve_candidate_sources(vertical_key: str) -> list[str]:
    """Return the vertical source map (same as resolve_source_sites without filtering)."""
    key = _canonical_vertical(vertical_key)
    raw = _VERTICAL_PROFILES.get(key) or _VERTICAL_PROFILES.get(vertical_key, [])
    return _dedupe_sources(raw)


def is_executable_source(source: str) -> bool:
    """True when the source has a registered runtime adapter."""
    return normalize_source(source) in _registered_sources()


def resolve_source_sites(
    vertical_key: str,
    *,
    experimental: Optional[bool] = None,
    explicit_sources: Optional[list[str]] = None,
) -> list[str]:
    """
    Return executable source_sites for a translated or manual hunt.

    explicit_sources: when provided (non-empty), use after normalize/filter.
    experimental: ignored (kept for call-site compatibility).
    """
    del experimental
    canonical = _canonical_vertical(vertical_key)
    if explicit_sources:
        return _filter_registered(
            [normalize_source(s) for s in explicit_sources if s and str(s).strip()]
        )

    profile = _VERTICAL_PROFILES.get(canonical) or _VERTICAL_PROFILES.get(
        vertical_key, _STABLE_DEFAULT
    )
    filtered = [
        s for s in _filter_registered(list(profile))
        if _source_allowed_for_vertical(s, canonical)
    ]
    return filtered or list(_STABLE_DEFAULT)


def filter_explicit_source_sites(
    source_sites: list[str],
    vertical_key: str,
) -> list[str]:
    """Normalize manual source_sites and drop unregistered sources."""
    del vertical_key
    return _filter_registered(
        [normalize_source(s) for s in source_sites if s and str(s).strip()]
    )
