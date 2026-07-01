#!/usr/bin/env python3
"""
Bandsintown REST API probe (read-only).

Requires BANDSINTOWN_APP_ID (https://www.bandsintown.com/api/overview).

Usage:
    python experiments/concerts/probe_bandsintown.py --artist "Breaking Benjamin"
    python experiments/concerts/probe_bandsintown.py --artist "Disturbed" --city Houston --state TX
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Any, Optional
from urllib.parse import quote

from probe_common import (
    BROWSER_HEADERS,
    MissingCredentialError,
    NormalizedEvent,
    build_normalized_event,
    handle_probe_main,
    http_get_json,
    require_env,
    save_artifact,
    setup_logging,
)

SOURCE = "bandsintown"
ENV_APP_ID = "BANDSINTOWN_APP_ID"


def normalize_bandsintown_event(raw: dict[str, Any], *, artist: str) -> NormalizedEvent:
    venue = raw.get("venue") or {}
    city = str(venue.get("city") or "")
    state = str(venue.get("region") or venue.get("state") or "")
    starts_at = str(raw.get("datetime") or raw.get("starts_at") or "")
    ticket_url = ""
    offers = raw.get("offers") or []
    if offers and isinstance(offers[0], dict):
        ticket_url = str(offers[0].get("url") or "")
    if not ticket_url:
        ticket_url = str(raw.get("url") or "")

    event_id = str(raw.get("id") or raw.get("event_id") or "")
    if not event_id and ticket_url:
        event_id = ticket_url.rstrip("/").split("/")[-1]

    return build_normalized_event(
        source=SOURCE,
        provider_event_id=event_id,
        artist_or_title=str(raw.get("artist_name") or raw.get("title") or artist),
        venue=str(venue.get("name") or ""),
        city=city,
        state=state,
        starts_at=starts_at,
        ticket_url=ticket_url,
        genre_or_classification="",
        raw_url=ticket_url,
    )


def _in_date_window(starts_at: str, start: date, end: date) -> bool:
    if not starts_at:
        return True
    try:
        parsed = datetime.fromisoformat(starts_at.replace("Z", "+00:00"))
        event_date = parsed.date()
    except ValueError:
        return True
    return start <= event_date <= end


def _matches_city_state(event: NormalizedEvent, city: Optional[str], state: Optional[str]) -> bool:
    if city and city.strip().lower() not in event.city.lower():
        return False
    if state and state.strip().upper() != event.state.upper():
        return False
    return True


def run_probe(args: Any, start: date, end: date, log: logging.Logger):
    if not args.artist:
        raise ValueError("Bandsintown probe requires --artist (artist-scoped API).")

    app_id = require_env(
        ENV_APP_ID,
        hint="Request an app_id from https://www.bandsintown.com/api/overview",
    )
    artist_encoded = quote(args.artist.strip())
    url = f"https://rest.bandsintown.com/artists/{artist_encoded}/events"
    params = {"app_id": app_id}
    log.info("GET %s", url)

    status, data, raw_text = http_get_json(url, params=params, headers=BROWSER_HEADERS)
    if status == 401:
        raise MissingCredentialError(ENV_APP_ID, raw_text[:200])
    if status != 200:
        raise RuntimeError(f"Bandsintown API returned HTTP {status}: {raw_text[:300]}")
    if not isinstance(data, list):
        raise RuntimeError(f"Unexpected Bandsintown payload type: {type(data).__name__}")

    normalized: list[NormalizedEvent] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        event = normalize_bandsintown_event(item, artist=args.artist)
        if not _in_date_window(event.starts_at, start, end):
            continue
        if not _matches_city_state(event, args.city, args.state):
            continue
        normalized.append(event)
        if len(normalized) >= args.limit:
            break

    sample = {
        "artist": args.artist,
        "events_sample": data[:3],
        "raw_count": len(data),
    }
    artifact = save_artifact(
        SOURCE,
        args.artifact_label,
        sample,
        events=normalized,
        meta={"http_status": status, "result_count": len(normalized)},
    )
    notes = [
        f"date_window={start.isoformat()}..{end.isoformat()} (client-side filter)",
        "artist-scoped only; no metro/genre search; city/state filtered post-fetch",
    ]
    return normalized, artifact, notes


def main() -> int:
    return handle_probe_main(setup_logging("probe_bandsintown"), source=SOURCE, run_probe=run_probe)


if __name__ == "__main__":
    raise SystemExit(main())
