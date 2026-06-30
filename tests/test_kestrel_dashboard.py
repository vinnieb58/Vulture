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
        assert "Energy + HVAC Correlation — Latest Overlapping 24h" in text
        assert "Nest Collection" in text
        assert "Downstairs" in text or "House any" in text

    def test_kestrel_page_shows_nest_auth_failure_when_stale_and_error_file(
        self, client, tmp_path, monkeypatch
    ):
        status_path = tmp_path / "kestrel_status.json"
        db_path = tmp_path / "kestrel.db"
        history_path = tmp_path / "nest_history.jsonl"
        error_path = tmp_path / "kestrel_nest_error.json"
        _write_status(status_path)
        _seed_db(db_path)
        monkeypatch.setattr("kestrel_status.KESTREL_STATUS_PATH", status_path)
        monkeypatch.setattr("kestrel_metrics.KESTREL_DB_PATH", db_path)
        monkeypatch.setattr("nest_hvac_runtime.NEST_HISTORY_PATH", history_path)
        monkeypatch.setattr("nest_energy_correlation.NEST_HISTORY_PATH", history_path)
        monkeypatch.setattr("nest_collection_health.NEST_HISTORY_PATH", history_path)
        monkeypatch.setattr("nest_error_status.NEST_ERROR_PATH", error_path)

        from kestrel.nest_history import append_history_from_snapshot

        append_history_from_snapshot(
            {
                "updated_at": "2026-06-16T04:30:00+00:00",
                "thermostats": {
                    "downstairs": {"action": "COOLING", "online": True},
                    "upstairs": {"action": "OFF", "online": True},
                },
            },
            path=history_path,
            now=datetime(2026, 6, 16, 4, 30, tzinfo=ZoneInfo("UTC")),
        )
        error_path.write_text(
            json.dumps(
                {
                    "timestamp": "2026-06-16T05:10:00+00:00",
                    "error_type": "oauth",
                    "message": 'OAuth token request failed: {"error":"invalid_grant"}',
                    "last_success": "2026-06-16T04:30:00+00:00",
                }
            )
            + "\n",
            encoding="utf-8",
        )

        from nest_hvac_formatting import format_hvac_section as _format_hvac_section

        fixed_now = datetime(2026, 6, 16, 5, 10, tzinfo=ZoneInfo("UTC"))
        monkeypatch.setattr(
            dashboard_app,
            "format_hvac_section",
            lambda: _format_hvac_section(now=fixed_now),
        )

        response = self._stub_host(client).get("/kestrel")

        text = response.text
        assert response.status_code == 200
        assert "Nest auth failure" in text
        assert "Auth failure" in text
        assert "invalid_grant" not in text


def _build_tuya_status_snapshot(**overrides: object) -> dict:
    from kestrel.tuya_power import (
        METER_1_KEY,
        METER_2_KEY,
        build_tuya_power_snapshot,
        parse_dual_meter_dps,
    )

    fixture_dir = Path(__file__).resolve().parent / "fixtures"
    meter_1 = parse_dual_meter_dps(
        json.loads((fixture_dir / "tuya_vwifi_meter1_observed.json").read_text(encoding="utf-8")),
        meter_key=METER_1_KEY,
        source="local",
    )
    meter_2 = parse_dual_meter_dps(
        json.loads((fixture_dir / "tuya_vwifi_meter2_observed.json").read_text(encoding="utf-8")),
        meter_key=METER_2_KEY,
        source="local",
    )
    snapshot = build_tuya_power_snapshot(
        {METER_1_KEY: meter_1, METER_2_KEY: meter_2},
        updated_at="2026-06-27T12:00:00+00:00",
        source="local",
        limited=False,
    )
    snapshot.update(overrides)
    return snapshot


def _write_tuya_status(path: Path, **overrides: object) -> None:
    path.write_text(json.dumps(_build_tuya_status_snapshot(**overrides), indent=2) + "\n", encoding="utf-8")


def _write_tuya_history(path: Path, snapshot: dict, *, count: int = 3) -> None:
    from kestrel.tuya_power_history import build_history_record

    lines = []
    for index in range(count):
        record = dict(snapshot)
        record["updated_at"] = f"2026-06-27T11:{index:02d}:00+00:00"
        lines.append(json.dumps(build_history_record(record)))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


class TestTuyaPowerDashboardParsing:
    def test_read_tuya_power_status_parses_appliances(self, tmp_path: Path, monkeypatch) -> None:
        status_path = tmp_path / "kestrel_tuya_power_status.json"
        _write_tuya_status(status_path)
        monkeypatch.setattr("tuya_power_status.TUYA_STATUS_PATH", status_path)

        from tuya_power_status import read_tuya_power_status

        fixed_now = datetime(2026, 6, 27, 12, 1, tzinfo=ZoneInfo("UTC"))
        status = read_tuya_power_status(now=fixed_now)

        assert status["state"] == "online"
        assert len(status["appliances"]) == 4
        ac = next(item for item in status["appliances"] if item["key"] == "ac_compressor")
        assert ac["label"] == "AC compressor"
        assert ac["power_w"] == pytest.approx(2649.4)
        assert ac["voltage_v"] == pytest.approx(122.7)
        assert ac["energy_forward_kwh_inferred"] == pytest.approx(154.27)
        assert "raw_dps" not in ac

    def test_read_tuya_power_status_strips_raw_dps_from_snapshot(self, tmp_path: Path, monkeypatch) -> None:
        status_path = tmp_path / "kestrel_tuya_power_status.json"
        _write_tuya_status(status_path)
        monkeypatch.setattr("tuya_power_status.TUYA_STATUS_PATH", status_path)

        raw_text = status_path.read_text(encoding="utf-8")
        assert "raw_dps" in raw_text

        from tuya_power_status import read_tuya_power_status

        status = read_tuya_power_status(
            now=datetime(2026, 6, 27, 12, 1, tzinfo=ZoneInfo("UTC"))
        )
        for appliance in status["appliances"]:
            assert "raw_dps" not in appliance
            assert "raw_unknown" not in appliance

    def test_read_tuya_power_status_marks_stale_snapshot(self, tmp_path: Path, monkeypatch) -> None:
        status_path = tmp_path / "kestrel_tuya_power_status.json"
        _write_tuya_status(status_path, updated_at="2026-06-27T11:50:00+00:00")
        monkeypatch.setattr("tuya_power_status.TUYA_STATUS_PATH", status_path)

        from tuya_power_status import read_tuya_power_status

        status = read_tuya_power_status(
            now=datetime(2026, 6, 27, 12, 0, tzinfo=ZoneInfo("UTC"))
        )
        assert status["state"] == "stale"
        assert "stale" in status["headline"].lower()

    def test_read_tuya_power_history_builds_series(self, tmp_path: Path, monkeypatch) -> None:
        history_path = tmp_path / "kestrel_tuya_power_history.jsonl"
        snapshot = _build_tuya_status_snapshot()
        _write_tuya_history(history_path, snapshot, count=4)
        monkeypatch.setattr("tuya_power_history.TUYA_HISTORY_PATH", history_path)

        from tuya_power_history import build_appliance_power_series, read_tuya_power_history

        records = read_tuya_power_history(history_path)
        assert len(records) == 4

        series = build_appliance_power_series(
            records,
            hours=24,
            now=datetime(2026, 6, 27, 12, 0, tzinfo=ZoneInfo("UTC")),
        )
        assert series
        ac_series = next(item for item in series if item["key"] == "ac_compressor")
        assert len(ac_series["points"]) == 4
        assert ac_series["points"][0]["watts"] == pytest.approx(2649.4)


class TestTuyaPowerDashboardHTTP:
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

    def test_kestrel_page_includes_tuya_section_with_no_data(
        self, client, tmp_path, monkeypatch
    ):
        monkeypatch.setattr("kestrel_status.KESTREL_STATUS_PATH", tmp_path / "missing.json")
        monkeypatch.setattr("kestrel_metrics.KESTREL_DB_PATH", tmp_path / "missing.db")
        monkeypatch.setattr("tuya_power_status.TUYA_STATUS_PATH", tmp_path / "missing.json")
        monkeypatch.setattr("tuya_power_history.TUYA_HISTORY_PATH", tmp_path / "missing.jsonl")
        monkeypatch.setattr("tuya_power_error_status.TUYA_ERROR_PATH", tmp_path / "missing_error.json")

        response = self._stub_host(client).get("/kestrel")
        text = response.text

        assert response.status_code == 200
        assert "Tuya Appliance Power" in text
        assert "tuya_power_charts.js" in text
        assert "AC compressor" in text

    def test_kestrel_page_renders_appliance_watts(self, client, tmp_path, monkeypatch):
        status_path = tmp_path / "kestrel_tuya_power_status.json"
        history_path = tmp_path / "kestrel_tuya_power_history.jsonl"
        _write_tuya_status(status_path)
        _write_tuya_history(history_path, _build_tuya_status_snapshot(), count=2)

        monkeypatch.setattr("kestrel_status.KESTREL_STATUS_PATH", tmp_path / "kestrel_status.json")
        monkeypatch.setattr("kestrel_metrics.KESTREL_DB_PATH", tmp_path / "missing.db")
        monkeypatch.setattr("tuya_power_status.TUYA_STATUS_PATH", status_path)
        monkeypatch.setattr("tuya_power_history.TUYA_HISTORY_PATH", history_path)
        monkeypatch.setattr("tuya_power_error_status.TUYA_ERROR_PATH", tmp_path / "missing_error.json")

        fixed_now = datetime(2026, 6, 27, 12, 1, tzinfo=ZoneInfo("UTC"))
        from tuya_power_formatting import format_tuya_power_section as _format_tuya_power_section

        monkeypatch.setattr(
            dashboard_app,
            "format_tuya_power_section",
            lambda: _format_tuya_power_section(now=fixed_now),
        )

        response = self._stub_host(client).get("/kestrel")
        text = response.text

        assert response.status_code == 200
        assert "Tuya Appliance Power" in text
        assert "AC compressor" in text
        assert "Furnace / air handler" in text
        assert "Dryer" in text
        assert "Dishwasher" in text
        assert "2650 W" in text or "2649 W" in text
        assert "chart-data-tuya-power-1h" in text
        assert "chart-data-tuya-power-24h" in text
        assert "raw_dps" not in text
        assert "raw_unknown" not in text

    def test_kestrel_page_shows_tuya_stale_warning_with_error_sidecar(
        self, client, tmp_path, monkeypatch
    ):
        status_path = tmp_path / "kestrel_tuya_power_status.json"
        error_path = tmp_path / "kestrel_tuya_power_error.json"
        _write_tuya_status(status_path, updated_at="2026-06-27T11:50:00+00:00")
        error_path.write_text(
            json.dumps(
                {
                    "timestamp": "2026-06-27T12:05:00+00:00",
                    "error_type": "local",
                    "message": "Local read failed for meter_2: connection timeout",
                    "last_success": "2026-06-27T11:50:00+00:00",
                }
            )
            + "\n",
            encoding="utf-8",
        )

        monkeypatch.setattr("kestrel_status.KESTREL_STATUS_PATH", tmp_path / "kestrel_status.json")
        monkeypatch.setattr("kestrel_metrics.KESTREL_DB_PATH", tmp_path / "missing.db")
        monkeypatch.setattr("tuya_power_status.TUYA_STATUS_PATH", status_path)
        monkeypatch.setattr("tuya_power_history.TUYA_HISTORY_PATH", tmp_path / "missing.jsonl")
        monkeypatch.setattr("tuya_power_error_status.TUYA_ERROR_PATH", error_path)

        fixed_now = datetime(2026, 6, 27, 12, 5, tzinfo=ZoneInfo("UTC"))
        from tuya_power_formatting import format_tuya_power_section as _format_tuya_power_section

        monkeypatch.setattr(
            dashboard_app,
            "format_tuya_power_section",
            lambda: _format_tuya_power_section(now=fixed_now),
        )

        response = self._stub_host(client).get("/kestrel")
        text = response.text

        assert response.status_code == 200
        assert "Tuya power stale" in text
        assert "Stale" in text
        assert "connection timeout" not in text
        assert "meter_2" not in text
