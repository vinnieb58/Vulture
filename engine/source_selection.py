"""
engine/source_selection.py

Vertical-aware source_sites selection for translated hunts.

Runtime defaults use only registered adapters (_VERTICAL_PROFILES).
Candidate mappings (_VERTICAL_CANDIDATES) document probe-only and future
sources (Swappa, Best Buy, Micro Center) without adding them to normal hunts.
"""

from __future__ import annotations

from typing import Optional

from adapters.registry import (
    get_capabilities,
    get_probe_capabilities,
    is_registered_source,
    list_sources,
    normalize_source,
)

_STABLE_DEFAULT = ["craigslist"]

# Normalize category/routing keys to canonical vertical profile keys.
_VERTICAL_ALIASES: dict[str, str] = {
    "pc_components": "computer_parts",
    "laptops": "laptops_computers",
    "home_theater": "tv_home_theater",
}

# Runtime default profiles — registered adapters only; unchanged stable behavior.
_VERTICAL_PROFILES: dict[str, list[str]] = {
    "computer_parts": ["craigslist", "mercari", "offerup"],
    "pc_components": ["craigslist", "mercari", "offerup"],
    "laptops_computers": ["craigslist", "mercari", "offerup"],
    "laptops": ["craigslist", "mercari", "offerup"],
    "gaming": ["craigslist", "mercari", "offerup"],
    "electronics": ["craigslist", "mercari", "offerup"],
    "phones_tablets": ["craigslist", "offerup"],
    "retail": ["craigslist"],
    "vehicles": ["craigslist", "carsdotcom", "offerup"],
    "tv_home_theater": ["craigslist", "offerup"],
    "home_theater": ["craigslist", "offerup"],
    "general": ["craigslist", "offerup", "mercari"],
    "general_marketplace": ["craigslist", "offerup", "mercari"],
    "furniture_home": ["craigslist", "offerup"],
}

# Full vertical → source map including probe-only / future candidates.
_VERTICAL_CANDIDATES: dict[str, list[str]] = {
    "computer_parts": [
        "craigslist", "mercari", "offerup", "swappa", "bestbuy", "microcenter",
    ],
    "pc_components": [
        "craigslist", "mercari", "offerup", "swappa", "bestbuy", "microcenter",
    ],
    "laptops_computers": [
        "craigslist", "mercari", "offerup", "swappa", "bestbuy",
    ],
    "laptops": ["craigslist", "mercari", "offerup", "swappa", "bestbuy"],
    "gaming": ["craigslist", "mercari", "offerup", "swappa"],
    "electronics": [
        "craigslist", "mercari", "offerup", "swappa", "bestbuy", "microcenter",
    ],
    "phones_tablets": ["craigslist", "offerup", "swappa"],
    "retail": ["bestbuy", "microcenter"],
    "vehicles": ["craigslist", "carsdotcom", "offerup"],
    "tv_home_theater": ["craigslist", "offerup"],
    "home_theater": ["craigslist", "offerup"],
    "general": ["craigslist", "offerup", "mercari"],
    "general_marketplace": ["craigslist", "offerup", "mercari"],
    "furniture_home": ["craigslist", "offerup"],
}

# Registered sources excluded from translated-hunt defaults (manual opt-in only).
_NON_DEFAULT_RUNTIME_SOURCES: frozenset[str] = frozenset({
    "swappa",
})

_VERTICAL_ONLY_SOURCES: dict[str, frozenset[str]] = {
    "carsdotcom": frozenset({"vehicles"}),
    "mercari": frozenset({
        "computer_parts",
        "pc_components",
        "laptops_computers",
        "laptops",
        "gaming",
        "electronics",
        "general",
        "general_marketplace",
        # phones_tablets intentionally omitted — Swappa candidate vertical
    }),
}

# Map new routing keys to capability tags already declared on adapters.
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
    """
    Return the full vertical source map including probe-only candidates.

    Does not imply runtime executability — use resolve_source_sites() for hunts.
    """
    key = _canonical_vertical(vertical_key)
    raw = _VERTICAL_CANDIDATES.get(key) or _VERTICAL_CANDIDATES.get(vertical_key, [])
    return _dedupe_sources(raw)


def is_executable_source(source: str) -> bool:
    """True when the source has a registered runtime adapter."""
    return is_registered_source(source)


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
        and s not in _NON_DEFAULT_RUNTIME_SOURCES
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
