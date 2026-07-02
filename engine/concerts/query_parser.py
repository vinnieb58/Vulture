"""Parse /concert command filter strings."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

from engine.concerts.areas import SUPPORTED_AREAS, normalize_area_name
from engine.concerts.search import SearchCriteria

_FILTER_RE = re.compile(
    r'(\w+):"([^"]*)"|(\w+):(\S+)',
)


@dataclass
class ParsedFilters:
    artist: Optional[str] = None
    genre: Optional[str] = None
    area: Optional[str] = None
    city: Optional[str] = None
    state: Optional[str] = None
    radius: Optional[int] = None
    days: Optional[int] = None
    force: bool = False


def parse_filter_string(text: str) -> ParsedFilters:
    """Parse key:value filters from a command query string."""
    filters = ParsedFilters()
    if not text:
        return filters

    for match in _FILTER_RE.finditer(text.strip()):
        if match.group(1):
            key = match.group(1).lower()
            value = match.group(2)
        else:
            key = match.group(3).lower()
            value = match.group(4)

        if key in ("artist", "artist_query"):
            filters.artist = value
        elif key == "genre":
            filters.genre = value
        elif key == "area":
            filters.area = normalize_area_name(value)
        elif key == "city":
            filters.city = value
        elif key == "state":
            filters.state = value.upper()
        elif key == "radius":
            filters.radius = int(value)
        elif key == "days":
            filters.days = int(value)
        elif key == "force":
            filters.force = value.lower() in ("1", "true", "yes")

    return filters


class FilterValidationError(ValueError):
    pass


def filters_to_criteria(filters: ParsedFilters, *, default_days: int = 180) -> SearchCriteria:
    """Convert parsed filters to search criteria with validation."""
    if not filters.artist and not filters.genre:
        raise FilterValidationError("Provide artist:\"...\" and/or genre:\"...\".")

    if filters.area and filters.area not in SUPPORTED_AREAS:
        raise FilterValidationError(
            f"Unknown area {filters.area!r}. Supported: {', '.join(sorted(SUPPORTED_AREAS))}"
        )

    area = filters.area
    if normalize_area_name(area or "") == "nationwide":
        if filters.genre and not filters.artist and not filters.force:
            raise FilterValidationError(
                "Broad nationwide genre watches are too noisy. "
                "Add-item artist:\"...\" or add force:true to override."
            )

    if not area and not filters.city and filters.genre and not filters.artist:
        raise FilterValidationError(
            "Broad genre watches require area:\"...\" or city:\"...\"."
        )

    return SearchCriteria(
        artist_query=filters.artist,
        genre=filters.genre,
        area=area,
        city=filters.city,
        state=filters.state,
        radius_miles=filters.radius,
        days_forward=filters.days or default_days,
    )


def parse_and_validate(query: str) -> SearchCriteria:
    return filters_to_criteria(parse_filter_string(query))


def _has_any_filter_input(args: dict) -> bool:
    if (args.get("query") or "").strip():
        return True
    for key in ("artist", "genre", "area", "city", "state", "radius", "days"):
        value = args.get(key)
        if value is not None and value != "":
            return True
    if args.get("force"):
        return True
    return False


def merge_filters_from_args(args: dict) -> ParsedFilters:
    """
    Merge freeform query filters with typed Discord options.

    Typed fields override conflicting values from the freeform query string.
    """
    query = (args.get("query") or "").strip()
    filters = parse_filter_string(query) if query else ParsedFilters()

    artist = args.get("artist")
    if artist is not None and str(artist).strip():
        filters.artist = str(artist).strip()

    genre = args.get("genre")
    if genre is not None and str(genre).strip():
        filters.genre = str(genre).strip()

    area = args.get("area")
    if area is not None and str(area).strip():
        filters.area = normalize_area_name(str(area))

    city = args.get("city")
    if city is not None and str(city).strip():
        filters.city = str(city).strip()

    state = args.get("state")
    if state is not None and str(state).strip():
        filters.state = str(state).strip().upper()

    radius = args.get("radius")
    if radius is not None:
        filters.radius = int(radius)

    days = args.get("days")
    if days is not None:
        filters.days = int(days)

    if args.get("force"):
        filters.force = True

    return filters


def criteria_from_args(args: dict, *, default_days: int = 180) -> SearchCriteria:
    """Build validated SearchCriteria from typed options and/or freeform query."""
    if not _has_any_filter_input(args):
        raise FilterValidationError(
            "Provide artist/genre/area/city via typed options or a freeform query string."
        )
    return filters_to_criteria(merge_filters_from_args(args), default_days=default_days)
