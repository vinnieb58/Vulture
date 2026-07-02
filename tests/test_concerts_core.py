"""Tests for concert dedupe, filters, formatter, and nightingale handoff."""

from __future__ import annotations

from engine.concerts.dedupe import merge_events
from engine.concerts.filters import passes_genre_filter
from engine.concerts.formatter import format_alert_message, format_event_card
from engine.concerts.nightingale import to_nightingale_handoff
from engine.concerts.probe_util import build_normalized_event


def _event(source: str, pid: str, artist: str, venue: str, starts: str, genre: str = "Rock"):
    return build_normalized_event(
        source=source,
        provider_event_id=pid,
        artist_or_title=artist,
        venue=venue,
        city="Houston",
        state="TX",
        starts_at=starts,
        ticket_url=f"https://example.com/{pid}",
        genre_or_classification=genre,
    )


class TestMergeEvents:
    def test_collapses_cross_provider_duplicates(self):
        tm = _event("ticketmaster", "tm-1", "Sevendust", "Toyota Center", "2026-09-11T20:00:00Z")
        sg = _event("seatgeek", "sg-99", "Sevendust", "Toyota Center", "2026-09-11T20:00:00Z")
        merged = merge_events([tm, sg])
        assert len(merged) == 1
        assert set(merged[0].sources) == {"ticketmaster", "seatgeek"}

    def test_collapses_same_provider_duplicates(self):
        a = _event("ticketmaster", "tm-a", "Scene Queen", "Paper Tiger", "2026-09-27T19:00:00Z")
        b = _event("ticketmaster", "tm-b", "Scene Queen", "Paper Tiger", "2026-09-27T19:00:00Z")
        merged = merge_events([a, b])
        assert len(merged) == 1
        assert merged[0].event_dedupe_key == a.event_dedupe_key


class TestGenreFilter:
    def test_artist_watch_skips_genre_filter(self):
        ev = _event("ticketmaster", "1", "Disturbed", "Arena", "2026-10-01T20:00:00Z", genre="Pop")
        assert passes_genre_filter(ev, genre="rock", artist_query="Disturbed")

    def test_rock_watch_excludes_pop(self):
        ev = _event("ticketmaster", "2", "Someone", "Arena", "2026-10-01T20:00:00Z", genre="Pop")
        assert not passes_genre_filter(ev, genre="rock", artist_query=None)

    def test_rock_watch_includes_metal(self):
        ev = _event("ticketmaster", "3", "Band", "Arena", "2026-10-01T20:00:00Z", genre="Metal")
        assert passes_genre_filter(ev, genre="rock", artist_query=None)

    def test_seatgeek_sports_excluded(self):
        ev = _event("seatgeek", "4", "Texans", "NRG", "2026-10-01T20:00:00Z", genre="sports, nfl")
        assert not passes_genre_filter(ev, genre="rock", artist_query=None)


class TestFormatter:
    def test_event_card_fields(self):
        merged = merge_events([_event("ticketmaster", "x", "Shinedown", "Arena", "2026-11-01T20:00:00Z")])[0]
        card = format_event_card(merged, index=1)
        assert "Shinedown" in card
        assert "ticketmaster" in card
        assert "https://example.com/x" in card

    def test_alert_format(self):
        merged = merge_events([_event("ticketmaster", "x", "Shinedown", "Arena", "2026-11-01T20:00:00Z")])[0]
        alert = format_alert_message(merged)
        assert alert.startswith("🎵")
        assert "Artist: Shinedown" in alert


class TestNightingaleHandoff:
    def test_handoff_fields(self):
        merged = merge_events([_event("ticketmaster", "pid-1", "Three Days Grace", "Arena", "2026-12-01T20:00:00Z")])[0]
        handoff = to_nightingale_handoff(merged)
        assert handoff["artist_or_title"] == "Three Days Grace"
        assert handoff["provider_event_id"] == "pid-1"
        assert handoff["event_dedupe_key"] == merged.event_dedupe_key
        assert "venue" in handoff
        assert "ticket_url" in handoff
