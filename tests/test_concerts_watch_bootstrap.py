"""Tests for concert watch bootstrap alert seeding and runner alert behavior."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from engine.concerts.command_router import dispatch_concert
from engine.concerts.dedupe import MergedConcertEvent
from engine.concerts.probe_util import build_normalized_event
from engine.concerts.repository import (
    alert_exists,
    create_watch,
    init_concert_tables,
    seed_bootstrap_alerts,
)
from engine.concerts.search import SearchCriteria, SearchResult
from engine.concerts.stats import SearchStats
from engine.concerts.watch_runner import run_concert_watches


@pytest.fixture()
def concert_db(tmp_path, monkeypatch):
    db_path = tmp_path / "test_vulture.db"
    monkeypatch.setattr("engine.database.DB_PATH", db_path)
    init_concert_tables()
    yield db_path


def _provider_event(source: str, pid: str, artist: str, venue: str, starts: str):
    return build_normalized_event(
        source=source,
        provider_event_id=pid,
        artist_or_title=artist,
        venue=venue,
        city="Houston",
        state="TX",
        starts_at=starts,
        ticket_url=f"https://example.com/{pid}",
        genre_or_classification="Rock",
    )


def _merged_event(
    artist: str,
    venue: str,
    starts: str,
    *,
    source: str = "ticketmaster",
    pid: str = "ev-1",
) -> MergedConcertEvent:
    pe = _provider_event(source, pid, artist, venue, starts)
    return MergedConcertEvent(
        artist_or_title=artist,
        venue=venue,
        city="Houston",
        state="TX",
        starts_at=starts,
        ticket_url=pe.ticket_url,
        genre_or_classification="Rock",
        event_dedupe_key=pe.event_dedupe_key,
        sources=[source],
        provider_events=[pe],
    )


def _search_result(events: list[MergedConcertEvent]) -> SearchResult:
    return SearchResult(events=events, provider_notes=[], queries_run=1, stats=SearchStats())


class TestBootstrapSeeding:
    def test_seed_bootstrap_alerts_records_ledger(self, concert_db):
        watch = create_watch(SearchCriteria(artist_query="Shinedown", area="houston"))
        existing = _merged_event("Shinedown", "Toyota Center", "2026-11-01T20:00:00Z")
        seeded = seed_bootstrap_alerts(watch.id, [existing])
        assert seeded == 1
        assert alert_exists(watch.id, existing.event_dedupe_key)

    @patch("engine.concerts.command_router.search_concerts")
    def test_watch_creation_seeds_alert_ledger(self, mock_search, concert_db):
        existing = _merged_event("Shinedown", "Toyota Center", "2026-11-01T20:00:00Z")
        mock_search.return_value = _search_result([existing])

        result = dispatch_concert(
            "watch",
            {"query": 'artist:"Shinedown" area:"houston" days:365'},
        )

        assert result.success
        watch_id = result.data["watch_id"]
        assert result.data["bootstrap_seeded"] == 1
        assert alert_exists(watch_id, existing.event_dedupe_key)


class TestWatchRunnerAlerts:
    @patch("engine.concerts.watch_runner.send_concert_alert", return_value=True)
    @patch("engine.concerts.watch_runner.search_concerts")
    @patch("engine.concerts.command_router.search_concerts")
    def test_first_cycle_after_watch_creation_sends_zero_alerts(
        self,
        mock_cmd_search,
        mock_runner_search,
        mock_send,
        concert_db,
    ):
        existing = _merged_event("Disturbed", "Arena", "2026-12-01T20:00:00Z")
        result = _search_result([existing])
        mock_cmd_search.return_value = result
        mock_runner_search.return_value = result

        created = dispatch_concert(
            "watch",
            {"query": 'artist:"Disturbed" area:"houston" days:365'},
        )
        assert created.success

        mock_send.reset_mock()
        mock_runner_search.return_value = result

        summary = run_concert_watches()

        assert summary["alerts_sent"] == 0
        mock_send.assert_not_called()

    @patch("engine.concerts.watch_runner.send_concert_alert", return_value=True)
    @patch("engine.concerts.watch_runner.search_concerts")
    @patch("engine.concerts.command_router.search_concerts")
    def test_future_new_event_sends_one_alert(
        self,
        mock_cmd_search,
        mock_runner_search,
        mock_send,
        concert_db,
    ):
        existing = _merged_event("Disturbed", "Arena", "2026-12-01T20:00:00Z", pid="ev-old")
        bootstrap = _search_result([existing])
        mock_cmd_search.return_value = bootstrap
        dispatch_concert("watch", {"query": 'artist:"Disturbed" area:"houston" days:365'})

        new_event = _merged_event(
            "Disturbed",
            "New Venue",
            "2027-01-15T20:00:00Z",
            pid="ev-new",
        )
        mock_send.reset_mock()
        mock_runner_search.return_value = _search_result([existing, new_event])

        summary = run_concert_watches()

        assert summary["alerts_sent"] == 1
        mock_send.assert_called_once()

    @patch("engine.concerts.watch_runner.send_concert_alert", return_value=True)
    @patch("engine.concerts.watch_runner.search_concerts")
    @patch("engine.concerts.command_router.search_concerts")
    def test_duplicate_second_run_suppresses_alert(
        self,
        mock_cmd_search,
        mock_runner_search,
        mock_send,
        concert_db,
    ):
        existing = _merged_event("Disturbed", "Arena", "2026-12-01T20:00:00Z", pid="ev-old")
        bootstrap = _search_result([existing])
        mock_cmd_search.return_value = bootstrap
        dispatch_concert("watch", {"query": 'artist:"Disturbed" area:"houston" days:365'})

        new_event = _merged_event(
            "Disturbed",
            "New Venue",
            "2027-01-15T20:00:00Z",
            pid="ev-new",
        )
        both = _search_result([existing, new_event])
        mock_runner_search.return_value = both

        first = run_concert_watches()
        assert first["alerts_sent"] == 1

        mock_send.reset_mock()
        second = run_concert_watches()

        assert second["alerts_sent"] == 0
        mock_send.assert_not_called()
