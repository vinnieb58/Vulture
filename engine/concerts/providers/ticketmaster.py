"""Ticketmaster Discovery API provider for Vulture Concerts."""

from __future__ import annotations

import logging
import os
from datetime import date
from typing import Any, Optional

from engine.concerts.probe_util import (
    BROWSER_HEADERS,
    NormalizedEvent,
    build_normalized_event,
    http_get_json,
    ticketmaster_datetime,
)

from engine.concerts.areas import GeoSearch

log = logging.getLogger(__name__)

SOURCE = "ticketmaster"
API_URL = "https://app.ticketmaster.com/discovery/v2/events.json"
ENV_API_KEY = "TICKETMASTER_API_KEY"

GENRE_CLASSIFICATIONS = {
    "rock": "Rock",
    "hard rock": "Hard Rock",
    "hard-rock": "Hard Rock",
    "metal": "Metal",
    "music": "Music",
}


def _first(values: Any) -> str:
    if isinstance(values, list) and values:
        item = values[0]
        if isinstance(item, dict):
            return str(item.get("name") or item.get("id") or "")
        return str(item)
    if isinstance(values, dict):
        return str(values.get("name") or values.get("id") or "")
    return str(values or "")


def normalize_ticketmaster_event(raw: dict[str, Any]) -> NormalizedEvent:
    venue_block = (raw.get("_embedded") or {}).get("venues") or []
    venue = venue_block[0] if venue_block else {}
    city = ((venue.get("city") or {}).get("name") or "").strip()
    state = ((venue.get("state") or {}).get("stateCode") or "").strip()
    venue_name = (venue.get("name") or "").strip()

    attractions = (raw.get("_embedded") or {}).get("attractions") or []
    artist = _first(attractions) if attractions else (raw.get("name") or "")

    classifications = raw.get("classifications") or []
    genre = ""
    if classifications:
        cls0 = classifications[0]
        genre = _first(cls0.get("genre")) or _first(cls0.get("segment")) or _first(cls0.get("subGenre"))

    dates = raw.get("dates") or {}
    start = ((dates.get("start") or {}).get("dateTime") or (dates.get("start") or {}).get("localDate") or "")

    ticket_url = (raw.get("url") or "").strip()
    event_id = str(raw.get("id") or "").strip()

    return build_normalized_event(
        source=SOURCE,
        provider_event_id=event_id,
        artist_or_title=str(artist or raw.get("name") or ""),
        venue=venue_name,
        city=city,
        state=state,
        starts_at=str(start),
        ticket_url=ticket_url,
        genre_or_classification=genre,
        raw_url=ticket_url,
    )


def _build_params(
    *,
    artist: Optional[str],
    genre: Optional[str],
    geo: Optional[GeoSearch],
    start: date,
    end: date,
    limit: int,
    api_key: str,
) -> dict[str, Any]:
    params: dict[str, Any] = {
        "apikey": api_key,
        "size": min(max(limit, 1), 200),
        "sort": "date,asc",
        "startDateTime": ticketmaster_datetime(start),
        "endDateTime": ticketmaster_datetime(end, end_of_day=True),
    }
    if artist:
        params["keyword"] = artist
    if geo:
        if geo.city:
            params["city"] = geo.city
        if geo.state:
            params["stateCode"] = geo.state
        if geo.lat is not None and geo.lng is not None:
            params["geoPoint"] = f"{geo.lat},{geo.lng}"
            params["radius"] = geo.radius_miles
            params["unit"] = "miles"
    if genre:
        genre_key = genre.strip().lower()
        params["classificationName"] = GENRE_CLASSIFICATIONS.get(genre_key, genre)
    elif not artist and not geo:
        params["classificationName"] = "Music"
    return params


def search_ticketmaster(
    *,
    artist: Optional[str] = None,
    genre: Optional[str] = None,
    geo: Optional[GeoSearch] = None,
    start: date,
    end: date,
    limit: int = 50,
) -> tuple[list[NormalizedEvent], Optional[str]]:
    """
    Query Ticketmaster Discovery API.

    Returns (events, error_message). error_message is set only on failure.
    """
    api_key = os.getenv(ENV_API_KEY, "").strip()
    if not api_key:
        return [], f"{ENV_API_KEY} not configured"

    params = _build_params(
        artist=artist,
        genre=genre,
        geo=geo,
        start=start,
        end=end,
        limit=limit,
        api_key=api_key,
    )
    try:
        status, data, raw_text = http_get_json(API_URL, params=params, headers=BROWSER_HEADERS)
    except Exception as exc:
        log.warning("Ticketmaster request failed: %s", exc)
        return [], str(exc)

    if status == 401:
        return [], "Ticketmaster API key rejected (HTTP 401)"
    if status != 200 or not isinstance(data, dict):
        return [], f"Ticketmaster HTTP {status}: {raw_text[:200]}"

    events_raw = (data.get("_embedded") or {}).get("events") or []
    normalized = [normalize_ticketmaster_event(item) for item in events_raw[:limit]]
    return normalized, None
