"""Shared helpers for Vulture Concerts source probe scripts (read-only recon)."""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import sys
import uuid
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

# Proposed deterministic filter signals (advisory Ticketmaster classifications).
POSITIVE_GENRE_SIGNALS = frozenset(
    {
        "rock",
        "hard rock",
        "metal",
        "alternative",
        "alternative rock",
        "punk",
        "punk rock",
    }
)
NEGATIVE_GENRE_SIGNALS = frozenset(
    {
        "pop",
        "country",
        "r&b",
        "rnb",
        "other",
        "hip-hop/rap",
        "hip hop/rap",
        "latin",
        "jazz",
        "classical",
        "dance/electronic",
    }
)

# SeatGeek taxonomies are comma-joined in probe output (e.g. "concert, rock").
SEATGEEK_POSITIVE_TAXONOMY_TOKENS = frozenset(
    {
        "concert",
        "rock",
        "indie",
        "metal",
        "alternative",
        "punk",
        "hard rock",
        "classic rock",
    }
)
SEATGEEK_NEGATIVE_TAXONOMY_TOKENS = frozenset(
    {
        "sports",
        "comedy",
        "theater",
        "family",
        "nfl",
        "nba",
        "mlb",
        "nhl",
        "mls",
        "ncaa",
        "monster truck",
        "wrestling",
        "rodeo",
    }
)

ARTIFACT_FILENAME_VERSION = 2
ARTIFACT_FILENAME_RE = re.compile(
    r"^[a-zA-Z0-9][a-zA-Z0-9_-]*_\d{8}T\d{9}_\d+_[0-9a-f]{8}\.json$"
)
LEGACY_ARTIFACT_FILENAME_RE = re.compile(
    r"^[a-zA-Z0-9][a-zA-Z0-9_-]*_\d{8}T\d{6}Z\.json$"
)


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
    event_dedupe_key: str

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


def normalize_dedupe_text(value: str) -> str:
    """Lowercase and collapse whitespace/punctuation for stable dedupe keys."""
    collapsed = re.sub(r"[^a-z0-9]+", " ", (value or "").strip().lower())
    return re.sub(r"\s+", " ", collapsed).strip()


def normalize_local_starts_at(starts_at: str) -> str:
    """Normalize start time to local date/time string for event-level dedupe."""
    raw = (starts_at or "").strip()
    if not raw:
        return ""
    if "T" in raw:
        local_part = raw.split("T", 1)[0]
        time_match = re.search(r"T(\d{2}:\d{2})", raw)
        if time_match:
            return f"{local_part} {time_match.group(1)}"
        return local_part
    return raw[:16]


def make_provider_dedupe_key(source: str, provider_event_id: str) -> str:
    """Provider-scoped identity: source + provider_event_id."""
    if provider_event_id:
        return f"{source}|{provider_event_id}"
    return f"{source}|"


def make_event_dedupe_key(
    *,
    artist_or_title: str,
    venue: str,
    starts_at: str,
) -> str:
    """Show-scoped identity: normalized artist + venue + local date/time."""
    parts = [
        normalize_dedupe_text(artist_or_title),
        normalize_dedupe_text(venue),
        normalize_local_starts_at(starts_at),
    ]
    if not any(parts):
        return ""
    digest = hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:16]
    return f"event|{digest}"


def make_dedupe_key(
    source: str,
    provider_event_id: str,
    *,
    artist_or_title: str = "",
    venue: str = "",
    starts_at: str = "",
) -> str:
    """Backward-compatible alias for provider_dedupe_key."""
    _ = (artist_or_title, venue, starts_at)
    return make_provider_dedupe_key(source, provider_event_id)


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
        dedupe_key=make_provider_dedupe_key(source, provider_event_id),
        event_dedupe_key=make_event_dedupe_key(
            artist_or_title=artist_or_title,
            venue=venue,
            starts_at=starts_at,
        ),
    )


def split_taxonomy_tokens(taxonomy_csv: str) -> list[str]:
    return [
        token.strip().lower()
        for token in re.split(r"[,/]", taxonomy_csv or "")
        if token.strip()
    ]


def classify_seatgeek_taxonomies(taxonomy_csv: str) -> str:
    """
    Deterministic SeatGeek taxonomy signal for broad concert watches.

    Returns: positive | negative | neutral
    """
    tokens = split_taxonomy_tokens(taxonomy_csv)
    if not tokens:
        return "neutral"
    if any(token in SEATGEEK_NEGATIVE_TAXONOMY_TOKENS for token in tokens):
        return "negative"
    if any(token in SEATGEEK_POSITIVE_TAXONOMY_TOKENS for token in tokens):
        return "positive"
    for token in tokens:
        if "sport" in token or "comedy" in token or "theater" in token:
            return "negative"
        if "rock" in token or "metal" in token or "concert" in token:
            return "positive"
    return "neutral"


def count_by_taxonomy_token(events: list[NormalizedEvent]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for event in events:
        for token in split_taxonomy_tokens(event.genre_or_classification):
            counts[token] = counts.get(token, 0) + 1
    return dict(sorted(counts.items(), key=lambda item: (-item[1], item[0])))


def classify_genre_signal(genre_or_classification: str) -> str:
    """
    Deterministic genre signal for broad rock watches.

    Returns: positive | negative | neutral
    """
    normalized = normalize_dedupe_text(genre_or_classification)
    if not normalized:
        return "neutral"
    if normalized in POSITIVE_GENRE_SIGNALS:
        return "positive"
    if normalized in NEGATIVE_GENRE_SIGNALS:
        return "negative"
    for label in POSITIVE_GENRE_SIGNALS:
        if label in normalized:
            return "positive"
    for label in NEGATIVE_GENRE_SIGNALS:
        if label in normalized:
            return "negative"
    return "neutral"


def count_by_genre(events: list[NormalizedEvent]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for event in events:
        label = (event.genre_or_classification or "(none)").strip() or "(none)"
        counts[label] = counts.get(label, 0) + 1
    return dict(sorted(counts.items(), key=lambda item: (-item[1], item[0].lower())))


def summarize_event_duplicates(events: list[NormalizedEvent]) -> list[dict[str, Any]]:
    """Group events by event_dedupe_key; return groups with more than one provider row."""
    grouped: dict[str, list[NormalizedEvent]] = {}
    for event in events:
        key = event.event_dedupe_key
        if not key:
            continue
        grouped.setdefault(key, []).append(event)

    duplicates: list[dict[str, Any]] = []
    for key, group in grouped.items():
        if len(group) < 2:
            continue
        duplicates.append(
            {
                "event_dedupe_key": key,
                "count": len(group),
                "artist_or_title": group[0].artist_or_title,
                "venue": group[0].venue,
                "starts_at": group[0].starts_at,
                "provider_event_ids": [event.provider_event_id for event in group],
                "dedupe_keys": [event.dedupe_key for event in group],
            }
        )
    duplicates.sort(
        key=lambda item: (
            str(item.get("starts_at") or ""),
            str(item.get("artist_or_title") or "").lower(),
        )
    )
    return duplicates


def artifact_filename(label: str) -> str:
    """
    Unique artifact basename: {label}_{YYYYMMDDTHHMMSSmmm}_{pid}_{uuid8}.json

    Example: probe_20260701T174415433_14394_67f80a94.json
    """
    safe_label = re.sub(r"[^a-zA-Z0-9_-]+", "_", (label or "probe").strip()).strip("_") or "probe"
    now = datetime.now(timezone.utc)
    stamp = now.strftime("%Y%m%dT%H%M%S")
    ms = f"{now.microsecond // 1000:03d}"
    suffix = uuid.uuid4().hex[:8]
    name = f"{safe_label}_{stamp}{ms}_{os.getpid()}_{suffix}.json"
    if not ARTIFACT_FILENAME_RE.match(name):
        raise RuntimeError(f"Invalid artifact filename generated: {name}")
    if LEGACY_ARTIFACT_FILENAME_RE.match(name):
        raise RuntimeError(f"Legacy artifact filename format detected: {name}")
    return name


def is_collision_resistant_artifact_name(name: str) -> bool:
    """True when filename matches the v2 collision-resistant artifact format."""
    return bool(ARTIFACT_FILENAME_RE.match(name))


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
    basename = artifact_filename(label)
    path = out_dir / basename
    body: dict[str, Any] = {
        "saved_at": stamp,
        "source": source,
        "artifact_basename": basename,
        "artifact_filename_version": ARTIFACT_FILENAME_VERSION,
        "meta": meta or {},
        "payload": payload,
    }
    if events is not None:
        body["normalized_events"] = [event.to_dict() for event in events]
        body["normalized_count"] = len(events)
        body["genre_counts"] = count_by_genre(events)
        body["taxonomy_token_counts"] = count_by_taxonomy_token(events)
        body["duplicate_groups"] = summarize_event_duplicates(events)
        body["duplicate_group_count"] = len(body["duplicate_groups"])
    path.write_text(json.dumps(body, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def print_probe_summary(
    *,
    source: str,
    events: list[NormalizedEvent],
    artifact_path: Optional[Path],
    notes: Optional[list[str]] = None,
    json_only: bool = False,
    show_duplicate_summary: bool = True,
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

    genre_counts = count_by_genre(events)
    if genre_counts:
        print("genre_counts=")
        print(json.dumps(genre_counts, indent=2, ensure_ascii=False))

    if show_duplicate_summary:
        duplicate_groups = summarize_event_duplicates(events)
        print(f"duplicate_group_count={len(duplicate_groups)}")
        if duplicate_groups:
            print("duplicate_groups=")
            print(json.dumps(duplicate_groups, indent=2, ensure_ascii=False))

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
