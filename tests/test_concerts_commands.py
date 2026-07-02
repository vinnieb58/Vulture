"""Tests for concert repository and command router."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from engine.concerts.command_router import dispatch_concert
from engine.concerts.dedupe import MergedConcertEvent
from engine.concerts.repository import (
    alert_exists,
    create_watch,
    init_concert_tables,
    list_watches,
    record_alert,
    upsert_provider_events,
)
from engine.concerts.search import SearchCriteria, SearchResult
from engine.concerts.stats import SearchStats
from engine.database import get_connection


@pytest.fixture()
def concert_db(tmp_path, monkeypatch):
    db_path = tmp_path / "test_vulture.db"
    monkeypatch.setattr("engine.database.DB_PATH", db_path)
    init_concert_tables()
    yield db_path


class TestRepository:
    def test_create_and_list_watch(self, concert_db):
        criteria = SearchCriteria(
            artist_query="Shinedown",
            area="houston",
            days_forward=365,
        )
        watch = create_watch(criteria)
        watches = list_watches()
        assert len(watches) == 1
        assert watches[0].artist_query == "Shinedown"
        assert watch.id == watches[0].id

    def test_alert_dedupe_by_event_key(self, concert_db):
        watch = create_watch(SearchCriteria(artist_query="Disturbed", area="houston"))
        key = "event|abc123"
        assert not alert_exists(watch.id, key)
        record_alert(watch.id, key)
        assert alert_exists(watch.id, key)

    def test_upsert_events(self, concert_db):
        merged = MergedConcertEvent(
            artist_or_title="Test Band",
            venue="Venue",
            city="Houston",
            state="TX",
            starts_at="2026-09-01T20:00:00Z",
            ticket_url="https://example.com/t",
            genre_or_classification="Rock",
            event_dedupe_key="event|test",
            sources=["ticketmaster"],
            provider_events=[],
        )
        from engine.concerts.probe_util import build_normalized_event

        pe = build_normalized_event(
            source="ticketmaster",
            provider_event_id="ev-1",
            artist_or_title="Test Band",
            venue="Venue",
            starts_at="2026-09-01T20:00:00Z",
        )
        merged.provider_events = [pe]
        merged.event_dedupe_key = pe.event_dedupe_key
        upsert_provider_events([merged])

        with get_connection() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS c FROM concert_events WHERE event_dedupe_key = ?",
                (pe.event_dedupe_key,),
            ).fetchone()
        assert row["c"] == 1


class TestCommandRouter:
    def test_help(self):
        result = dispatch_concert("help", {})
        assert result.success
        assert "/concert search" in result.message

    def test_test_command(self):
        result = dispatch_concert("test", {})
        assert result.success
        assert "Sample query validation" in result.message

    @patch("engine.concerts.command_router.search_concerts")
    def test_search_mocked(self, mock_search, concert_db):
        mock_search.return_value = SearchResult(
            events=[],
            provider_notes=[],
            queries_run=2,
            stats=SearchStats(),
        )
        result = dispatch_concert(
            "search",
            {"query": 'artist:"Three Days Grace" city:"Houston" days:180'},
        )
        assert result.success
        mock_search.assert_called_once()

    def test_unknown_command(self):
        result = dispatch_concert("fly", {})
        assert not result.success
