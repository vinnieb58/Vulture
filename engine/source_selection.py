"""
engine/source_selection.py

Vertical-aware source_sites selection with experimental-adapter gating.

Production hunts (default): craigslist only.
Dev/test (VULTURE_ENABLE_EXPERIMENTAL_ADAPTERS=true): multi-source profiles
per vertical. Explicit source lists (e.g. /hunt_create) bypass gating.
"""

from __future__ import annotations

import os
from typing import Optional

from adapters.registry import get_capabilities, list_sources, normalize_source

_STABLE_DEFAULT = ["craigslist"]

# Vertical keys match VERTICALS in llm_translator.py and v2 classify_vertical().
_EXPERIMENTAL_PROFILES: dict[str, list[str]] = {
    "computer_parts": ["craigslist", "mercari", "offerup"],
    "laptops_computers": ["craigslist", "mercari", "offerup"],
    "vehicles": ["craigslist", "carsdotcom", "offerup"],
    "tv_home_theater": ["craigslist", "offerup"],
    "home_theater": ["craigslist", "offerup"],  # v2 vertical name
    "general": ["craigslist", "offerup"],
    "general_marketplace": ["craigslist", "offerup"],
    "furniture_home": ["craigslist", "offerup"],
}

# Sources that must never appear outside their allowed verticals when auto-selected.
_VERTICAL_ONLY_SOURCES: dict[str, frozenset[str]] = {
    "carsdotcom": frozenset({"vehicles"}),
    "mercari": frozenset({
        "computer_parts",
        "laptops_computers",
        "gaming",
    }),
}


def experimental_adapters_enabled() -> bool:
    """True when VULTURE_ENABLE_EXPERIMENTAL_ADAPTERS is a truthy env value."""
    raw = os.getenv("VULTURE_ENABLE_EXPERIMENTAL_ADAPTERS", "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


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
    """Enforce vehicle-only / electronics-focused adapter policy."""
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

    explicit_sources: when provided (non-empty), use after normalize/filter;
      experimental sources are allowed without the env flag.
    experimental: override env; default reads VULTURE_ENABLE_EXPERIMENTAL_ADAPTERS.
    """
    if explicit_sources:
        cleaned = _filter_registered([normalize_source(s) for s in explicit_sources if s and str(s).strip()])
        return cleaned

    use_experimental = (
        experimental_adapters_enabled()
        if experimental is None
        else experimental
    )
    if not use_experimental:
        return list(_STABLE_DEFAULT)

    profile = _EXPERIMENTAL_PROFILES.get(vertical_key, _STABLE_DEFAULT)
    filtered = [
        s for s in _filter_registered(list(profile))
        if _source_allowed_for_vertical(s, vertical_key)
    ]
    return filtered or list(_STABLE_DEFAULT)


def filter_explicit_source_sites(
    source_sites: list[str],
    vertical_key: str,
) -> list[str]:
    """
    Normalize manual source_sites and drop unknown sources.
    Does not strip experimental adapters when explicitly requested.
    """
    return _filter_registered([normalize_source(s) for s in source_sites if s and str(s).strip()])
