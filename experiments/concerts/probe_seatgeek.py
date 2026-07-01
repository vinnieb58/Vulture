#!/usr/bin/env python3
"""
SeatGeek API probe (read-only).

Requires SEATGEEK_CLIENT_ID (https://seatgeek.com/account/develop).

Usage:
    python experiments/concerts/probe_seatgeek.py --city Houston --state TX
    python experiments/concerts/probe_seatgeek.py --artist "Shinedown"
    python experiments/concerts/probe_seatgeek.py --genre rock --city Dallas --state TX
"""

from __future__ import annotations

import logging
from datetime import date, datetime, time, timezone
from typing import Any
from urllib.parse import urlencode

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

SOURCE = "seatgeek"
API_URL = "https://api.seatgeek.com/2/events"
ENV_CLIENT_ID = "SEATGEEK_CLIENT_ID"

GENRE_TAXONOMIES = {
    "rock": "rock",
    "hard rock": "hard-rock",
    "hard-rock": "hard-rock",
    "metal": "metal",
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


def build_params(args: Any, start: date, end: date, client_id: str) -> dict[str, Any]:
    params: dict[str, Any] = {
        "client_id": client_id,
        "per_page": min(max(args.limit, 1), 100),
        "sort": "datetime_utc.asc",
        "datetime_utc.gte": _iso_utc(start),
        "datetime_utc.lte": _iso_utc(end, end_of_day=True),
    }
    if args.artist:
        params["q"] = args.artist
    if args.city:
        params["venue.city"] = args.city
    if args.state:
        params["venue.state"] = args.state
    if args.genre:
        key = args.genre.strip().lower()
        params["taxonomies.name"] = GENRE_TAXONOMIES.get(key, args.genre)
    return params


def run_probe(args: Any, start: date, end: date, log: logging.Logger):
    client_id = require_env(
        ENV_CLIENT_ID,
        hint='Create a free client at https://seatgeek.com/account/develop',
    )
    params = build_params(args, start, end, client_id)
    log.info("GET %s?%s", API_URL, urlencode({k: v for k, v in params.items() if k != "client_id"}))

    status, data, raw_text = http_get_json(API_URL, params=params, headers=BROWSER_HEADERS)
    if status == 403 and "Client is required" in raw_text:
        raise MissingCredentialError(ENV_CLIENT_ID, raw_text[:200])
    if status == 401:
        raise MissingCredentialError(ENV_CLIENT_ID, "Client ID rejected (HTTP 401).")
    if status != 200 or not isinstance(data, dict):
        raise RuntimeError(f"SeatGeek API returned HTTP {status}: {raw_text[:300]}")

    events_raw = data.get("events") or []
    normalized = [normalize_seatgeek_event(item) for item in events_raw[: args.limit]]

    sample = {
        "request": {k: v for k, v in params.items() if k != "client_id"},
        "meta": data.get("meta"),
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
        "supports q, venue.city/state, taxonomies.name, datetime_utc range, stable numeric id",
    ]
    return normalized, artifact, notes


def main() -> int:
    return handle_probe_main(setup_logging("probe_seatgeek"), source=SOURCE, run_probe=run_probe)


if __name__ == "__main__":
    raise SystemExit(main())
