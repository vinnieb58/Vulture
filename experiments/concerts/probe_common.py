"""Shared helpers for Vulture Concerts source probe scripts (read-only recon)."""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import sys
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote_plus

import requests

PROBE_DIR = Path(__file__).resolve().parent
REPO_ROOT = PROBE_DIR.parents[1]
ARTIFACTS_ROOT = REPO_ROOT / "artifacts" / "concerts"

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

BROWSER_HEADERS = {
    "User-Agent": DEFAULT_USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate",
    "DNT": "1",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}

GENRE_SLUGS = {
    "rock": "music/rock",
    "hard rock": "music/hard-rock",
    "hard-rock": "music/hard-rock",
    "metal": "music/metal",
}

TEXAS_CITY_SLUGS = {
    "houston": "tx--houston",
    "austin": "tx--austin",
    "dallas": "tx--dallas",
}


@dataclass(frozen=True)
class NormalizedEvent:
    source: str
    provider_event_id: str
    artist_or_title: str
    venue: str
    city: str
    state: str
    starts_at: str
    ticket_url: str
    genre_or_classification: str
    raw_url: str
    dedupe_key: str

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


class MissingCredentialError(RuntimeError):
    """Raised when a probe requires an API key that is not configured."""

    def __init__(self, env_var: str, hint: str = "") -> None:
        self.env_var = env_var
        self.hint = hint
        message = (
            f"Missing required credential: set {env_var} in the environment."
            + (f" {hint}" if hint else "")
        )
        super().__init__(message)


def setup_logging(name: str) -> logging.Logger:
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)-8s %(message)s",
        stream=sys.stdout,
        force=True,
    )
    return logging.getLogger(name)


def require_env(name: str, *, hint: str = "") -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise MissingCredentialError(name, hint)
    return value


def optional_env(name: str) -> Optional[str]:
    value = os.getenv(name, "").strip()
    return value or None


def add_common_cli_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--artist", help="Artist or keyword to search for")
    parser.add_argument("--city", help="City name (e.g. Houston)")
    parser.add_argument("--state", default="TX", help="US state code (default: TX)")
    parser.add_argument(
        "--genre",
        help="Genre/classification slug or label (rock, hard rock, metal)",
    )
    parser.add_argument(
        "--start-date",
        dest="start_date",
        help="Inclusive start date YYYY-MM-DD (default: today UTC)",
    )
    parser.add_argument(
        "--end-date",
        dest="end_date",
        help="Inclusive end date YYYY-MM-DD (default: today + 180 days UTC)",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=180,
        help="Date window length when --end-date omitted (default: 180)",
    )
    parser.add_argument(
        "--radius",
        type=int,
        default=50,
        help="Search radius in miles for geo-capable APIs (default: 50)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=20,
        help="Maximum normalized events to return (default: 20)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print only normalized JSON array on stdout",
    )
    parser.add_argument(
        "--artifact-label",
        default="probe",
        help="Suffix for saved artifact filename (default: probe)",
    )


def resolve_date_window(
    start_date: Optional[str],
    end_date: Optional[str],
    *,
    days: int = 180,
) -> tuple[date, date]:
    today = datetime.now(timezone.utc).date()
    start = date.fromisoformat(start_date) if start_date else today
    end = date.fromisoformat(end_date) if end_date else start + timedelta(days=days)
    if end < start:
        raise ValueError(f"end-date {end} is before start-date {start}")
    return start, end


def city_state_slug(city: Optional[str], state: str = "TX") -> Optional[str]:
    if not city:
        return None
    key = city.strip().lower()
    if key in TEXAS_CITY_SLUGS:
        return TEXAS_CITY_SLUGS[key]
    region = state.strip().lower()
    slug = re.sub(r"[^a-z0-9]+", "-", key).strip("-")
    return f"{region}--{slug}" if slug else None


def artist_slug(artist: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", artist.strip().lower()).strip("-")


def genre_browse_slug(genre: Optional[str]) -> Optional[str]:
    if not genre:
        return None
    key = genre.strip().lower()
    if key in GENRE_SLUGS:
        return GENRE_SLUGS[key]
    return re.sub(r"[^a-z0-9]+", "-", key).strip("-")


def make_dedupe_key(
    source: str,
    provider_event_id: str,
    *,
    artist_or_title: str = "",
    venue: str = "",
    starts_at: str = "",
) -> str:
    if provider_event_id:
        return f"{source}|{provider_event_id}"
    digest = hashlib.sha1(
        "|".join(
            [
                source,
                artist_or_title.strip().lower(),
                venue.strip().lower(),
                starts_at.strip(),
            ]
        ).encode("utf-8")
    ).hexdigest()[:16]
    return f"{source}|{digest}"


def build_normalized_event(
    *,
    source: str,
    provider_event_id: str,
    artist_or_title: str,
    venue: str = "",
    city: str = "",
    state: str = "",
    starts_at: str = "",
    ticket_url: str = "",
    genre_or_classification: str = "",
    raw_url: str = "",
) -> NormalizedEvent:
    return NormalizedEvent(
        source=source,
        provider_event_id=provider_event_id or "",
        artist_or_title=artist_or_title or "",
        venue=venue or "",
        city=city or "",
        state=state or "",
        starts_at=starts_at or "",
        ticket_url=ticket_url or "",
        genre_or_classification=genre_or_classification or "",
        raw_url=raw_url or ticket_url or "",
        dedupe_key=make_dedupe_key(
            source,
            provider_event_id,
            artist_or_title=artist_or_title,
            venue=venue,
            starts_at=starts_at,
        ),
    )


def extract_eventbrite_id(url: str) -> str:
    match = re.search(r"-(\d{9,})(?:\?|$)", url or "")
    return match.group(1) if match else ""


def http_get_json(
    url: str,
    *,
    params: Optional[dict[str, Any]] = None,
    headers: Optional[dict[str, str]] = None,
    timeout: int = 30,
) -> tuple[int, Any, str]:
    response = requests.get(
        url,
        params=params,
        headers=headers or BROWSER_HEADERS,
        timeout=timeout,
    )
    content_type = response.headers.get("content-type", "")
    if "json" in content_type.lower():
        try:
            return response.status_code, response.json(), response.text
        except ValueError:
            return response.status_code, None, response.text
    return response.status_code, None, response.text


def http_get_text(
    url: str,
    *,
    params: Optional[dict[str, Any]] = None,
    headers: Optional[dict[str, str]] = None,
    timeout: int = 30,
) -> requests.Response:
    return requests.get(
        url,
        params=params,
        headers=headers or BROWSER_HEADERS,
        timeout=timeout,
    )


def save_artifact(
    source: str,
    label: str,
    payload: Any,
    *,
    events: Optional[list[NormalizedEvent]] = None,
    meta: Optional[dict[str, Any]] = None,
) -> Path:
    out_dir = ARTIFACTS_ROOT / source
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = out_dir / f"{label}_{stamp}.json"
    body: dict[str, Any] = {
        "saved_at": stamp,
        "source": source,
        "meta": meta or {},
        "payload": payload,
    }
    if events is not None:
        body["normalized_events"] = [event.to_dict() for event in events]
        body["normalized_count"] = len(events)
    path.write_text(json.dumps(body, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def print_probe_summary(
    *,
    source: str,
    events: list[NormalizedEvent],
    artifact_path: Optional[Path],
    notes: Optional[list[str]] = None,
    json_only: bool = False,
) -> None:
    payload = [event.to_dict() for event in events]
    if json_only:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return

    print(f"source={source}")
    print(f"normalized_count={len(events)}")
    if artifact_path is not None:
        print(f"artifact={artifact_path.relative_to(REPO_ROOT)}")
    if notes:
        for note in notes:
            print(f"note={note}")
    print("normalized_events=")
    print(json.dumps(payload, indent=2, ensure_ascii=False))


def handle_probe_main(
    logger: logging.Logger,
    *,
    source: str,
    run_probe: Any,
    argv: Optional[list[str]] = None,
) -> int:
    parser = argparse.ArgumentParser(description=f"Read-only {source} concerts probe")
    add_common_cli_args(parser)
    args = parser.parse_args(argv)
    try:
        start, end = resolve_date_window(args.start_date, args.end_date, days=args.days)
        events, artifact_path, notes = run_probe(args, start, end, logger)
        print_probe_summary(
            source=source,
            events=events,
            artifact_path=artifact_path,
            notes=notes,
            json_only=args.json,
        )
        return 0
    except MissingCredentialError as exc:
        logger.error(str(exc))
        return 2
    except requests.RequestException as exc:
        logger.error("HTTP error: %s", exc)
        return 1
    except ValueError as exc:
        logger.error("%s", exc)
        return 1


def ticketmaster_datetime(value: date, *, end_of_day: bool = False) -> str:
    if end_of_day:
        return f"{value.isoformat()}T23:59:59Z"
    return f"{value.isoformat()}T00:00:00Z"


def quote_path_segment(value: str) -> str:
    return quote_plus(value.strip())
