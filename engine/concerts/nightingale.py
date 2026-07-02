"""Nightingale handoff object for future concert integration."""

from __future__ import annotations

from typing import Any

from engine.concerts.dedupe import MergedConcertEvent
from engine.concerts.probe_util import NormalizedEvent


def to_nightingale_handoff(event: MergedConcertEvent) -> dict[str, str]:
    """
    Export a clean handoff payload for future Nightingale integration.

    Does not persist or send anywhere — callers decide when to use it.
    """
    primary = event.provider_events[0] if event.provider_events else None
    return {
        "artist_or_title": event.artist_or_title,
        "venue": event.venue,
        "city": event.city,
        "state": event.state,
        "starts_at": event.starts_at,
        "ticket_url": event.ticket_url,
        "source": event.source_label,
        "provider_event_id": primary.provider_event_id if primary else "",
        "event_dedupe_key": event.event_dedupe_key,
    }


def to_nightingale_handoff_from_provider(event: NormalizedEvent) -> dict[str, str]:
    return {
        "artist_or_title": event.artist_or_title,
        "venue": event.venue,
        "city": event.city,
        "state": event.state,
        "starts_at": event.starts_at,
        "ticket_url": event.ticket_url,
        "source": event.source,
        "provider_event_id": event.provider_event_id,
        "event_dedupe_key": event.event_dedupe_key,
    }


def export_handoffs(events: list[MergedConcertEvent]) -> list[dict[str, Any]]:
    return [to_nightingale_handoff(e) for e in events]
