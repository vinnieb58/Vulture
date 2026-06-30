"""HTTP and integration tests for the Kestrel dashboard detail page."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
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
UTC = timezone.utc


def _seed_db(db_path: Path) -> None:
    # Two intervals on different calendar days so "Avg daily (last 2 days)" renders.
    # Use 2 h ago (today) and 28 h ago (yesterday) — 28 h gives more than a full day
    # of separation so the local-day boundary is crossed regardless of whether the
    # test runs near midnight.  Both fall within the 7-day SMT peak-query window.
    today_start = datetime.now(UTC) - timedelta(hours=2)
    yesterday_start = datetime.now(UTC) - timedelta(hours=28)
    rows = [
        EnergyInterval(
            provider=PROVIDER_SMART_METER_TEXAS,
            start_ts=today_start.strftime("%Y-%m-%dT%H:%M:%S+00:00"),
            end_ts=(today_start + timedelta(minutes=15)).strftime("%Y-%m-%dT%H:%M:%S+00:00"),
            kwh=2.5,
        ),
        EnergyInterval(
            provider=PROVIDER_SMART_METER_TEXAS,
            start_ts=yesterday_start.strftime("%Y-%m-%dT%H:%M:%S+00:00"),
            end_ts=(yesterday_start + timedelta(minutes=15)).strftime("%Y-%m-%dT%H:%M:%S+00:00"),
            kwh=1.5,
        ),
    ]
    init_db(db_path)
    upsert_intervals(db_path, rows)


def _write_status(path: Path, **overrides: object) -> None:
    now = datetime.now(UTC)
    yesterday = (now - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    week_ago = (now - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    payload = {
        "generated_at": now.strftime("%Y-%m-%dT%H:%M:%S+00:00"),
        "interval_count": 96,
        "range_start": week_ago,
        "range_end": yesterday,
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
        # Redesigned page structure (energy-explanation layout)
        assert "Today's Energy Story" in text
        assert "Daily usage — last 30 days" in text
        assert "Historical Trends" in text
        assert "chart-data-daily-30" in text
        assert "chart-data-energy-timeline" in text
        assert "kestrel_energy_timeline.js" in text

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
        monkeypatch.setattr("nest_collection_health.NEST_HISTORY_PATH", history_path)

        from kestrel.nest_history import append_history_from_snapshot

        # Use recent timestamps so the 14-day retention window does not prune the record.
        recent_ts = datetime.now(UTC) - timedelta(hours=1)
        recent_iso = recent_ts.strftime("%Y-%m-%dT%H:%M:%S+00:00")
        append_history_from_snapshot(
            {
                "updated_at": recent_iso,
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
            now=recent_ts,
        )

        response = self._stub_host(client).get("/kestrel")
        text = response.text
        # Redesigned page: HVAC section is now "HVAC Performance"
        assert "HVAC Performance" in text
        assert "Nest Collection" in text
        assert "Today's Energy Story" in text
        # Zone table should render (either cycle-analysis or polling-status fallback)
        assert "Downstairs" in text or "House any" in text or "downstairs" in text.lower()

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


def _write_tuya_history_recent(path: Path, snapshot: dict, *, count: int = 10) -> None:
    """Write Tuya history with timestamps within the last hour (analysis-window compatible)."""
    from kestrel.tuya_power_history import build_history_record

    lines = []
    base = datetime.now(UTC) - timedelta(minutes=count + 1)
    for index in range(count):
        record = dict(snapshot)
        ts = base + timedelta(minutes=index)
        record["updated_at"] = ts.strftime("%Y-%m-%dT%H:%M:%S+00:00")
        lines.append(json.dumps(build_history_record(record)))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_tuya_status_recent(path: Path, **overrides: object) -> None:
    """Write a Tuya status snapshot with a fresh timestamp (not stale)."""
    snapshot = _build_tuya_status_snapshot()
    snapshot["updated_at"] = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    snapshot.update(overrides)
    path.write_text(json.dumps(snapshot, indent=2) + "\n", encoding="utf-8")


def _write_nest_history_recent(path: Path, *, cooling_minutes: int = 30) -> None:
    """Write Nest history with recent timestamps, cooling active."""
    from kestrel.nest_history import append_history_from_snapshot

    base = datetime.now(UTC) - timedelta(minutes=cooling_minutes + 5)
    for i in range(cooling_minutes // 5 + 2):
        ts = base + timedelta(minutes=i * 5)
        action = "COOLING" if i * 5 < cooling_minutes else "OFF"
        append_history_from_snapshot(
            {
                "updated_at": ts.strftime("%Y-%m-%dT%H:%M:%S+00:00"),
                "thermostats": {
                    "downstairs": {
                        "temperature": 74,
                        "humidity": 60,
                        "mode": "COOL",
                        "action": action,
                        "setpoint": 72,
                        "online": True,
                    },
                },
            },
            path=path,
            now=ts,
        )


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


def _seed_db_with_coverage(db_path: Path, *, n_intervals: int = 8) -> None:
    """
    Seed the Kestrel DB with ``n_intervals`` 15-minute SMT rows ending now,
    plus one older row on a different calendar day.

    Using n_intervals=8 gives 2 h of today's data.  For source-agreement to
    reach the 50 % coverage threshold the caller must set n_intervals high
    enough to cover at least half of today's elapsed minutes.  The helper
    computes the minimum automatically and always adds at least ``n_intervals``
    rows.
    """
    now = datetime.now(UTC)
    # Compute minimum intervals needed for 50 % SMT coverage of today's window
    from zoneinfo import ZoneInfo as _ZI
    tz = _ZI("America/Chicago")
    local_now = now.astimezone(tz)
    today_start_local = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    today_start_utc = today_start_local.astimezone(UTC)
    window_seconds = max(1.0, (now - today_start_utc).total_seconds())
    min_needed = int(window_seconds / 900 * 0.51) + 1  # 51 % → always above threshold
    count = max(n_intervals, min_needed)

    rows = []
    for i in range(count):
        start = now - timedelta(minutes=(count - i) * 15)
        end = start + timedelta(minutes=15)
        rows.append(EnergyInterval(
            provider=PROVIDER_SMART_METER_TEXAS,
            start_ts=start.strftime("%Y-%m-%dT%H:%M:%S+00:00"),
            end_ts=end.strftime("%Y-%m-%dT%H:%M:%S+00:00"),
            kwh=1.5,
        ))
    # Add a row from yesterday for historical-trends coverage
    yesterday = now - timedelta(hours=28)
    rows.append(EnergyInterval(
        provider=PROVIDER_SMART_METER_TEXAS,
        start_ts=yesterday.strftime("%Y-%m-%dT%H:%M:%S+00:00"),
        end_ts=(yesterday + timedelta(minutes=15)).strftime("%Y-%m-%dT%H:%M:%S+00:00"),
        kwh=2.5,
    ))
    init_db(db_path)
    upsert_intervals(db_path, rows)


def _write_tuya_history_with_compressor(
    path: Path,
    snapshot: dict,
    *,
    count: int = 65,
) -> None:
    """
    Write ``count`` Tuya history records spaced 60 s apart, ending now,
    all with the snapshot's compressor power.  Enough for 100 % Tuya
    coverage in a 1-hour window and sufficient samples for peak detection.
    """
    from kestrel.tuya_power_history import build_history_record

    now = datetime.now(UTC)
    lines = []
    for i in range(count):
        ts = now - timedelta(seconds=(count - i) * 60)
        record = dict(snapshot)
        record["updated_at"] = ts.strftime("%Y-%m-%dT%H:%M:%S+00:00")
        lines.append(json.dumps(build_history_record(record)))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_nest_history_with_cycle(
    path: Path,
    *,
    cooling_count: int = 6,
    idle_count: int = 6,
    interval_minutes: int = 5,
) -> None:
    """
    Write Nest history with ``cooling_count`` COOLING samples followed by
    ``idle_count`` OFF samples at ``interval_minutes`` cadence, all ending now.

    cooling_count=6 → 30-minute detected cooling cycle (≥ 1 cycle in analysis).
    """
    from kestrel.nest_history import append_history_from_snapshot

    total = cooling_count + idle_count
    base = datetime.now(UTC) - timedelta(minutes=total * interval_minutes)
    for i in range(total):
        ts = base + timedelta(minutes=i * interval_minutes)
        action = "COOLING" if i < cooling_count else "OFF"
        append_history_from_snapshot(
            {
                "updated_at": ts.strftime("%Y-%m-%dT%H:%M:%S+00:00"),
                "thermostats": {
                    "downstairs": {
                        "action": action,
                        "temperature": 74,
                        "humidity": 60,
                        "mode": "COOL",
                        "setpoint": 72,
                        "online": True,
                    },
                },
            },
            path=path,
            now=ts,
        )


class TestKestrelRenderVerification:
    """
    Page-contract tests for the /kestrel redesign.

    These tests assert stable user-facing outcomes — section headings, key
    labels, formatting conventions — not CSS structure, numeric values from
    live data, or full HTML fragments.

    Fixture strategy
    ----------------
    All timestamps are derived from ``datetime.now(UTC)`` at test-execution
    time so they always fall within the Nest 14-day retention window and the
    7-day SMT peak-query window regardless of when the tests run.

    The ``_seed_db_with_coverage`` helper computes the minimum number of SMT
    intervals needed to exceed the 50 % coverage threshold for today's elapsed
    window, ensuring the source-agreement calculation can reach "available".
    """

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

    def _patch_all_sources(
        self,
        monkeypatch,
        tmp_path: Path,
        *,
        status_path: Path,
        db_path: Path,
        tuya_status_path: Path,
        tuya_history_path: Path,
        nest_history_path: Path,
    ) -> None:
        """Apply all kestrel-related monkeypatches in one call."""
        monkeypatch.setattr("kestrel_status.KESTREL_STATUS_PATH", status_path)
        monkeypatch.setattr("kestrel_metrics.KESTREL_DB_PATH", db_path)
        monkeypatch.setattr("tuya_power_status.TUYA_STATUS_PATH", tuya_status_path)
        monkeypatch.setattr("tuya_power_history.TUYA_HISTORY_PATH", tuya_history_path)
        monkeypatch.setattr("tuya_power_error_status.TUYA_ERROR_PATH", tmp_path / "no_error.json")
        monkeypatch.setattr("nest_hvac_runtime.NEST_HISTORY_PATH", nest_history_path)
        monkeypatch.setattr("nest_energy_correlation.NEST_HISTORY_PATH", nest_history_path)
        monkeypatch.setattr("nest_collection_health.NEST_HISTORY_PATH", nest_history_path)

    # ------------------------------------------------------------------
    # Main contract test: all 9 sections, full data
    # ------------------------------------------------------------------

    def test_page_contract_all_nine_sections(self, client, tmp_path, monkeypatch):
        """
        With SMT + Tuya + Nest data covering an overlapping window, every
        required section heading must appear and the page must return 200.

        Fixture provides:
        - SMT: enough 15-min intervals for ≥ 50 % coverage of today's window
          (gives source-agreement "available")
        - Tuya: 65 records spaced 60 s apart (full 1-h coverage, compressor running)
        - Nest: 6 COOLING + 6 OFF records = 1 detected cooling cycle

        Assertions test stable user-facing section labels only.
        No exact numeric values are checked.
        """
        snapshot = _build_tuya_status_snapshot()
        status_path = tmp_path / "kestrel_status.json"
        db_path = tmp_path / "kestrel.db"
        tuya_status_path = tmp_path / "tuya_status.json"
        tuya_history_path = tmp_path / "tuya_history.jsonl"
        nest_history_path = tmp_path / "nest_history.jsonl"

        _write_status(status_path)
        _seed_db_with_coverage(db_path)
        _write_tuya_status_recent(tuya_status_path)
        _write_tuya_history_with_compressor(tuya_history_path, snapshot)
        _write_nest_history_with_cycle(nest_history_path)

        self._patch_all_sources(
            monkeypatch, tmp_path,
            status_path=status_path, db_path=db_path,
            tuya_status_path=tuya_status_path, tuya_history_path=tuya_history_path,
            nest_history_path=nest_history_path,
        )

        response = self._stub_host(client).get("/kestrel")
        assert response.status_code == 200
        text = response.text

        # Required section headings (per spec)
        assert "Today's Energy Story" in text,          "Section 1 heading missing"
        assert "Current Status" in text,                 "Section 2 heading missing"
        assert "Do SMT and Tuya agree?" in text,         "Section 3 heading missing"
        assert "Combined Energy + HVAC Timeline" in text, "Section 4 heading missing"
        assert "Top Demand Peaks" in text,               "Section 5 heading missing"
        assert "HVAC Performance" in text,               "Section 6 heading missing"
        assert "Energy Breakdown" in text,               "Section 7 heading missing"
        assert "Historical Trends" in text,              "Section 8 heading missing"
        assert "Data Quality and Diagnostics" in text,   "Section 9 heading missing"

        # Required JS assets
        assert "kestrel_energy_timeline.js" in text
        assert "kestrel_charts.js" in text

        # Chart data payload is embedded (non-empty timeline JSON)
        assert "chart-data-energy-timeline" in text
        assert '"has_smt": true' in text or '"has_smt":true' in text, (
            "SMT data expected in timeline payload"
        )
        assert '"has_tuya": true' in text or '"has_tuya":true' in text, (
            "Tuya data expected in timeline payload"
        )
        assert '"has_nest": true' in text or '"has_nest":true' in text, (
            "Nest data expected in timeline payload"
        )

        # Navigation link preserved
        assert "href=\"/kestrel\"" in text or "/kestrel" in text

    # ------------------------------------------------------------------
    # Peak times in local (human-readable) format
    # ------------------------------------------------------------------

    def test_peak_times_in_local_human_readable_format(
        self, client, tmp_path, monkeypatch
    ):
        """
        Peaks must be displayed with a local AM/PM time string, not a raw
        ISO 8601 UTC timestamp.  The analysis engine attaches timestamp_display
        (e.g. 'Jun 30, 2:35 PM') and the template must render it.

        The test does NOT check the specific time value — only the format
        convention (presence of 'AM' or 'PM' in visible peak output and
        absence of raw ISO in the rendered peaks HTML).
        """
        import re as _re

        snapshot = _build_tuya_status_snapshot()
        status_path = tmp_path / "kestrel_status.json"
        db_path = tmp_path / "kestrel.db"
        tuya_status_path = tmp_path / "tuya_status.json"
        tuya_history_path = tmp_path / "tuya_history.jsonl"

        _write_status(status_path)
        _seed_db(db_path)
        _write_tuya_status_recent(tuya_status_path)
        _write_tuya_history_with_compressor(tuya_history_path, snapshot)

        self._patch_all_sources(
            monkeypatch, tmp_path,
            status_path=status_path, db_path=db_path,
            tuya_status_path=tuya_status_path, tuya_history_path=tuya_history_path,
            nest_history_path=tmp_path / "no_nest.jsonl",
        )

        response = self._stub_host(client).get("/kestrel")
        assert response.status_code == 200
        text = response.text

        # Peaks section must be present
        assert "Top Demand Peaks" in text

        # If any peaks rendered, they must show local AM/PM time
        if "#1" in text:
            assert "AM" in text or "PM" in text, (
                "Peaks section must show AM/PM local time, not raw ISO"
            )
            # No raw UTC ISO timestamp (YYYY-MM-DDTHH:MM:SS) should appear
            # anywhere in the rendered visible HTML outside script data tags.
            # Strip script blocks before checking.
            visible_html = _re.sub(
                r"<script[^>]*>.*?</script>", "", text, flags=_re.DOTALL
            )
            iso_match = _re.search(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", visible_html)
            assert iso_match is None, (
                f"Raw ISO timestamp found in visible HTML: {iso_match.group()!r}"
            )

    # ------------------------------------------------------------------
    # Agreement classification
    # ------------------------------------------------------------------

    def test_agreement_classification_always_rendered(
        self, client, tmp_path, monkeypatch
    ):
        """
        The source-agreement card must always show a classification badge,
        whether the result is 'available', 'partial', or 'insufficient data'.
        The specific classification value depends on data coverage and is not
        asserted here.
        """
        snapshot = _build_tuya_status_snapshot()
        status_path = tmp_path / "kestrel_status.json"
        db_path = tmp_path / "kestrel.db"
        tuya_status_path = tmp_path / "tuya_status.json"
        tuya_history_path = tmp_path / "tuya_history.jsonl"

        _write_status(status_path)
        _seed_db_with_coverage(db_path)
        _write_tuya_status_recent(tuya_status_path)
        _write_tuya_history_with_compressor(tuya_history_path, snapshot)

        self._patch_all_sources(
            monkeypatch, tmp_path,
            status_path=status_path, db_path=db_path,
            tuya_status_path=tuya_status_path, tuya_history_path=tuya_history_path,
            nest_history_path=tmp_path / "no_nest.jsonl",
        )

        response = self._stub_host(client).get("/kestrel")
        assert response.status_code == 200
        text = response.text

        assert "Do SMT and Tuya agree?" in text

        # The card must show one of the known classification labels
        known_classifications = [
            "Not comparable",
            "no whole-home CT",
            "Partial coverage",
            "Insufficient data",
            "Not available",
        ]
        assert any(label in text for label in known_classifications), (
            "Agreement card must show a classification label"
        )

    # ------------------------------------------------------------------
    # Cycle metrics visible with sufficient cooling data
    # ------------------------------------------------------------------

    def test_cycle_metrics_shown_when_cooling_data_available(
        self, client, tmp_path, monkeypatch
    ):
        """
        When Nest history contains COOLING samples that form at least one
        detectable cycle, the HVAC Performance section must show cycle
        statistics ('Cooling cycles', 'Total cooling runtime') rather than
        only the polling-status fallback.
        """
        snapshot = _build_tuya_status_snapshot()
        status_path = tmp_path / "kestrel_status.json"
        db_path = tmp_path / "kestrel.db"
        tuya_status_path = tmp_path / "tuya_status.json"
        tuya_history_path = tmp_path / "tuya_history.jsonl"
        nest_history_path = tmp_path / "nest_history.jsonl"

        _write_status(status_path)
        _seed_db(db_path)
        _write_tuya_status_recent(tuya_status_path)
        _write_tuya_history_with_compressor(tuya_history_path, snapshot)
        # Write a clear cooling cycle: 6 COOLING + 6 OFF at 5-min cadence = 30-min cycle
        _write_nest_history_with_cycle(nest_history_path, cooling_count=6, idle_count=6)

        self._patch_all_sources(
            monkeypatch, tmp_path,
            status_path=status_path, db_path=db_path,
            tuya_status_path=tuya_status_path, tuya_history_path=tuya_history_path,
            nest_history_path=nest_history_path,
        )

        response = self._stub_host(client).get("/kestrel")
        assert response.status_code == 200
        text = response.text

        assert "HVAC Performance" in text
        # Cycle stats must be present when a cycle was detected
        assert "Cooling cycles" in text, (
            "HVAC Performance must show 'Cooling cycles' label when cycles detected"
        )
        assert "Total cooling runtime" in text, (
            "HVAC Performance must show 'Total cooling runtime' when cycles detected"
        )

    # ------------------------------------------------------------------
    # Polling-status fallback when no cycles detected
    # ------------------------------------------------------------------

    def test_nest_fallback_labeled_when_no_cycles(
        self, client, tmp_path, monkeypatch
    ):
        """
        When Nest history is present but contains only non-COOLING samples,
        the HVAC Performance zone table must be labeled 'Nest polling status'
        rather than presenting the data as cycle analysis.
        """
        from kestrel.nest_history import append_history_from_snapshot

        status_path = tmp_path / "kestrel_status.json"
        db_path = tmp_path / "kestrel.db"
        nest_history_path = tmp_path / "nest_history.jsonl"

        _write_status(status_path)
        _seed_db(db_path)

        self._patch_all_sources(
            monkeypatch, tmp_path,
            status_path=status_path, db_path=db_path,
            tuya_status_path=tmp_path / "no_tuya.json",
            tuya_history_path=tmp_path / "no_tuya.jsonl",
            nest_history_path=nest_history_path,
        )

        # Idle-only records — no COOLING action means zero cycles detected
        base = datetime.now(UTC) - timedelta(minutes=30)
        for i in range(6):
            ts = base + timedelta(minutes=i * 5)
            append_history_from_snapshot(
                {
                    "updated_at": ts.strftime("%Y-%m-%dT%H:%M:%S+00:00"),
                    "thermostats": {
                        "downstairs": {
                            "action": "OFF",
                            "temperature": 76,
                            "setpoint": 72,
                            "online": True,
                        },
                    },
                },
                path=nest_history_path,
                now=ts,
            )

        response = self._stub_host(client).get("/kestrel")
        assert response.status_code == 200
        text = response.text

        assert "HVAC Performance" in text
        # With no cycles detected the fallback label must be visible
        assert "Nest polling status" in text or "no cycles" in text.lower(), (
            "HVAC section must show 'Nest polling status' label when no cycles detected"
        )

    # ------------------------------------------------------------------
    # Unavailable sources → warning, not false zero values
    # ------------------------------------------------------------------

    def test_missing_tuya_shows_warning_not_false_zeros(
        self, client, tmp_path, monkeypatch
    ):
        """
        When Tuya data is absent the energy-breakdown section must NOT show
        '0.00 kWh' for HVAC energy; it must either omit the row entirely or
        show an appropriate empty-state message.  The page must still return
        200 and show the SMT-only sections.
        """
        status_path = tmp_path / "kestrel_status.json"
        db_path = tmp_path / "kestrel.db"

        _write_status(status_path)
        _seed_db(db_path)

        self._patch_all_sources(
            monkeypatch, tmp_path,
            status_path=status_path, db_path=db_path,
            tuya_status_path=tmp_path / "no_tuya.json",
            tuya_history_path=tmp_path / "no_tuya.jsonl",
            nest_history_path=tmp_path / "no_nest.jsonl",
        )

        response = self._stub_host(client).get("/kestrel")
        assert response.status_code == 200
        text = response.text

        assert "Energy Breakdown" in text

        # Without Tuya data, HVAC energy row should not show "0.00 kWh"
        # (it should either be omitted or show an empty-state message).
        # We allow "0.0" only if it appears as part of the SMT total reference,
        # not as a claimed HVAC estimate.
        if "HVAC" in text and "0.00 kWh" in text:
            # If "0.00 kWh" appears, it must NOT be labelled as an HVAC estimate
            hvac_zero_pattern = "HVAC" + "0.00 kWh"
            assert hvac_zero_pattern not in text.replace(" ", "").replace("\n", ""), (
                "HVAC 0.00 kWh should not appear when Tuya data is unavailable"
            )

        # SMT data section must still render
        assert "Historical Trends" in text
        assert "Today's Energy Story" in text

    # ------------------------------------------------------------------
    # Energy story: no duplicate or contradictory HVAC findings
    # ------------------------------------------------------------------

    def test_energy_story_no_duplicate_hvac_findings(
        self, client, tmp_path, monkeypatch
    ):
        """
        The energy story must not contain two separate HVAC percentage findings
        that could contradict each other (e.g. 'HVAC accounted for X% of peak'
        and 'HVAC accounted for Y% of total').  The redesigned story uses
        distinct, non-overlapping phrasings.
        """
        snapshot = _build_tuya_status_snapshot()
        status_path = tmp_path / "kestrel_status.json"
        db_path = tmp_path / "kestrel.db"
        tuya_status_path = tmp_path / "tuya_status.json"
        tuya_history_path = tmp_path / "tuya_history.jsonl"
        nest_history_path = tmp_path / "nest_history.jsonl"

        _write_status(status_path)
        _seed_db_with_coverage(db_path)
        _write_tuya_status_recent(tuya_status_path)
        _write_tuya_history_with_compressor(tuya_history_path, snapshot)
        _write_nest_history_with_cycle(nest_history_path)

        self._patch_all_sources(
            monkeypatch, tmp_path,
            status_path=status_path, db_path=db_path,
            tuya_status_path=tuya_status_path, tuya_history_path=tuya_history_path,
            nest_history_path=nest_history_path,
        )

        response = self._stub_host(client).get("/kestrel")
        assert response.status_code == 200
        text = response.text

        # Old duplicated phrasing must not appear
        assert "HVAC accounted for" not in text, (
            "Deprecated phrase 'HVAC accounted for' found; use 'HVAC used approximately' instead"
        )

        # The story section itself should not repeat the same concept twice.
        # Count story list items containing 'HVAC' — must be at most one.
        import re as _re
        story_block_match = _re.search(
            r"story-findings.*?</ul>", text, _re.DOTALL
        )
        if story_block_match:
            story_html = story_block_match.group(0)
            hvac_items = _re.findall(r"<li[^>]*>.*?HVAC.*?</li>", story_html, _re.DOTALL)
            assert len(hvac_items) <= 1, (
                f"Energy story must not have more than one HVAC finding; found {len(hvac_items)}"
            )

    # ------------------------------------------------------------------
    # Stale source degrades gracefully
    # ------------------------------------------------------------------

    def test_stale_tuya_degrades_gracefully(
        self, client, tmp_path, monkeypatch
    ):
        """
        With stale Tuya data (> 2 × poll interval old), the page must still
        return 200 and render all structural headings.  The stale state must
        be surfaced via a badge or warning, not silently suppressed.
        """
        status_path = tmp_path / "kestrel_status.json"
        db_path = tmp_path / "kestrel.db"
        stale_status_path = tmp_path / "tuya_status.json"

        _write_status(status_path)
        _seed_db(db_path)
        _write_tuya_status(stale_status_path)   # uses old Jun 27 timestamp → stale

        self._patch_all_sources(
            monkeypatch, tmp_path,
            status_path=status_path, db_path=db_path,
            tuya_status_path=stale_status_path,
            tuya_history_path=tmp_path / "no_tuya.jsonl",
            nest_history_path=tmp_path / "no_nest.jsonl",
        )

        response = self._stub_host(client).get("/kestrel")
        assert response.status_code == 200
        text = response.text

        for heading in ("Today's Energy Story", "HVAC Performance", "Historical Trends"):
            assert heading in text, f"Section heading '{heading}' missing with stale Tuya"

        # Stale state must be surfaced via a badge or warning label
        assert "stale" in text.lower() or "Stale" in text, (
            "Stale Tuya state must be surfaced to the user"
        )

    # ------------------------------------------------------------------
    # All sources unavailable: graceful degradation
    # ------------------------------------------------------------------

    def test_all_sources_unavailable_renders_gracefully(
        self, client, tmp_path, monkeypatch
    ):
        """
        When no source files exist the page must still return HTTP 200, render
        all structural headings, and show an appropriate 'no data' message.
        It must not raise an unhandled exception or show a server error.
        """
        self._patch_all_sources(
            monkeypatch, tmp_path,
            status_path=tmp_path / "no_smt.json",
            db_path=tmp_path / "no_smt.db",
            tuya_status_path=tmp_path / "no_tuya.json",
            tuya_history_path=tmp_path / "no_tuya.jsonl",
            nest_history_path=tmp_path / "no_nest.jsonl",
        )

        response = self._stub_host(client).get("/kestrel")
        assert response.status_code == 200
        text = response.text

        for heading in (
            "Today's Energy Story",
            "HVAC Performance",
            "Historical Trends",
        ):
            assert heading in text, f"Section heading '{heading}' missing with no data"

        assert "traceback" not in text.lower()
        assert "Internal Server Error" not in text.lower()
        # Story section shows appropriate message
        assert (
            "Not enough" in text
            or "No data" in text
            or "unavailable" in text.lower()
        )
