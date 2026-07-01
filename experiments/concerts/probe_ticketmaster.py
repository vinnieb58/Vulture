#!/usr/bin/env python3
"""
Ticketmaster Discovery API probe (read-only).

Requires TICKETMASTER_API_KEY (https://developer.ticketmaster.com/).

Usage:
    python experiments/concerts/probe_ticketmaster.py --city Houston --state TX
    python experiments/concerts/probe_ticketmaster.py --artist "Breaking Benjamin"
    python experiments/concerts/probe_ticketmaster.py --genre rock --city Austin --state TX
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Any, Optional
from urllib.parse import urlencode

from probe_common import (
    BROWSER_HEADERS,
    MissingCredentialError,
    NormalizedEvent,
    build_normalized_event,
    handle_probe_main,
    http_get_json,
    optional_env,
    require_env,
    save_artifact,
    setup_logging,
    ticketmaster_datetime,
)

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


def build_params(args: Any, start: date, end: date, api_key: str) -> dict[str, Any]:
    params: dict[str, Any] = {
        "apikey": api_key,
        "size": min(max(args.limit, 1), 200),
        "sort": "date,asc",
        "startDateTime": ticketmaster_datetime(start),
        "endDateTime": ticketmaster_datetime(end, end_of_day=True),
    }
    if args.artist:
        params["keyword"] = args.artist
    if args.city:
        params["city"] = args.city
    if args.state:
        params["stateCode"] = args.state
    if args.genre:
        genre_key = args.genre.strip().lower()
        if genre_key in GENRE_CLASSIFICATIONS:
            params["classificationName"] = GENRE_CLASSIFICATIONS[genre_key]
        else:
            params["classificationName"] = args.genre
    if not args.city and not args.artist:
        params["classificationName"] = params.get("classificationName", "Music")
    return params


def run_probe(args: Any, start: date, end: date, log: logging.Logger):
    api_key = require_env(
        ENV_API_KEY,
        hint="Register at https://developer.ticketmaster.com/products-and-docs/apis/discovery-api/v2/",
    )
    params = build_params(args, start, end, api_key)
    log.info("GET %s?%s", API_URL, urlencode({k: v for k, v in params.items() if k != "apikey"}))

    status, data, raw_text = http_get_json(API_URL, params=params, headers=BROWSER_HEADERS)
    if status == 401:
        raise MissingCredentialError(ENV_API_KEY, "API key rejected (HTTP 401).")
    if status != 200 or not isinstance(data, dict):
        raise RuntimeError(f"Ticketmaster Discovery API returned HTTP {status}: {raw_text[:300]}")

    embedded = data.get("_embedded") or {}
    events_raw = embedded.get("events") or []
    normalized = [normalize_ticketmaster_event(item) for item in events_raw[: args.limit]]

    sample = {
        "request": {k: v for k, v in params.items() if k != "apikey"},
        "page": data.get("page"),
        "total_elements": (data.get("page") or {}).get("totalElements"),
        "events_sample": events_raw[:3],
    }
    artifact = save_artifact(
        SOURCE,
        args.artifact_label,
        sample,
        events=normalized,
        meta={"http_status": status, "result_count": len(normalized)},
    )
    notes = [
        f"date_window={start.isoformat()}..{end.isoformat()}",
        "supports city/state, artist keyword, classificationName, date range, stable event.id",
    ]
    return normalized, artifact, notes


def main() -> int:
    return handle_probe_main(setup_logging("probe_ticketmaster"), source=SOURCE, run_probe=run_probe)


if __name__ == "__main__":
    raise SystemExit(main())
