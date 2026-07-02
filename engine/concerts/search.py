"""Multi-provider concert search orchestration."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Optional

from engine.concerts.probe_util import NormalizedEvent, resolve_date_window

from engine.concerts.areas import GeoSearch, normalize_area_name, resolve_geo_searches
from engine.concerts.dedupe import MergedConcertEvent, merge_events
from engine.concerts.filters import passes_genre_filter
from engine.concerts.providers.seatgeek import search_seatgeek
from engine.concerts.providers.ticketmaster import search_ticketmaster
from engine.concerts.ranking import is_seatgeek_generic_noise, rank_merged_events
from engine.concerts.stats import SearchStats

log = logging.getLogger(__name__)

DEFAULT_DISPLAY_LIMIT = 10


@dataclass
class SearchCriteria:
    artist_query: Optional[str] = None
    genre: Optional[str] = None
    area: Optional[str] = None
    city: Optional[str] = None
    state: Optional[str] = None
    radius_miles: Optional[int] = None
    days_forward: int = 180
    limit_per_query: int = 50


@dataclass
class SearchResult:
    events: list[MergedConcertEvent] = field(default_factory=list)
    provider_notes: list[str] = field(default_factory=list)
    queries_run: int = 0
    stats: SearchStats = field(default_factory=SearchStats)


def _date_window(days_forward: int) -> tuple[date, date]:
    return resolve_date_window(None, None, days=days_forward)


def _is_nationwide(criteria: SearchCriteria) -> bool:
    return bool(criteria.area and normalize_area_name(criteria.area) == "nationwide")


def search_concerts(criteria: SearchCriteria) -> SearchResult:
    """
    Run Ticketmaster (primary) and SeatGeek (secondary) searches.

    Provider failures are logged but do not fail the whole search.
    """
    start, end = _date_window(criteria.days_forward)
    result = SearchResult()
    stats = SearchStats()

    if _is_nationwide(criteria):
        if not criteria.artist_query:
            result.provider_notes.append(
                "Nationwide search requires an artist; broad genre-only nationwide is not supported."
            )
            return result
        geo_searches: list[Optional[GeoSearch]] = [None]
    else:
        try:
            geo_searches = resolve_geo_searches(
                area=criteria.area,
                city=criteria.city,
                state=criteria.state,
                radius_miles=criteria.radius_miles,
            )
        except ValueError as exc:
            result.provider_notes.append(str(exc))
            return result

    tm_raw: list[NormalizedEvent] = []
    sg_raw: list[NormalizedEvent] = []

    for geo in geo_searches:
        result.queries_run += 1
        tm_events, tm_err = search_ticketmaster(
            artist=criteria.artist_query,
            genre=criteria.genre,
            geo=geo,
            start=start,
            end=end,
            limit=criteria.limit_per_query,
        )
        if tm_err:
            label = geo.label if geo else "nationwide"
            result.provider_notes.append(f"Ticketmaster ({label}): {tm_err}")
        else:
            tm_raw.extend(tm_events)

        result.queries_run += 1
        sg_events, sg_err = search_seatgeek(
            artist=criteria.artist_query,
            genre=criteria.genre,
            geo=geo,
            start=start,
            end=end,
            limit=criteria.limit_per_query,
        )
        if sg_err:
            label = geo.label if geo else "nationwide"
            result.provider_notes.append(f"SeatGeek ({label}): {sg_err}")
        else:
            sg_raw.extend(sg_events)

    stats.ticketmaster_returned = len(tm_raw)
    stats.seatgeek_returned = len(sg_raw)
    raw_events = tm_raw + sg_raw

    genre_filtered = [
        e
        for e in raw_events
        if passes_genre_filter(
            e,
            genre=criteria.genre,
            artist_query=criteria.artist_query,
        )
    ]
    stats.after_genre_filter = len(genre_filtered)

    cleaned: list[NormalizedEvent] = []
    for event in genre_filtered:
        if is_seatgeek_generic_noise(
            event,
            genre=criteria.genre,
            artist_query=criteria.artist_query,
        ):
            stats.noise_hidden += 1
            continue
        cleaned.append(event)

    merged = merge_events(cleaned)
    stats.merged_count = len(merged)
    ranked = rank_merged_events(
        merged,
        artist_query=criteria.artist_query,
        genre=criteria.genre,
    )
    result.events = ranked
    stats.displayed_count = min(len(ranked), DEFAULT_DISPLAY_LIMIT)
    result.stats = stats

    stats.log_summary(logger=log)
    return result


def format_starts_at_display(starts_at: str) -> str:
    """Compact human-readable date/time for Discord cards."""
    raw = (starts_at or "").strip()
    if not raw:
        return "TBA"
    if "T" in raw:
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            local = dt.astimezone(timezone.utc)
            return local.strftime("%Y-%m-%d %H:%M UTC")
        except ValueError:
            return raw[:16].replace("T", " ")
    return raw[:10]
