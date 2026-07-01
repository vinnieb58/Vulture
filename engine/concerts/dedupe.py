"""Merge and dedupe concert events across providers."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Iterable

from engine.concerts.probe_util import NormalizedEvent


@dataclass
class MergedConcertEvent:
    """One show collapsed across providers."""

    artist_or_title: str
    venue: str
    city: str
    state: str
    starts_at: str
    ticket_url: str
    genre_or_classification: str
    event_dedupe_key: str
    sources: list[str] = field(default_factory=list)
    provider_events: list[NormalizedEvent] = field(default_factory=list)

    @property
    def source_label(self) -> str:
        unique = sorted(set(self.sources))
        return ", ".join(unique)


def merge_events(events: Iterable[NormalizedEvent]) -> list[MergedConcertEvent]:
    """Collapse duplicate same-show rows by event_dedupe_key."""
    grouped: dict[str, list[NormalizedEvent]] = defaultdict(list)
    for event in events:
        key = event.event_dedupe_key
        if not key:
            continue
        grouped[key].append(event)

    merged: list[MergedConcertEvent] = []
    for key, group in grouped.items():
        # Prefer Ticketmaster ticket URL when available.
        group_sorted = sorted(
            group,
            key=lambda e: (0 if e.source == "ticketmaster" else 1, e.ticket_url),
        )
        primary = group_sorted[0]
        sources = [e.source for e in group_sorted]
        merged.append(
            MergedConcertEvent(
                artist_or_title=primary.artist_or_title,
                venue=primary.venue,
                city=primary.city,
                state=primary.state,
                starts_at=primary.starts_at,
                ticket_url=primary.ticket_url,
                genre_or_classification=primary.genre_or_classification,
                event_dedupe_key=key,
                sources=sources,
                provider_events=group_sorted,
            )
        )

    merged.sort(key=lambda e: (e.starts_at, e.artist_or_title.lower()))
    return merged
