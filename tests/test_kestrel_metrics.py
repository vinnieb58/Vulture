"""Tests for read-only Kestrel dashboard metrics helpers."""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "dashboard"))

from kestrel.config import PROVIDER_SMART_METER_TEXAS
from kestrel.models import EnergyInterval
from kestrel.storage import init_db, upsert_intervals
import kestrel_metrics as metrics


CHICAGO = ZoneInfo("America/Chicago")
FIXED_NOW = datetime(2026, 6, 17, 12, 0, tzinfo=CHICAGO)


def _interval(start: str, end: str, kwh: float) -> EnergyInterval:
    return EnergyInterval(
        provider=PROVIDER_SMART_METER_TEXAS,
        start_ts=start,
        end_ts=end,
        kwh=kwh,
    )


def _seed_sample_db(db_path: Path) -> None:
    rows = [
        _interval("2026-06-15T05:00:00+00:00", "2026-06-15T05:15:00+00:00", 1.0),
        _interval("2026-06-15T18:00:00+00:00", "2026-06-15T18:15:00+00:00", 2.5),
        _interval("2026-06-16T05:00:00+00:00", "2026-06-16T05:15:00+00:00", 1.5),
        _interval("2026-06-16T06:00:00+00:00", "2026-06-16T06:15:00+00:00", 0.75),
        _interval("2026-06-10T05:00:00+00:00", "2026-06-10T05:15:00+00:00", 0.5),
    ]
    init_db(db_path)
    upsert_intervals(db_path, rows)


@pytest.fixture
def metrics_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db_path = tmp_path / "kestrel.db"
    _seed_sample_db(db_path)
    monkeypatch.setattr(metrics, "KESTREL_DB_PATH", db_path)
    return db_path


class TestKestrelMetrics:
    def test_get_daily_totals_empty_when_db_missing(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(metrics, "KESTREL_DB_PATH", tmp_path / "missing.db")
        assert metrics.get_daily_totals() == {}

    def test_get_daily_totals_groups_by_local_day(self, metrics_db: Path):
        totals = metrics.get_daily_totals(days=30)
        assert totals["2026-06-15"] == pytest.approx(3.5)
        assert totals["2026-06-16"] == pytest.approx(2.25)

    def test_get_average_daily_usage_uses_available_days_only(self, metrics_db: Path):
        avg = metrics.get_average_daily_usage(7)
        assert avg.day_count == 2
        assert avg.requested_days == 7
        assert avg.kwh == pytest.approx((3.5 + 2.25) / 2)

    def test_get_peak_interval_last_7_days(self, metrics_db: Path):
        peak = metrics.get_peak_interval(7)
        assert peak is not None
        assert peak.kwh == pytest.approx(2.5)
        assert peak.estimated_peak_kw == pytest.approx(10.0)

    def test_get_hourly_average(self, metrics_db: Path):
        hourly = metrics.get_hourly_average(30)
        assert 0 in hourly
        assert hourly[0] == pytest.approx(1.0)

    def test_get_monthly_totals(self, metrics_db: Path):
        monthly = metrics.get_monthly_totals()
        assert "2026-06" in monthly
        assert monthly["2026-06"] == pytest.approx(6.25)

    def test_get_top_intervals_limits_results(self, metrics_db: Path):
        peaks = metrics.get_top_intervals(30, limit=2)
        assert len(peaks) == 2
        assert peaks[0].kwh >= peaks[1].kwh

    def test_downsample_daily_totals_for_long_ranges(self, metrics_db: Path):
        long_totals = {
            (FIXED_NOW.date() - timedelta(days=offset)).isoformat(): 1.0
            for offset in range(150)
        }
        downsampled = metrics._downsample_daily_totals(long_totals)
        assert len(downsampled) < len(long_totals)
        assert len(downsampled) <= metrics.MAX_FULL_RANGE_CHART_POINTS

    def test_get_detail_metrics_never_raises_without_db(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(metrics, "KESTREL_DB_PATH", tmp_path / "missing.db")
        detail = metrics.get_detail_metrics()
        assert detail["available"] is False
        assert detail["daily_30"] == []
