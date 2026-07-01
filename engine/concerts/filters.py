"""Deterministic genre/taxonomy filtering for concert watches."""

from __future__ import annotations

from typing import Protocol

from engine.concerts.probe_util import classify_genre_signal, classify_seatgeek_taxonomies


class _HasGenre(Protocol):
    genre_or_classification: str
    source: str


def passes_genre_filter(event: _HasGenre, *, genre: str | None, artist_query: str | None) -> bool:
    """
    Deterministic genre filter for broad watches.

    Explicit artist watches skip genre filtering.
    """
    if artist_query:
        return True
    if not genre:
        return True

    genre_key = genre.strip().lower()
    if genre_key != "rock":
        # Only rock broad filtering is defined for v1.
        return True

    label = event.genre_or_classification or ""
    if event.source == "seatgeek":
        signal = classify_seatgeek_taxonomies(label)
    else:
        signal = classify_genre_signal(label)

    if signal == "negative":
        return False
    if signal == "positive":
        return True
    # Neutral unknown classifications are excluded from broad rock watches.
    return False
