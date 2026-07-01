"""Area presets for concert geo search."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class GeoSearch:
    """One geo-scoped provider query."""

    city: Optional[str] = None
    state: Optional[str] = None
    lat: Optional[float] = None
    lng: Optional[float] = None
    radius_miles: int = 50
    label: str = ""


# Center coordinates for metro presets (lat, lng).
_METRO_CENTERS: dict[str, tuple[float, float, str, int]] = {
    "houston": (29.7604, -95.3698, "TX", 75),
    "dallas": (32.7767, -96.7970, "TX", 75),
    "austin": (30.2672, -97.7431, "TX", 60),
    "san antonio": (29.4241, -98.4936, "TX", 60),
}

# East Texas anchor cities for multi-city fan-out.
_EAST_TEXAS_CITIES: list[tuple[str, str, float, float, int]] = [
    ("Tyler", "TX", 32.3513, -95.3011, 50),
    ("Longview", "TX", 32.5007, -94.7405, 50),
    ("Beaumont", "TX", 30.0802, -94.1266, 50),
    ("Lufkin", "TX", 31.3382, -94.7291, 50),
    ("Nacogdoches", "TX", 31.6035, -94.6555, 50),
]

# Louisiana anchor cities for multi-city fan-out.
_LOUISIANA_CITIES: list[tuple[str, str, float, float, int]] = [
    ("New Orleans", "LA", 29.9511, -90.0715, 60),
    ("Baton Rouge", "LA", 30.4515, -91.1871, 60),
    ("Lafayette", "LA", 30.2241, -92.0198, 50),
    ("Lake Charles", "LA", 30.2266, -93.2174, 50),
    ("Shreveport", "LA", 32.5252, -93.7502, 60),
]

SUPPORTED_AREAS = frozenset(
    {
        "houston",
        "dallas",
        "austin",
        "san antonio",
        "east texas",
        "louisiana",
        "texas",
        "nationwide",
    }
)


def normalize_area_name(area: str) -> str:
    return " ".join((area or "").strip().lower().split())


def resolve_geo_searches(
    *,
    area: Optional[str] = None,
    city: Optional[str] = None,
    state: Optional[str] = None,
    radius_miles: Optional[int] = None,
) -> list[GeoSearch]:
    """
    Expand area presets or explicit city/state/radius into provider geo queries.

    Returns an empty list for nationwide (no geo constraint).
    """
    if area:
        key = normalize_area_name(area)
        if key == "nationwide":
            return []
        if key in _METRO_CENTERS:
            lat, lng, st, default_radius = _METRO_CENTERS[key]
            return [
                GeoSearch(
                    city=key.title(),
                    state=st,
                    lat=lat,
                    lng=lng,
                    radius_miles=radius_miles or default_radius,
                    label=key,
                )
            ]
        if key == "east texas":
            return [
                GeoSearch(
                    city=c,
                    state=st,
                    lat=lat,
                    lng=lng,
                    radius_miles=radius_miles or r,
                    label=f"east texas ({c})",
                )
                for c, st, lat, lng, r in _EAST_TEXAS_CITIES
            ]
        if key == "louisiana":
            return [
                GeoSearch(
                    city=c,
                    state=st,
                    lat=lat,
                    lng=lng,
                    radius_miles=radius_miles or r,
                    label=f"louisiana ({c})",
                )
                for c, st, lat, lng, r in _LOUISIANA_CITIES
            ]
        if key == "texas":
            searches: list[GeoSearch] = []
            for metro in ("houston", "dallas", "austin", "san antonio"):
                searches.extend(
                    resolve_geo_searches(area=metro, radius_miles=radius_miles)
                )
            searches.extend(
                resolve_geo_searches(area="east texas", radius_miles=radius_miles)
            )
            return searches
        raise ValueError(f"Unknown area preset: {area!r}")

    if city:
        st = (state or "TX").strip().upper()
        city_key = city.strip().lower()
        if city_key in _METRO_CENTERS:
            lat, lng, default_st, default_radius = _METRO_CENTERS[city_key]
            return [
                GeoSearch(
                    city=city.strip(),
                    state=st or default_st,
                    lat=lat,
                    lng=lng,
                    radius_miles=radius_miles or default_radius,
                    label=city.strip(),
                )
            ]
        return [
            GeoSearch(
                city=city.strip(),
                state=st,
                radius_miles=radius_miles or 50,
                label=f"{city.strip()}, {st}",
            )
        ]

    raise ValueError("Provide area preset or city (optionally with state/radius).")
