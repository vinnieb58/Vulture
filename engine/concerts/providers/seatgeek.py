"""SeatGeek API provider for Vulture Concerts."""

from __future__ import annotations

import logging
import os
from datetime import date, datetime, time, timezone
from typing import Any, Optional
from urllib.parse import urlencode

from engine.concerts.probe_util import (
    BROWSER_HEADERS,
    NormalizedEvent,
    build_normalized_event,
    http_get_json,
)

from engine.concerts.areas import GeoSearch

log = logging.getLogger(__name__)

SOURCE = "seatgeek"
EVENTS_URL = "https://api.seatgeek.com/2/events"
PERFORMERS_URL = "https://api.seatgeek.com/2/performers"
ENV_CLIENT_ID = "SEATGEEK_CLIENT_ID"

GENRE_TAXONOMIES = {
    "rock": "rock",
    "hard rock": "hard-rock",
    "hard-rock": "hard-rock",
    "metal": "metal",
    "concert": "concert",
    "concerts": "concert",
    "music": "concert",
}


def normalize_seatgeek_event(raw: dict[str, Any]) -> NormalizedEvent:
    venue = raw.get("venue") or {}
    performers = raw.get("performers") or []
    primary = next((p for p in performers if p.get("primary")), performers[0] if performers else {})
    artist = (primary or {}).get("name") or raw.get("title") or raw.get("short_title") or ""

    taxonomies = raw.get("taxonomies") or []
    genre = ", ".join(
        sorted({str(t.get("name") or "") for t in taxonomies if isinstance(t, dict) and t.get("name")})
    )

    return build_normalized_event(
        source=SOURCE,
        provider_event_id=str(raw.get("id") or ""),
        artist_or_title=str(artist),
        venue=str(venue.get("name") or ""),
        city=str(venue.get("city") or ""),
        state=str(venue.get("state") or ""),
        starts_at=str(raw.get("datetime_utc") or raw.get("datetime_local") or ""),
        ticket_url=str(raw.get("url") or ""),
        genre_or_classification=genre,
        raw_url=str(raw.get("url") or ""),
    )


def _iso_utc(value: date, *, end_of_day: bool = False) -> str:
    if end_of_day:
        dt = datetime.combine(value, time(23, 59, 59), tzinfo=timezone.utc)
    else:
        dt = datetime.combine(value, time(0, 0, 0), tzinfo=timezone.utc)
    return dt.isoformat().replace("+00:00", "Z")


def _base_event_params(client_id: str, start: date, end: date, *, per_page: int) -> dict[str, Any]:
    return {
        "client_id": client_id,
        "per_page": min(max(per_page, 1), 100),
        "sort": "datetime_utc.asc",
        "datetime_utc.gte": _iso_utc(start),
        "datetime_utc.lte": _iso_utc(end, end_of_day=True),
    }


def _fetch_events(client_id: str, params: dict[str, Any]) -> tuple[list[dict[str, Any]], Optional[str]]:
    try:
        status, data, raw_text = http_get_json(EVENTS_URL, params=params, headers=BROWSER_HEADERS)
    except Exception as exc:
        return [], str(exc)
    if status != 200 or not isinstance(data, dict):
        return [], f"SeatGeek HTTP {status}: {raw_text[:200]}"
    return data.get("events") or [], None


def _fetch_performers(client_id: str, *, q: str) -> list[dict[str, Any]]:
    params = {
        "client_id": client_id,
        "q": q,
        "per_page": 10,
        "taxonomies.name": "concert",
    }
    try:
        status, data, _ = http_get_json(PERFORMERS_URL, params=params, headers=BROWSER_HEADERS)
    except Exception:
        return []
    if status != 200 or not isinstance(data, dict):
        return []
    return [p for p in (data.get("performers") or []) if isinstance(p, dict)]


def _pick_performer(performers: list[dict[str, Any]], artist: str) -> Optional[dict[str, Any]]:
    if not performers:
        return None
    needle = artist.strip().lower()
    for performer in performers:
        if str(performer.get("name") or "").strip().lower() == needle:
            return performer
    for performer in performers:
        if needle in str(performer.get("name") or "").strip().lower():
            return performer
    return performers[0]


def _build_strategies(
    *,
    artist: Optional[str],
    genre: Optional[str],
    geo: Optional[GeoSearch],
    performer: Optional[dict[str, Any]],
) -> list[tuple[str, dict[str, Any]]]:
    strategies: list[tuple[str, dict[str, Any]]] = []

    if geo and geo.city:
        city_params: dict[str, Any] = {"venue.city": geo.city}
        if geo.state:
            city_params["venue.state"] = geo.state
        strategies.append(("city_concert_taxonomy", {**city_params, "taxonomies.name": "concert"}))

    if genre:
        genre_key = genre.strip().lower()
        taxonomy = GENRE_TAXONOMIES.get(genre_key, "concert")
        genre_params: dict[str, Any] = {"taxonomies.name": taxonomy}
        if geo and geo.city:
            genre_params["venue.city"] = geo.city
        if geo and geo.state:
            genre_params["venue.state"] = geo.state
        strategies.append((f"taxonomy_{taxonomy}", genre_params))

    if artist:
        if performer and performer.get("id") is not None:
            id_params: dict[str, Any] = {"performers.id": performer["id"]}
            if geo and geo.city:
                id_params["venue.city"] = geo.city
            if geo and geo.state:
                id_params["venue.state"] = geo.state
            strategies.append(("performers_id", id_params))
        strategies.append(("events_q_artist", {"q": artist}))

    deduped: list[tuple[str, dict[str, Any]]] = []
    seen: set[str] = set()
    for name, params in strategies:
        key = urlencode(sorted(params.items()))
        if key in seen:
            continue
        seen.add(key)
        deduped.append((name, params))
    return deduped


def search_seatgeek(
    *,
    artist: Optional[str] = None,
    genre: Optional[str] = None,
    geo: Optional[GeoSearch] = None,
    start: date,
    end: date,
    limit: int = 50,
) -> tuple[list[NormalizedEvent], Optional[str]]:
    """
    Query SeatGeek API using performer-ID workflow when artist is set.

    Returns (events, error_message).
    """
    client_id = os.getenv(ENV_CLIENT_ID, "").strip()
    if not client_id:
        return [], f"{ENV_CLIENT_ID} not configured"

    performer: Optional[dict[str, Any]] = None
    if artist:
        performers = _fetch_performers(client_id, q=artist)
        performer = _pick_performer(performers, artist)

    strategies = _build_strategies(
        artist=artist,
        genre=genre,
        geo=geo,
        performer=performer,
    )
    if not strategies:
        if artist:
            strategies = [("events_q_artist", {"q": artist})]
        elif geo and geo.city:
            strategies = [
                (
                    "city_concert_taxonomy",
                    {
                        "venue.city": geo.city,
                        **({"venue.state": geo.state} if geo.state else {}),
                        "taxonomies.name": "concert",
                    },
                )
            ]
        else:
            return [], "SeatGeek requires artist or geo scope"

    best_events: list[dict[str, Any]] = []
    last_error: Optional[str] = None
    for _name, params in strategies:
        query = {**_base_event_params(client_id, start, end, per_page=limit), **params}
        events_raw, err = _fetch_events(client_id, query)
        if err:
            last_error = err
            continue
        if len(events_raw) > len(best_events):
            best_events = events_raw
            last_error = None
        if best_events:
            break

    if not best_events and last_error:
        return [], last_error

    normalized = [normalize_seatgeek_event(item) for item in best_events[:limit]]
    return normalized, None
