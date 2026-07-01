"""Dataclasses for the Vulture Concerts vertical."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass
class ConcertWatch:
    id: int
    artist_query: Optional[str]
    genre: Optional[str]
    area: Optional[str]
    city: Optional[str]
    state: Optional[str]
    radius_miles: Optional[int]
    days_forward: int
    active: bool
    created_at: str


@dataclass
class ConcertEvent:
    id: int
    source: str
    provider_event_id: str
    artist_or_title: str
    venue: str
    city: str
    state: str
    starts_at: str
    ticket_url: str
    genre_or_classification: str
    event_dedupe_key: str
    first_seen_at: str
    last_seen_at: str


@dataclass
class ConcertAlert:
    id: int
    watch_id: int
    event_dedupe_key: str
    alerted_at: str
