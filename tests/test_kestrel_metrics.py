"""Tests for read-only Kestrel dashboard metrics helpers."""

from __future__ import annotations

import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
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

# ---------------------------------------------------------------------------
# Frozen reference point
#
# Using a frozen "now" lets the fixture use stable dates and lets window-based
# functions (_cutoff_iso) produce deterministic cutoffs.  We freeze CHICAGO
# noon on an arbitrary Wednesday; all seeded data is within a 30-day window
# from this reference so every day-count assertion remains correct as the
# calendar advances.
#
# All five seeded intervals are keyed relative to FROZEN_NOW:
#   day0  (today):     2 × 1.0 kWh  → daily total 1.0 kWh  (hour 0 local)
#   day0  (peak):      1 × 2.5 kWh  → peak in 7-day window
#   day1  (yesterday): 2 × 1.125 kWh→ daily total 2.25 kWh
#   day7  (week ago):  1 × 0.5 kWh  → outside 7-day window, inside 30-day
#
# The hour-0 local interval lands at (FROZEN_NOW local midnight) in UTC.
# ---------------------------------------------------------------------------

FROZEN_NOW = datetime(2026, 6, 17, 12, 0, tzinfo=CHICAGO)


def _frozen_cutoff(days: int, *, tz_name: str = metrics.KESTREL_TIMEZONE) -> str:
    """Replacement for kestrel_metrics._cutoff_iso that uses FROZEN_NOW."""
    tz = ZoneInfo(tz_name)
    start_local_date = FROZEN_NOW.astimezone(tz).date() - timedelta(days=days - 1)
    start_dt = datetime(
        start_local_date.year,
        start_local_date.month,
        start_local_date.day,
        0, 0,
        tzinfo=tz,
    )
    return start_dt.astimezone(timezone.utc).isoformat()


def _interval(start: str, end: str, kwh: float) -> EnergyInterval:
    return EnergyInterval(
        provider=PROVIDER_SMART_METER_TEXAS,
        start_ts=start,
        end_ts=end,
        kwh=kwh,
    )


def _ts(dt: datetime) -> str:
    """Format datetime as ISO 8601 UTC string for interval timestamps."""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")


def _seed_sample_db(db_path: Path) -> None:
    """
    Seed the test database with five intervals relative to FROZEN_NOW.

    Layout (all times in Chicago local unless noted):
      FROZEN_NOW local date  (day0):  00:00–00:15  1.0 kWh   → hourly bucket 0
      FROZEN_NOW local date  (day0):  13:00–13:15  2.5 kWh   → peak in 7-day window
      day0 - 1 day           (day1):  00:00–00:15  1.5 kWh
      day0 - 1 day           (day1):  01:00–01:15  0.75 kWh
      day0 - 7 days          (day7):  00:00–00:15  0.5 kWh   → outside 7-day, inside 30-day

    day0 total  = 2.5 kWh    (only the 2.5 kWh interval, plus 1.0 = 3.5... wait
    Actually let me recalculate to match the original test assertions:
      test expects day0 daily = 3.5 kWh and day1 daily = 2.25 kWh.
      day0: 1.0 + 2.5 = 3.5 kWh ✓
      day1: 1.5 + 0.75 = 2.25 kWh ✓
    """
    tz = CHICAGO
    frozen_local = FROZEN_NOW.astimezone(tz)
    day0_midnight = frozen_local.replace(hour=0, minute=0, second=0, microsecond=0)
    day1_midnight = day0_midnight - timedelta(days=1)
    day7_midnight = day0_midnight - timedelta(days=7)

    rows = [
        # day0 00:00 local → hourly bucket 0; 1.0 kWh
        _interval(
            _ts(day0_midnight),
            _ts(day0_midnight + timedelta(minutes=15)),
            1.0,
        ),
        # day0 13:00 local → 2.5 kWh (peak in 7-day window)
        _interval(
            _ts(day0_midnight + timedelta(hours=13)),
            _ts(day0_midnight + timedelta(hours=13, minutes=15)),
            2.5,
        ),
        # day1 00:00 local → 1.5 kWh
        _interval(
            _ts(day1_midnight),
            _ts(day1_midnight + timedelta(minutes=15)),
            1.5,
        ),
        # day1 01:00 local → 0.75 kWh
        _interval(
            _ts(day1_midnight + timedelta(hours=1)),
            _ts(day1_midnight + timedelta(hours=1, minutes=15)),
            0.75,
        ),
        # day7 00:00 local → 0.5 kWh (outside 7-day window, inside 30-day)
        _interval(
            _ts(day7_midnight),
            _ts(day7_midnight + timedelta(minutes=15)),
            0.5,
        ),
    ]
    init_db(db_path)
    upsert_intervals(db_path, rows)


@pytest.fixture
def metrics_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """
    Provide a seeded metrics DB and freeze the rolling-window cutoff so that
    window-based queries (7-day, 30-day) produce deterministic results as
    the calendar advances past the fixture dates.
    """
    db_path = tmp_path / "kestrel.db"
    _seed_sample_db(db_path)
    monkeypatch.setattr(metrics, "KESTREL_DB_PATH", db_path)
    # Freeze _cutoff_iso so rolling windows are always relative to FROZEN_NOW,
    # not to the real current time.  Production behavior is unchanged; only
    # the test-local copy of the function is replaced.
    monkeypatch.setattr(metrics, "_cutoff_iso", _frozen_cutoff)
    return db_path


class TestKestrelMetrics:
    def test_get_daily_totals_empty_when_db_missing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setattr(metrics, "KESTREL_DB_PATH", tmp_path / "missing.db")
        assert metrics.get_daily_totals() == {}

    def test_get_daily_totals_groups_by_local_day(self, metrics_db: Path):
        tz = CHICAGO
        frozen_local = FROZEN_NOW.astimezone(tz)
        day0 = frozen_local.date().isoformat()
        day1 = (frozen_local.date() - timedelta(days=1)).isoformat()

        totals = metrics.get_daily_totals(days=30)
        assert totals[day0] == pytest.approx(3.5)
        assert totals[day1] == pytest.approx(2.25)

    def test_get_average_daily_usage_uses_available_days_only(
        self, metrics_db: Path
    ):
        # The 7-day window (relative to FROZEN_NOW) includes day0 and day1
        # but not day7 (which lands exactly on the cutoff boundary and is
        # excluded by the strict >= comparison).
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
        # The two 00:00-local intervals (1.0 kWh from day0, 1.5 kWh from day1,
        # 0.5 kWh from day7) average to 1.0 kWh for bucket 0 within 30 days.
        hourly = metrics.get_hourly_average(30)
        assert 0 in hourly
        assert hourly[0] == pytest.approx(1.0)

    def test_get_monthly_totals(self, metrics_db: Path):
        monthly = metrics.get_monthly_totals()
        frozen_month = FROZEN_NOW.astimezone(CHICAGO).strftime("%Y-%m")
        assert frozen_month in monthly
        assert monthly[frozen_month] == pytest.approx(6.25)

    def test_get_top_intervals_limits_results(self, metrics_db: Path):
        peaks = metrics.get_top_intervals(30, limit=2)
        assert len(peaks) == 2
        assert peaks[0].kwh >= peaks[1].kwh

    def test_downsample_daily_totals_for_long_ranges(self, metrics_db: Path):
        tz = CHICAGO
        ref_date = FROZEN_NOW.astimezone(tz).date()
        long_totals = {
            (ref_date - timedelta(days=offset)).isoformat(): 1.0
            for offset in range(150)
        }
        downsampled = metrics._downsample_daily_totals(long_totals)
        assert len(downsampled) < len(long_totals)
        assert len(downsampled) <= metrics.MAX_FULL_RANGE_CHART_POINTS

    def test_get_detail_metrics_never_raises_without_db(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setattr(metrics, "KESTREL_DB_PATH", tmp_path / "missing.db")
        detail = metrics.get_detail_metrics()
        assert detail["available"] is False
        assert detail["daily_30"] == []
