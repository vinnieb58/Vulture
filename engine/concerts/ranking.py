"""Deterministic result ranking and SeatGeek noise filtering."""

from __future__ import annotations

import re

from engine.concerts.dedupe import MergedConcertEvent
from engine.concerts.probe_util import NormalizedEvent, classify_seatgeek_taxonomies

# Title/artist/venue tokens that rescue generic SeatGeek "concert" rows.
ROCK_MATCH_TERMS = frozenset(
    {
        "rock",
        "metal",
        "alternative",
        "punk",
        "grunge",
        "hardcore",
        "metalcore",
        "hard rock",
        "shinedown",
        "disturbed",
        "sevendust",
        "metallica",
        "slayer",
        "anthrax",
        "megadeth",
    }
)

ROCK_TAXONOMY_TOKENS = frozenset(
    {
        "rock",
        "metal",
        "alternative",
        "punk",
        "indie",
        "hard rock",
        "classic rock",
        "hard-rock",
    }
)


def _normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", (value or "").strip().lower())


def _searchable_blob(*parts: str) -> str:
    return _normalize_text(" ".join(p for p in parts if p))


def is_seatgeek_generic_noise(
    event: NormalizedEvent,
    *,
    genre: str | None,
    artist_query: str | None,
) -> bool:
    """
    Hide SeatGeek-only rows with generic concert taxonomy and no rock signal.

    Explicit artist searches skip this filter — provider already scoped.
    """
    if event.source != "seatgeek":
        return False
    if artist_query:
        return False
    if not genre:
        return False

    label = (event.genre_or_classification or "").strip().lower()
    if classify_seatgeek_taxonomies(label) == "negative":
        return True

    tokens = {_normalize_text(t) for t in re.split(r"[,/]", label) if t.strip()}
    has_rock_taxonomy = any(t in ROCK_TAXONOMY_TOKENS for t in tokens)
    if has_rock_taxonomy:
        return False

    blob = _searchable_blob(event.artist_or_title, event.venue)
    if any(term in blob for term in ROCK_MATCH_TERMS):
        return False

    # Generic SeatGeek "concert" taxonomy without rock/metal/alternative signal.
    if "concert" in tokens or not tokens:
        return True
    return False


def _artist_match_score(artist_query: str | None, title: str) -> int:
    if not artist_query:
        return 0
    needle = _normalize_text(artist_query)
    hay = _normalize_text(title)
    if not needle or not hay:
        return 0
    if hay == needle:
        return 1000
    if needle in hay:
        return 500
    return 0


def _source_rank_score(
    event: MergedConcertEvent,
    *,
    artist_query: str | None,
    genre: str | None,
) -> int:
    score = 0
    sources = set(event.sources)
    if "ticketmaster" in sources:
        score += 100
    if sources == {"seatgeek"}:
        score -= 10
    if genre and not artist_query and "ticketmaster" in sources:
        score += 50
    return score


def rank_merged_events(
    events: list[MergedConcertEvent],
    *,
    artist_query: str | None = None,
    genre: str | None = None,
) -> list[MergedConcertEvent]:
    """Rank merged events: explicit artist match first, Ticketmaster preferred."""

    def sort_key(event: MergedConcertEvent) -> tuple:
        artist_score = _artist_match_score(artist_query, event.artist_or_title)
        source_score = _source_rank_score(
            event,
            artist_query=artist_query,
            genre=genre,
        )
        total = artist_score + source_score
        return (-total, event.starts_at, event.artist_or_title.lower())

    return sorted(events, key=sort_key)
