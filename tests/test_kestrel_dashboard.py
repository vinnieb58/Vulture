"""HTTP and integration tests for the Kestrel dashboard detail page."""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
DASHBOARD_DIR = ROOT / "dashboard"
sys.path.insert(0, str(DASHBOARD_DIR))

import app as dashboard_app  # noqa: E402
from kestrel.config import PROVIDER_SMART_METER_TEXAS
from kestrel.models import EnergyInterval
from kestrel.storage import init_db, upsert_intervals


CHICAGO = ZoneInfo("America/Chicago")
FIXED_NOW = datetime(2026, 6, 17, 12, 0, tzinfo=CHICAGO)


def _seed_db(db_path: Path) -> None:
    rows = [
        EnergyInterval(
            provider=PROVIDER_SMART_METER_TEXAS,
            start_ts="2026-06-15T18:00:00+00:00",
            end_ts="2026-06-15T18:15:00+00:00",
            kwh=2.5,
        ),
        EnergyInterval(
            provider=PROVIDER_SMART_METER_TEXAS,
            start_ts="2026-06-16T05:00:00+00:00",
            end_ts="2026-06-16T05:15:00+00:00",
            kwh=1.5,
        ),
    ]
    init_db(db_path)
    upsert_intervals(db_path, rows)


def _write_status(path: Path, **overrides: object) -> None:
    payload = {
        "generated_at": "2026-06-16T12:00:00+00:00",
        "interval_count": 96,
        "range_start": "2026-06-09T00:00:00+00:00",
        "range_end": "2026-06-16T00:00:00+00:00",
        "total_kwh": 42.5,
        "missing_interval_count": 3,
    }
    payload.update(overrides)
    path.write_text(json.dumps(payload), encoding="utf-8")


class TestKestrelDashboardHTTP:
    @pytest.fixture
    def client(self, tmp_path, monkeypatch):
        db_path = tmp_path / "missing.db"
        log_path = tmp_path / "missing.log"
        monkeypatch.setattr(dashboard_app, "DB_PATH", db_path)
        monkeypatch.setattr(dashboard_app, "LOG_PATH", log_path)
        monkeypatch.setattr("db_readers.DB_PATH", db_path)
        monkeypatch.setattr("log_readers.LOG_PATH", log_path)
        monkeypatch.setattr("vulture_runtime.LOG_PATH", log_path)
        return TestClient(dashboard_app.app)

    def _stub_host(self, client):
        with patch("host_status.run_host_command", return_value=(False, "unavailable")):
            with patch("host_status.run_systemctl", return_value=(False, "unavailable")):
                return client

    def test_kestrel_page_returns_200_with_no_data(self, client, tmp_path, monkeypatch):
        monkeypatch.setattr("kestrel_status.KESTREL_STATUS_PATH", tmp_path / "missing.json")
        monkeypatch.setattr("kestrel_metrics.KESTREL_DB_PATH", tmp_path / "missing.db")
        response = self._stub_host(client).get("/kestrel")
        assert response.status_code == 200
        assert "Kestrel Energy" in response.text
        assert "kestrel_charts.js" in response.text

    def test_kestrel_page_returns_200_with_fixture_data(self, client, tmp_path, monkeypatch):
        status_path = tmp_path / "kestrel_status.json"
        db_path = tmp_path / "kestrel.db"
        _write_status(status_path)
        _seed_db(db_path)
        monkeypatch.setattr("kestrel_status.KESTREL_STATUS_PATH", status_path)
        monkeypatch.setattr("kestrel_metrics.KESTREL_DB_PATH", db_path)
        response = self._stub_host(client).get("/kestrel")

        assert response.status_code == 200
        text = response.text
        assert "Daily usage — last 30 days" in text
        assert "Top 10 peak 15-minute intervals" in text
        assert "4.00" in text
        assert "Mon 6/15, 1:00–1:15 PM" in text
        assert "chart-data-daily-30" in text

    def test_nest_home_card_no_longer_renders_top_intervals_or_full_daily_totals(
        self, client, tmp_path, monkeypatch
    ):
        status_path = tmp_path / "kestrel_status.json"
        _write_status(
            status_path,
            top_intervals=[
                {
                    "start_ts": "2026-06-15T18:00:00+00:00",
                    "end_ts": "2026-06-15T18:15:00+00:00",
                    "kwh": 2.5,
                    "estimated_peak_kw": 10.0,
                }
            ],
            daily_totals={"2026-06-15": 6.25, "2026-06-16": 5.0},
        )
        monkeypatch.setattr("kestrel_status.KESTREL_STATUS_PATH", status_path)
        monkeypatch.setattr("kestrel_metrics.KESTREL_DB_PATH", tmp_path / "missing.db")
        response = self._stub_host(client).get("/")
        text = response.text
        assert "Top intervals" not in text
        assert "Daily totals" not in text

    def test_nest_home_card_renders_new_summary_fields(self, client, tmp_path, monkeypatch):
        status_path = tmp_path / "kestrel_status.json"
        db_path = tmp_path / "kestrel.db"
        _write_status(status_path)
        _seed_db(db_path)
        monkeypatch.setattr("kestrel_status.KESTREL_STATUS_PATH", status_path)
        monkeypatch.setattr("kestrel_metrics.KESTREL_DB_PATH", db_path)
        response = self._stub_host(client).get("/")

        text = response.text
        assert "Peak interval (7 days)" in text
        assert "Avg daily (last 2 days)" in text
        assert "Recent daily totals" in text
        assert "Energy details" in text
        assert "/kestrel" in text

    def test_pages_do_not_render_sensitive_fields(self, client, tmp_path, monkeypatch):
        status_path = tmp_path / "kestrel_status.json"
        _write_status(
            status_path,
            account_id="secret-account",
            meter_id_hash="abc123hash",
            esiid="123456789012345678",
            db_path="/app/data/kestrel/kestrel.db",
            raw_source="csv:/secret/path.csv",
        )
        monkeypatch.setattr("kestrel_status.KESTREL_STATUS_PATH", status_path)
        monkeypatch.setattr("kestrel_metrics.KESTREL_DB_PATH", tmp_path / "missing.db")

        for path in ("/", "/kestrel"):
            response = self._stub_host(client).get(path)
            text = response.text
            for forbidden in (
                "secret-account",
                "abc123hash",
                "123456789012345678",
                "kestrel.db",
                "secret/path.csv",
            ):
                assert forbidden not in text

    def test_kestrel_page_renders_hvac_sections(self, client, tmp_path, monkeypatch):
        status_path = tmp_path / "kestrel_status.json"
        db_path = tmp_path / "kestrel.db"
        history_path = tmp_path / "nest_history.jsonl"
        _write_status(status_path)
        _seed_db(db_path)
        monkeypatch.setattr("kestrel_status.KESTREL_STATUS_PATH", status_path)
        monkeypatch.setattr("kestrel_metrics.KESTREL_DB_PATH", db_path)
        monkeypatch.setattr("nest_hvac_runtime.NEST_HISTORY_PATH", history_path)
        monkeypatch.setattr("nest_energy_correlation.NEST_HISTORY_PATH", history_path)

        from kestrel.nest_history import append_history_from_snapshot

        append_history_from_snapshot(
            {
                "updated_at": "2026-06-16T05:05:00+00:00",
                "thermostats": {
                    "downstairs": {
                        "temperature": 72,
                        "humidity": 65,
                        "mode": "COOL",
                        "action": "COOLING",
                        "setpoint": 71,
                        "online": True,
                    },
                    "upstairs": {
                        "temperature": 77,
                        "humidity": 65,
                        "mode": "MANUAL_ECO",
                        "action": "OFF",
                        "setpoint": 76,
                        "online": True,
                    },
                },
            },
            path=history_path,
            now=datetime(2026, 6, 16, 5, 5, tzinfo=ZoneInfo("UTC")),
        )

        response = self._stub_host(client).get("/kestrel")
        text = response.text
        assert "HVAC Runtime" in text
        assert "Energy + HVAC Correlation" in text
        assert "Nest Collection" in text
        assert "Downstairs" in text or "House any" in text
