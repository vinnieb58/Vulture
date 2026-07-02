"""Tests for concert watch status snapshot and dashboard visibility."""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
DASHBOARD_DIR = REPO_ROOT / "dashboard"
SCRIPTS_DIR = REPO_ROOT / "scripts"
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(DASHBOARD_DIR))
sys.path.insert(0, str(SCRIPTS_DIR))

from engine.concerts.repository import create_watch, init_concert_tables, pause_watch  # noqa: E402
from engine.concerts.search import SearchCriteria  # noqa: E402
from engine.concerts.status_snapshot import (  # noqa: E402
    build_status_snapshot,
    read_status_snapshot,
    write_status_snapshot,
)
from host_status import ServiceStatus  # noqa: E402
from pelican.sqlite_backup import verify_concert_tables  # noqa: E402
import concert_status  # noqa: E402


@pytest.fixture
def concert_db(tmp_path, monkeypatch):
    db_path = tmp_path / "vulture.db"
    monkeypatch.setattr("engine.database.DB_PATH", db_path)
    init_concert_tables()
    return db_path


class TestConcertStatusSnapshot:
    def test_build_snapshot_tracks_success_and_error_timestamps(self):
        first = build_status_snapshot(
            {"watches_checked": 1, "events_found": 2, "alerts_sent": 0, "errors": []},
            previous=None,
        )
        assert first["last_success_at"] is not None
        assert first["last_error_at"] is None

        second = build_status_snapshot(
            {"watches_checked": 1, "events_found": 0, "alerts_sent": 0, "errors": ["boom"]},
            previous=first,
        )
        assert second["last_error_at"] is not None
        assert second["last_success_at"] == first["last_success_at"]

    def test_write_and_read_status_file(self, tmp_path, concert_db):
        status_path = tmp_path / "concert_watch_status.json"
        create_watch(SearchCriteria(artist_query="Disturbed", area="houston", days_forward=180))

        snapshot = write_status_snapshot(
            {"watches_checked": 1, "events_found": 0, "alerts_sent": 0, "errors": []},
            path=status_path,
        )

        assert status_path.is_file()
        loaded = read_status_snapshot(status_path)
        assert loaded["watches_checked"] == snapshot["watches_checked"]
        assert loaded["active_watch_count"] == 1
        assert loaded["paused_watch_count"] == 0

    def test_write_includes_paused_watch_counts(self, tmp_path, concert_db):
        status_path = tmp_path / "status.json"
        watch = create_watch(SearchCriteria(artist_query="A", area="houston", days_forward=90))
        pause_watch(watch.id)

        snapshot = write_status_snapshot(
            {"watches_checked": 0, "events_found": 0, "alerts_sent": 0, "errors": []},
            path=status_path,
        )
        assert snapshot["active_watch_count"] == 0
        assert snapshot["paused_watch_count"] == 1


class TestPelicanConcertTableVerify:
    def _init_concert_schema(self, db_path: Path) -> None:
        conn = sqlite3.connect(db_path)
        conn.executescript(
            """
            CREATE TABLE concert_watches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                artist_query TEXT,
                genre TEXT,
                area TEXT,
                city TEXT,
                state TEXT,
                radius_miles INTEGER,
                days_forward INTEGER NOT NULL DEFAULT 180,
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL
            );
            CREATE TABLE concert_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source TEXT NOT NULL,
                provider_event_id TEXT NOT NULL,
                artist_or_title TEXT NOT NULL,
                venue TEXT,
                city TEXT,
                state TEXT,
                starts_at TEXT,
                ticket_url TEXT,
                genre_or_classification TEXT,
                event_dedupe_key TEXT NOT NULL,
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL
            );
            CREATE TABLE concert_alerts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                watch_id INTEGER NOT NULL,
                event_dedupe_key TEXT NOT NULL,
                alerted_at TEXT NOT NULL
            );
            INSERT INTO concert_watches (artist_query, area, days_forward, active, created_at)
            VALUES ('A', 'houston', 180, 1, '2026-01-01T00:00:00+00:00');
            """
        )
        conn.commit()
        conn.close()

    def test_verify_counts_concert_tables(self, tmp_path: Path) -> None:
        db = tmp_path / "vulture.db"
        self._init_concert_schema(db)
        result = verify_concert_tables(db)
        assert result.ok
        assert result.counts["concert_watches"] == 1
        assert result.counts["concert_events"] == 0
        assert result.counts["concert_alerts"] == 0

    def test_verify_skips_when_tables_not_initialized(self, tmp_path: Path) -> None:
        db = tmp_path / "vulture.db"
        conn = sqlite3.connect(db)
        conn.execute("CREATE TABLE hunts (id INTEGER PRIMARY KEY)")
        conn.commit()
        conn.close()
        result = verify_concert_tables(db)
        assert result.ok
        assert "not initialized" in result.message.lower()


class TestConcertDashboardStatus:
    def test_build_concert_card_uses_snapshot_cycle(self, tmp_path, monkeypatch):
        status_path = tmp_path / "concert_watch_status.json"
        status_path.write_text(
            json.dumps(
                {
                    "checked_at": "2026-07-01T12:00:00Z",
                    "watches_checked": 2,
                    "events_found": 5,
                    "alerts_sent": 1,
                    "errors": [],
                    "active_watch_count": 2,
                    "paused_watch_count": 1,
                }
            )
            + "\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(concert_status, "CONCERT_STATUS_PATH", status_path)

        timer_svc = ServiceStatus(
            "vulture-concert-watches timer",
            "vulture-concert-watches.timer",
            "active",
            "enabled",
        )
        with patch("concert_status._check_service", return_value=timer_svc):
            with patch("concert_status._check_concert_service") as mock_service:
                mock_service.return_value = ServiceStatus(
                    "vulture-concert-watches service",
                    "vulture-concert-watches.service",
                    "inactive",
                    "static",
                )
                with patch("concert_status._list_timer_next_run", return_value="Thu 2026-07-02 12:00:00 UTC"):
                    with patch("concert_status._journal_lines", return_value=[]):
                        with patch("concert_status._read_log_lines", return_value=[]):
                            with patch(
                                "concert_status._provider_configured",
                                return_value={"ticketmaster": True, "seatgeek": False},
                            ):
                                card = concert_status.build_concert_card(
                                    {
                                        "concert_counts": {
                                            "active": 2,
                                            "paused": 1,
                                            "recent_events": 3,
                                            "recent_alerts": 1,
                                        }
                                    }
                                )

        assert card["status"] == "OK"
        assert card["active_watches"] == 2
        assert card["paused_watches"] == 1
        assert card["cycle"]["events_found"] == 5
        assert card["providers"]["ticketmaster"] is True
        assert card["providers"]["seatgeek"] is False

    def test_timer_missing_is_fail(self):
        timer_svc = ServiceStatus(
            "vulture-concert-watches timer",
            None,
            "not found",
            "not configured",
        )
        with patch("concert_status._check_service", return_value=timer_svc):
            with patch("concert_status._check_concert_service") as mock_service:
                mock_service.return_value = ServiceStatus(
                    "vulture-concert-watches service",
                    None,
                    "not found",
                    "not configured",
                )
                with patch("concert_status._list_timer_next_run", return_value=None):
                    with patch("concert_status._journal_lines", return_value=[]):
                        with patch("concert_status._read_log_lines", return_value=[]):
                            result = concert_status.evaluate_concert_watch_timer()

        assert result["status"] == "FAIL"
        assert result["warning"]
