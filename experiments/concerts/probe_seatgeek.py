#!/usr/bin/env python3
"""
SeatGeek API probe (read-only).

Requires SEATGEEK_CLIENT_ID (https://seatgeek.com/account/develop).

SeatGeek artist discovery requires a two-step flow for reliable results:
  1. GET /2/performers?q={artist}&taxonomies.name=concert
  2. GET /2/events?performers.id={id} (+ optional venue.city/state)

Broad rock watches should use taxonomies.name=concert (not rock alone).

Usage:
    python experiments/concerts/probe_seatgeek.py --city Houston --state TX --days 365
    python experiments/concerts/probe_seatgeek.py --artist "Shinedown" --days 365
    python experiments/concerts/probe_seatgeek.py --genre rock --city Dallas --state TX
    python experiments/concerts/probe_seatgeek.py --experiment --days 365
"""

from __future__ import annotations

import argparse
import json
import logging
from datetime import date, datetime, time, timezone
from typing import Any, Optional
from urllib.parse import urlencode

from probe_common import (
    BROWSER_HEADERS,
    MissingCredentialError,
    NormalizedEvent,
    add_common_cli_args,
    build_normalized_event,
    count_by_taxonomy_token,
    http_get_json,
    print_probe_summary,
    require_env,
    resolve_date_window,
    save_artifact,
    setup_logging,
)

SOURCE = "seatgeek"
EVENTS_URL = "https://api.seatgeek.com/2/events"
PERFORMERS_URL = "https://api.seatgeek.com/2/performers"
ENV_CLIENT_ID = "SEATGEEK_CLIENT_ID"

EXPERIMENT_ARTISTS = [
    "Breaking Benjamin",
    "Shinedown",
    "Disturbed",
    "Three Days Grace",
    "Papa Roach",
]
EXPERIMENT_CITIES = [
    ("Houston", "TX"),
    ("Dallas", "TX"),
    ("Austin", "TX"),
    ("San Antonio", "TX"),
]

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


def _fetch_events(
    client_id: str,
    params: dict[str, Any],
    log: logging.Logger,
) -> tuple[str, dict[str, Any], list[dict[str, Any]], int]:
    redacted = {k: v for k, v in params.items() if k != "client_id"}
    log.info("GET %s?%s", EVENTS_URL, urlencode(redacted))
    status, data, raw_text = http_get_json(EVENTS_URL, params=params, headers=BROWSER_HEADERS)
    if status == 403 and "Client is required" in raw_text:
        raise MissingCredentialError(ENV_CLIENT_ID, raw_text[:200])
    if status == 401:
        raise MissingCredentialError(ENV_CLIENT_ID, "Client ID rejected (HTTP 401).")
    if status != 200 or not isinstance(data, dict):
        raise RuntimeError(f"SeatGeek events API returned HTTP {status}: {raw_text[:300]}")
    events = data.get("events") or []
    return "events", data, events, status


def _fetch_performers(
    client_id: str,
    *,
    q: str,
    log: logging.Logger,
    taxonomies_name: str = "concert",
) -> list[dict[str, Any]]:
    params = {
        "client_id": client_id,
        "q": q,
        "per_page": 10,
        "taxonomies.name": taxonomies_name,
    }
    log.info("GET %s?%s", PERFORMERS_URL, urlencode({k: v for k, v in params.items() if k != "client_id"}))
    status, data, raw_text = http_get_json(PERFORMERS_URL, params=params, headers=BROWSER_HEADERS)
    if status != 200 or not isinstance(data, dict):
        log.warning("Performer lookup failed for %r: HTTP %s %s", q, status, raw_text[:120])
        return []
    performers = data.get("performers") or []
    return [p for p in performers if isinstance(p, dict)]


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


def build_query_strategies(
    *,
    artist: Optional[str],
    city: Optional[str],
    state: Optional[str],
    genre: Optional[str],
    performer: Optional[dict[str, Any]],
) -> list[tuple[str, dict[str, Any]]]:
    strategies: list[tuple[str, dict[str, Any]]] = []

    if city:
        city_params: dict[str, Any] = {"venue.city": city}
        if state:
            city_params["venue.state"] = state
        strategies.append(("city_only", dict(city_params)))
        strategies.append(("city_concert_taxonomy", {**city_params, "taxonomies.name": "concert"}))

    if genre:
        genre_key = genre.strip().lower()
        taxonomy = GENRE_TAXONOMIES.get(genre_key, genre)
        genre_params: dict[str, Any] = {"taxonomies.name": taxonomy}
        if city:
            genre_params["venue.city"] = city
        if state:
            genre_params["venue.state"] = state
        strategies.append((f"taxonomy_{taxonomy}", genre_params))
        if taxonomy != "concert":
            concert_params = dict(genre_params)
            concert_params["taxonomies.name"] = "concert"
            strategies.append(("taxonomy_concert_plus_city", concert_params))

    if artist:
        strategies.append(("events_q_artist", {"q": artist}))
        if city:
            strategies.append(
                (
                    "events_q_artist_city",
                    {"q": artist, "venue.city": city, **({"venue.state": state} if state else {})},
                )
            )
        if performer:
            performer_id = performer.get("id")
            performer_slug = performer.get("slug")
            if performer_id is not None:
                id_params: dict[str, Any] = {"performers.id": performer_id}
                if city:
                    id_params["venue.city"] = city
                if state:
                    id_params["venue.state"] = state
                strategies.append(("performers_id", id_params))
                strategies.append(
                    (
                        "performers_id_concert_taxonomy",
                        {**id_params, "taxonomies.name": "concert"},
                    )
                )
            if performer_slug:
                slug_params: dict[str, Any] = {"performers.slug": performer_slug}
                if city:
                    slug_params["venue.city"] = city
                if state:
                    slug_params["venue.state"] = state
                strategies.append(("performers_slug", slug_params))

    deduped: list[tuple[str, dict[str, Any]]] = []
    seen: set[str] = set()
    for name, params in strategies:
        key = urlencode(sorted(params.items()))
        if key in seen:
            continue
        seen.add(key)
        deduped.append((name, params))
    return deduped


def execute_strategy(
    strategy_name: str,
    params: dict[str, Any],
    *,
    client_id: str,
    start: date,
    end: date,
    limit: int,
    log: logging.Logger,
) -> dict[str, Any]:
    query = {**_base_event_params(client_id, start, end, per_page=limit), **params}
    _, data, events_raw, status = _fetch_events(client_id, query, log)
    normalized = [normalize_seatgeek_event(item) for item in events_raw[:limit]]
    meta = data.get("meta") or {}
    total = meta.get("total") if isinstance(meta, dict) else None
    return {
        "strategy": strategy_name,
        "params": {k: v for k, v in query.items() if k != "client_id"},
        "http_status": status,
        "result_count": len(normalized),
        "api_total": total,
        "performer_lookup": None,
        "events_sample": events_raw[:2],
        "normalized_sample": [event.to_dict() for event in normalized[:2]],
    }


def choose_best_strategy(results: list[dict[str, Any]]) -> Optional[dict[str, Any]]:
    if not results:
        return None

    def score(item: dict[str, Any]) -> tuple[int, int, str]:
        count = int(item.get("result_count") or 0)
        api_total = int(item.get("api_total") or 0)
        strategy = str(item.get("strategy") or "")
        # Prefer performer-scoped strategies, then highest counts.
        performer_bonus = 1 if strategy.startswith("performers_") else 0
        return (performer_bonus, count, api_total, strategy)

    ranked = sorted(results, key=score, reverse=True)
    best = ranked[0]
    return best if int(best.get("result_count") or 0) > 0 else None


def run_single_probe(args: Any, start: date, end: date, log: logging.Logger):
    client_id = require_env(
        ENV_CLIENT_ID,
        hint='Create a free client at https://seatgeek.com/account/develop',
    )

    performer: Optional[dict[str, Any]] = None
    performer_lookup: Optional[dict[str, Any]] = None
    if args.artist:
        performers = _fetch_performers(client_id, q=args.artist, log=log)
        performer = _pick_performer(performers, args.artist)
        performer_lookup = {
            "query": args.artist,
            "candidates": [
                {
                    "id": p.get("id"),
                    "name": p.get("name"),
                    "slug": p.get("slug"),
                    "taxonomies": p.get("taxonomies"),
                }
                for p in performers[:5]
            ],
            "selected": {
                "id": performer.get("id") if performer else None,
                "name": performer.get("name") if performer else None,
                "slug": performer.get("slug") if performer else None,
            },
        }

    strategies = build_query_strategies(
        artist=args.artist,
        city=args.city,
        state=args.state,
        genre=args.genre,
        performer=performer,
    )
    if not strategies:
        raise ValueError("SeatGeek probe requires --city and/or --artist and/or --genre")

    strategy_results = [
        execute_strategy(
            name,
            params,
            client_id=client_id,
            start=start,
            end=end,
            limit=args.limit,
            log=log,
        )
        for name, params in strategies
    ]
    for result in strategy_results:
        result["performer_lookup"] = performer_lookup

    best = choose_best_strategy(strategy_results)
    if best is None:
        best = strategy_results[0]

    # Re-fetch best strategy to produce normalized output list.
    best_params = best["params"]
    query = {**_base_event_params(client_id, start, end, per_page=args.limit), **best_params}
    _, data, events_raw, status = _fetch_events(client_id, query, log)
    normalized = [normalize_seatgeek_event(item) for item in events_raw[: args.limit]]

    sample = {
        "mode": "single",
        "selected_strategy": best.get("strategy"),
        "performer_lookup": performer_lookup,
        "strategy_results": strategy_results,
        "request": best_params,
        "meta": data.get("meta"),
        "events_sample": events_raw[:3],
    }
    artifact = save_artifact(
        SOURCE,
        args.artifact_label,
        sample,
        events=normalized,
        meta={
            "http_status": status,
            "result_count": len(normalized),
            "selected_strategy": best.get("strategy"),
            "strategy_count": len(strategy_results),
        },
    )
    notes = [
        f"date_window={start.isoformat()}..{end.isoformat()}",
        f"selected_strategy={best.get('strategy')}",
        f"strategies_tried={len(strategy_results)}",
        "artist watches should use performers.id/slug after /2/performers lookup",
        "broad watches should use taxonomies.name=concert and filter sports/comedy/theater",
    ]
    return normalized, artifact, notes


def run_experiment(args: Any, start: date, end: date, log: logging.Logger):
    client_id = require_env(ENV_CLIENT_ID)
    rows: list[dict[str, Any]] = []

    for artist in EXPERIMENT_ARTISTS:
        performers = _fetch_performers(client_id, q=artist, log=log)
        performer = _pick_performer(performers, artist)
        artist_strategies = build_query_strategies(
            artist=artist,
            city=None,
            state=None,
            genre=None,
            performer=performer,
        )
        for strategy_name, params in artist_strategies:
            if not strategy_name.startswith("performers_") and strategy_name != "events_q_artist":
                continue
            result = execute_strategy(
                strategy_name,
                params,
                client_id=client_id,
                start=start,
                end=end,
                limit=args.limit,
                log=log,
            )
            result["artist"] = artist
            result["city"] = None
            result["performer_lookup"] = {
                "selected": {
                    "id": performer.get("id") if performer else None,
                    "slug": performer.get("slug") if performer else None,
                    "name": performer.get("name") if performer else None,
                }
            }
            rows.append(result)

    for city, state in EXPERIMENT_CITIES:
        for strategy_name, params in (
            ("city_only", {"venue.city": city, "venue.state": state}),
            ("city_concert_taxonomy", {"venue.city": city, "venue.state": state, "taxonomies.name": "concert"}),
            ("city_concert_rock", {"venue.city": city, "venue.state": state, "taxonomies.name": "concert", "q": "rock"}),
        ):
            result = execute_strategy(
                strategy_name,
                params,
                client_id=client_id,
                start=start,
                end=end,
                limit=args.limit,
                log=log,
            )
            result["artist"] = None
            result["city"] = city
            rows.append(result)

    normalized: list[NormalizedEvent] = []
    for row in rows:
        if int(row.get("result_count") or 0) <= 0:
            continue
        for item in row.get("normalized_sample") or []:
            normalized.append(
                NormalizedEvent(
                    source=SOURCE,
                    provider_event_id=str(item.get("provider_event_id") or ""),
                    artist_or_title=str(item.get("artist_or_title") or ""),
                    venue=str(item.get("venue") or ""),
                    city=str(item.get("city") or ""),
                    state=str(item.get("state") or ""),
                    starts_at=str(item.get("starts_at") or ""),
                    ticket_url=str(item.get("ticket_url") or ""),
                    genre_or_classification=str(item.get("genre_or_classification") or ""),
                    raw_url=str(item.get("raw_url") or ""),
                    dedupe_key=str(item.get("dedupe_key") or ""),
                    event_dedupe_key=str(item.get("event_dedupe_key") or ""),
                )
            )

    payload = {
        "mode": "experiment",
        "date_window": {"start": start.isoformat(), "end": end.isoformat()},
        "artists": EXPERIMENT_ARTISTS,
        "cities": [{"city": c, "state": s} for c, s in EXPERIMENT_CITIES],
        "rows": rows,
        "summary": {
            "queries": len(rows),
            "successful_queries": sum(1 for row in rows if int(row.get("result_count") or 0) > 0),
            "failed_queries": sum(1 for row in rows if int(row.get("result_count") or 0) == 0),
        },
    }
    artifact = save_artifact(
        SOURCE,
        args.artifact_label or "experiment",
        payload,
        events=normalized,
        meta=payload["summary"],
    )
    notes = [
        f"experiment_queries={len(rows)}",
        f"experiment_successful={payload['summary']['successful_queries']}",
        "see payload.rows for per-strategy counts and params",
    ]
    return normalized, artifact, notes


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Read-only SeatGeek concerts probe")
    add_common_cli_args(parser)
    parser.add_argument(
        "--experiment",
        action="store_true",
        help="Run the Raven experiment matrix (artists + Texas cities, 365-day window by default)",
    )
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    log = setup_logging("probe_seatgeek")
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.experiment and args.days == 180:
        args.days = 365
    try:
        start, end = resolve_date_window(args.start_date, args.end_date, days=args.days)
        if args.experiment:
            events, artifact_path, notes = run_experiment(args, start, end, log)
        else:
            events, artifact_path, notes = run_single_probe(args, start, end, log)
        print_probe_summary(
            source=SOURCE,
            events=events,
            artifact_path=artifact_path,
            notes=notes,
            json_only=args.json,
        )
        if args.experiment:
            taxonomy_counts = count_by_taxonomy_token(events)
            if taxonomy_counts:
                print("taxonomy_token_counts=")
                print(json.dumps(taxonomy_counts, indent=2, ensure_ascii=False))
        return 0
    except MissingCredentialError as exc:
        log.error(str(exc))
        return 2
    except Exception as exc:
        log.error("%s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
