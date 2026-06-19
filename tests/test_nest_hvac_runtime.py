"""Tests for Nest HVAC runtime summaries from polling history."""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

ROOT = Path(__file__).resolve().parent.parent
DASHBOARD_DIR = ROOT / "dashboard"
sys.path.insert(0, str(DASHBOARD_DIR))
sys.path.insert(0, str(ROOT))

from kestrel.nest_history import NestHistoryRecord, POLL_INTERVAL_MINUTES, append_history_from_snapshot  # noqa: E402
from nest_hvac_runtime import (  # noqa: E402
    WINDOW_LAST_24H,
    WINDOW_TODAY,
    WINDOW_YESTERDAY,
    compute_household_runtime,
    compute_runtime_summary,
    compute_thermostat_runtime,
    get_hvac_runtime_summaries,
)

CHICAGO = ZoneInfo("America/Chicago")


def _record(
    *,
    ts: datetime,
    downstairs: str = "OFF",
    upstairs: str = "OFF",
) -> NestHistoryRecord:
    return NestHistoryRecord(
        timestamp=ts,
        thermostats={
            "downstairs": {"action": downstairs},
            "upstairs": {"action": upstairs},
        },
    )


class TestThermostatRuntimeCalculation:
    def test_cooling_heating_idle_minutes_from_five_minute_samples(self) -> None:
        base = datetime(2026, 6, 19, 12, 0, tzinfo=timezone.utc)
        records = [
            _record(ts=base, downstairs="COOLING"),
            _record(ts=base + timedelta(minutes=5), downstairs="COOLING"),
            _record(ts=base + timedelta(minutes=10), downstairs="HEATING"),
            _record(ts=base + timedelta(minutes=15), downstairs="OFF"),
        ]
        runtime = compute_thermostat_runtime(records, "downstairs")
        assert runtime.cooling_minutes == 2 * POLL_INTERVAL_MINUTES
        assert runtime.heating_minutes == POLL_INTERVAL_MINUTES
        assert runtime.idle_minutes == POLL_INTERVAL_MINUTES
        assert runtime.sample_count == 4
        assert runtime.percent_time_cooling == pytest.approx(50.0)


class TestHouseholdRuntimeCalculation:
    def test_any_cooling_vs_both_cooling(self) -> None:
        base = datetime(2026, 6, 19, 12, 0, tzinfo=timezone.utc)
        records = [
            _record(ts=base, downstairs="COOLING", upstairs="OFF"),
            _record(ts=base + timedelta(minutes=5), downstairs="COOLING", upstairs="COOLING"),
            _record(ts=base + timedelta(minutes=10), downstairs="OFF", upstairs="OFF"),
        ]
        household = compute_household_runtime(records, ["downstairs", "upstairs"])
        assert household.any_cooling_minutes == 2 * POLL_INTERVAL_MINUTES
        assert household.both_cooling_minutes == POLL_INTERVAL_MINUTES
        assert household.sample_count == 3


class TestRuntimeWindows:
    def test_last_24h_window(self) -> None:
        now = datetime(2026, 6, 19, 12, 0, tzinfo=timezone.utc)
        records = [
            _record(ts=now - timedelta(hours=23), downstairs="COOLING"),
            _record(ts=now - timedelta(hours=30), downstairs="COOLING"),
        ]
        summary = compute_runtime_summary(records, window=WINDOW_LAST_24H, now=now)
        assert summary is not None
        assert summary.sample_count == 1

    def test_today_and_yesterday_windows(self) -> None:
        now = datetime(2026, 6, 19, 15, 0, tzinfo=CHICAGO).astimezone(timezone.utc)
        today_local = datetime(2026, 6, 19, 10, 0, tzinfo=CHICAGO).astimezone(timezone.utc)
        yesterday_local = datetime(2026, 6, 18, 10, 0, tzinfo=CHICAGO).astimezone(timezone.utc)
        records = [
            _record(ts=today_local, downstairs="COOLING"),
            _record(ts=yesterday_local, downstairs="COOLING"),
        ]

        today = compute_runtime_summary(records, window=WINDOW_TODAY, now=now)
        yesterday = compute_runtime_summary(records, window=WINDOW_YESTERDAY, now=now)
        assert today is not None and today.sample_count == 1
        assert yesterday is not None and yesterday.sample_count == 1


class TestMissingAndStaleHistory:
    def test_no_history_file_reports_no_data(self, tmp_path: Path, monkeypatch) -> None:
        missing = tmp_path / "missing.jsonl"
        monkeypatch.setattr("nest_hvac_runtime.NEST_HISTORY_PATH", missing)
        result = get_hvac_runtime_summaries(path=missing)
        assert result["state"] == "no_data"
        assert "not found" in (result["warning"] or "").lower()

    def test_stale_history_reports_warning(self, tmp_path: Path, monkeypatch) -> None:
        path = tmp_path / "history.jsonl"
        monkeypatch.setattr("nest_hvac_runtime.NEST_HISTORY_PATH", path)
        old = datetime(2026, 6, 19, 10, 0, tzinfo=timezone.utc)
        now = old + timedelta(minutes=30)
        append_history_from_snapshot(
            {
                "updated_at": old.isoformat(),
                "thermostats": {"downstairs": {"action": "OFF", "online": True}},
            },
            path=path,
            now=old,
        )
        result = get_hvac_runtime_summaries(path=path, now=now)
        assert result["state"] == "stale"
        assert "stale" in (result["warning"] or "").lower()
