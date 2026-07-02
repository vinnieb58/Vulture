"""Tests for concert search cleanup: ranking, noise filter, formatter, watch management."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from engine.concerts.command_router import dispatch_concert
from engine.concerts.dedupe import merge_events
from engine.concerts.formatter import format_search_results
from engine.concerts.probe_util import build_normalized_event
from engine.concerts.ranking import is_seatgeek_generic_noise, rank_merged_events
from engine.concerts.repository import (
    create_watch,
    init_concert_tables,
    list_watches,
    pause_watch,
    unwatch,
)
from engine.concerts.search import DEFAULT_DISPLAY_LIMIT, SearchCriteria, SearchResult
from engine.concerts.stats import SearchStats


def _event(
    source: str,
    pid: str,
    artist: str,
    venue: str,
    starts: str,
    genre: str = "Rock",
):
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


def _merged_from_events(events):
    return merge_events(events)


class TestRanking:
    def test_ticketmaster_ranks_above_seatgeek_duplicate(self):
        tm = _event("ticketmaster", "tm-1", "Sevendust", "Toyota Center", "2026-09-11T20:00:00Z")
        sg = _event("seatgeek", "sg-1", "Sevendust", "Toyota Center", "2026-09-11T20:00:00Z")
        merged = _merged_from_events([tm, sg])[0]
        assert merged.ticket_url == tm.ticket_url
        assert "ticketmaster" in merged.sources

    def test_ticketmaster_only_ranks_above_seatgeek_only(self):
        tm = _merged_from_events(
            [_event("ticketmaster", "tm", "Band A", "Arena", "2026-10-01T20:00:00Z")]
        )[0]
        sg = _merged_from_events(
            [_event("seatgeek", "sg", "Band B", "Arena", "2026-10-02T20:00:00Z", genre="concert")]
        )[0]
        ranked = rank_merged_events([sg, tm], genre="rock")
        assert ranked[0].artist_or_title == "Band A"

    def test_explicit_artist_match_ranks_first(self):
        exact = _merged_from_events(
            [_event("ticketmaster", "1", "Shinedown", "Arena", "2026-12-01T20:00:00Z")]
        )[0]
        other = _merged_from_events(
            [_event("ticketmaster", "2", "Shinedown Tribute", "Club", "2026-11-01T20:00:00Z")]
        )[0]
        ranked = rank_merged_events([other, exact], artist_query="Shinedown")
        assert ranked[0].artist_or_title == "Shinedown"


class TestSeatGeekNoise:
    def test_generic_concert_hidden_on_broad_genre(self):
        ev = _event("seatgeek", "sg", "Symphony Night", "Hall", "2026-10-01T20:00:00Z", genre="concert")
        assert is_seatgeek_generic_noise(ev, genre="rock", artist_query=None)

    def test_rock_title_rescues_generic_concert(self):
        ev = _event("seatgeek", "sg", "Metal Night", "Hall", "2026-10-01T20:00:00Z", genre="concert")
        assert not is_seatgeek_generic_noise(ev, genre="rock", artist_query=None)

    def test_artist_search_skips_noise_filter(self):
        ev = _event("seatgeek", "sg", "Symphony", "Hall", "2026-10-01T20:00:00Z", genre="concert")
        assert not is_seatgeek_generic_noise(ev, genre="rock", artist_query="Symphony")

    @patch("engine.concerts.search.search_seatgeek")
    @patch("engine.concerts.search.search_ticketmaster")
    def test_broad_genre_hides_seatgeek_noise_in_pipeline(
        self,
        mock_tm,
        mock_sg,
    ):
        from engine.concerts.search import search_concerts

        mock_tm.return_value = (
            [_event("ticketmaster", "tm", "Rock Band", "Arena", "2026-10-01T20:00:00Z")],
            None,
        )
        mock_sg.return_value = (
            [
                _event("seatgeek", "sg1", "Rock Band SG", "Arena", "2026-10-02T20:00:00Z", genre="concert, rock"),
                _event("seatgeek", "sg2", "Comedy Hour", "Club", "2026-10-03T20:00:00Z", genre="concert"),
            ],
            None,
        )
        result = search_concerts(SearchCriteria(genre="rock", area="houston", days_forward=180))
        assert result.stats.noise_hidden >= 1
        titles = [e.artist_or_title for e in result.events]
        assert "Comedy Hour" not in titles


class TestFormatterCleanup:
    def test_max_ten_results(self):
        events = _merged_from_events(
            [
                _event("ticketmaster", f"tm-{i}", f"Band {i}", "Arena", f"2026-10-{i+1:02d}T20:00:00Z")
                for i in range(15)
            ]
        )
        stats = SearchStats(ticketmaster_returned=15, merged_count=15, displayed_count=10)
        text = format_search_results(SearchResult(events=events, stats=stats))
        assert "showing top 10" in text
        assert text.count("\n   http") == 10

    def test_provider_summary_in_output(self):
        events = _merged_from_events(
            [_event("ticketmaster", "tm", "Band", "Arena", "2026-10-01T20:00:00Z")]
        )
        stats = SearchStats(
            ticketmaster_returned=5,
            seatgeek_returned=12,
            merged_count=1,
            noise_hidden=3,
            displayed_count=1,
        )
        text = format_search_results(SearchResult(events=events, stats=stats))
        assert "Ticketmaster returned **5**" in text
        assert "SeatGeek returned **12**" in text
        assert "3 noisy SeatGeek" in text

    def test_default_display_limit_is_ten(self):
        assert DEFAULT_DISPLAY_LIMIT == 10


class TestWatchManagement:
    @pytest.fixture()
    def concert_db(self, tmp_path, monkeypatch):
        db_path = tmp_path / "test_vulture.db"
        monkeypatch.setattr("engine.database.DB_PATH", db_path)
        init_concert_tables()
        yield db_path

    def test_pause_watch(self, concert_db):
        watch = create_watch(SearchCriteria(artist_query="Disturbed", area="houston"))
        paused = pause_watch(watch.id)
        assert paused is not None
        assert paused.active is False
        assert list_watches(active_only=True) == []

    def test_unwatch_removes_watch(self, concert_db):
        watch = create_watch(SearchCriteria(artist_query="Shinedown", area="houston"))
        assert unwatch(watch.id) is True
        assert list_watches(active_only=True) == []
        assert unwatch(watch.id) is False

    def test_dispatch_pause(self, concert_db):
        watch = create_watch(SearchCriteria(artist_query="A", area="houston"))
        result = dispatch_concert("pause", {"watch_id": watch.id})
        assert result.success
        assert "paused" in result.message.lower()

    def test_dispatch_unwatch(self, concert_db):
        watch = create_watch(SearchCriteria(artist_query="B", area="houston"))
        result = dispatch_concert("unwatch", {"watch_id": watch.id})
        assert result.success
        assert "removed" in result.message.lower()
