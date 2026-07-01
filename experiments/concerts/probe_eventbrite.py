#!/usr/bin/env python3
"""
Eventbrite concerts probe (read-only).

Primary path: public browse pages with schema.org ItemList JSON-LD (no API key).
Optional path: Eventbrite API v3 when EVENTBRITE_TOKEN is set.

Usage:
    python experiments/concerts/probe_eventbrite.py --city Houston --state TX
    python experiments/concerts/probe_eventbrite.py --artist "Breaking Benjamin" --city Houston
    python experiments/concerts/probe_eventbrite.py --genre rock --city Austin --state TX
"""

from __future__ import annotations

import json
import logging
import re
from datetime import date
from typing import Any, Optional
from urllib.parse import urlencode

from bs4 import BeautifulSoup

from probe_common import (
    BROWSER_HEADERS,
    NormalizedEvent,
    artist_slug,
    build_normalized_event,
    city_state_slug,
    extract_eventbrite_id,
    genre_browse_slug,
    handle_probe_main,
    http_get_json,
    http_get_text,
    optional_env,
    save_artifact,
    setup_logging,
)

SOURCE = "eventbrite"
ENV_TOKEN = "EVENTBRITE_TOKEN"
API_SEARCH_URL = "https://www.eventbriteapi.com/v3/events/search/"


def _parse_ld_json_events(html: str) -> list[dict[str, Any]]:
    soup = BeautifulSoup(html, "html.parser")
    events: list[dict[str, Any]] = []
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            payload = json.loads(script.string or "")
        except (TypeError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict) and payload.get("@type") == "ItemList":
            for entry in payload.get("itemListElement") or []:
                item = entry.get("item") if isinstance(entry, dict) else None
                if isinstance(item, dict) and item.get("@type") == "Event":
                    events.append(item)
    return events


def normalize_eventbrite_ld(item: dict[str, Any], *, genre: str = "") -> NormalizedEvent:
    location = item.get("location") or {}
    address = location.get("address") or {}
    ticket_url = str(item.get("url") or "")
    return build_normalized_event(
        source=SOURCE,
        provider_event_id=extract_eventbrite_id(ticket_url),
        artist_or_title=str(item.get("name") or ""),
        venue=str(location.get("name") or ""),
        city=str(address.get("addressLocality") or ""),
        state=str(address.get("addressRegion") or ""),
        starts_at=str(item.get("startDate") or ""),
        ticket_url=ticket_url,
        genre_or_classification=genre,
        raw_url=ticket_url,
    )


def normalize_eventbrite_api(item: dict[str, Any]) -> NormalizedEvent:
    venue = item.get("venue") or {}
    ticket_url = str(item.get("url") or "")
    category = ""
    if isinstance(item.get("category"), dict):
        category = str(item["category"].get("name") or "")
    return build_normalized_event(
        source=SOURCE,
        provider_event_id=str(item.get("id") or extract_eventbrite_id(ticket_url)),
        artist_or_title=str(item.get("name", {}).get("text") if isinstance(item.get("name"), dict) else item.get("name") or ""),
        venue=str(venue.get("name") or ""),
        city=str((venue.get("address") or {}).get("city") or ""),
        state=str((venue.get("address") or {}).get("region") or ""),
        starts_at=str(item.get("start") or {}).get("utc") if isinstance(item.get("start"), dict) else str(item.get("start") or ""),
        ticket_url=ticket_url,
        genre_or_classification=category,
        raw_url=ticket_url,
    )


def _browse_url(args: Any, start: date, end: date) -> tuple[str, str]:
    region_slug = city_state_slug(args.city, args.state or "TX")
    if not region_slug:
        raise ValueError("Eventbrite browse probe requires --city and --state")

    date_q = urlencode({"start_date": start.isoformat(), "end_date": end.isoformat()})
    genre_slug = genre_browse_slug(args.genre)
    if args.artist:
        path = f"https://www.eventbrite.com/d/{region_slug}/{artist_slug(args.artist)}/"
        return f"{path}?{date_q}", args.genre or ""
    if genre_slug:
        return f"https://www.eventbrite.com/b/{region_slug}/{genre_slug}/?{date_q}", args.genre or ""
    return f"https://www.eventbrite.com/b/{region_slug}/music/?{date_q}", "music"


def _filter_events(
    events: list[NormalizedEvent],
    *,
    artist: Optional[str],
    limit: int,
) -> list[NormalizedEvent]:
    if not artist:
        return events[:limit]
    needle = artist.lower()
    filtered = [
        event
        for event in events
        if needle in event.artist_or_title.lower()
    ]
    return (filtered or events)[:limit]


def _probe_api(args: Any, start: date, end: date, token: str, log: logging.Logger):
    headers = {
        **BROWSER_HEADERS,
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    }
    params = {
        "start_date.range_start": f"{start.isoformat()}T00:00:00Z",
        "start_date.range_end": f"{end.isoformat()}T23:59:59Z",
        "expand": "venue,category",
        "page_size": min(max(args.limit, 1), 50),
    }
    if args.city and args.state:
        params["location.address"] = f"{args.city}, {args.state}"
    if args.artist:
        params["q"] = args.artist
    log.info("GET %s", API_SEARCH_URL)
    status, data, raw_text = http_get_json(API_SEARCH_URL, params=params, headers=headers)
    if status != 200 or not isinstance(data, dict):
        raise RuntimeError(f"Eventbrite API returned HTTP {status}: {raw_text[:300]}")
    events_raw = data.get("events") or []
    normalized = [normalize_eventbrite_api(item) for item in events_raw if isinstance(item, dict)]
    return normalized, {"mode": "api", "request": params, "events_sample": events_raw[:3]}


def _probe_browse(args: Any, start: date, end: date, log: logging.Logger):
    url, genre_label = _browse_url(args, start, end)
    log.info("GET %s", url)
    response = http_get_text(url, headers=BROWSER_HEADERS)
    if response.status_code != 200:
        raise RuntimeError(f"Eventbrite browse returned HTTP {response.status_code}")

    ld_events = _parse_ld_json_events(response.text)
    normalized = [normalize_eventbrite_ld(item, genre=genre_label) for item in ld_events]
    normalized = _filter_events(normalized, artist=args.artist, limit=args.limit)
    sample = {
        "mode": "browse_ld_json",
        "request_url": url,
        "ld_json_event_count": len(ld_events),
        "events_sample": ld_events[:3],
        "html_bytes": len(response.text),
    }
    return normalized, sample


def run_probe(args: Any, start: date, end: date, log: logging.Logger):
    token = optional_env(ENV_TOKEN)
    if token:
        normalized, sample = _probe_api(args, start, end, token, log)
        notes = [
            f"date_window={start.isoformat()}..{end.isoformat()}",
            "authenticated Eventbrite API v3",
        ]
    else:
        normalized, sample = _probe_browse(args, start, end, log)
        notes = [
            f"date_window={start.isoformat()}..{end.isoformat()}",
            "public browse JSON-LD (no EVENTBRITE_TOKEN)",
            "artist keyword matching is title-substring only on browse pages",
        ]

    artifact = save_artifact(
        SOURCE,
        args.artifact_label,
        sample,
        events=normalized,
        meta={"result_count": len(normalized)},
    )
    return normalized, artifact, notes


def main() -> int:
    return handle_probe_main(setup_logging("probe_eventbrite"), source=SOURCE, run_probe=run_probe)


if __name__ == "__main__":
    raise SystemExit(main())
