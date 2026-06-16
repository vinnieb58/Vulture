"""Unit tests for Kestrel interval summaries."""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kestrel.config import PROVIDER_SMART_METER_TEXAS
from kestrel.models import EnergyInterval
from kestrel.summarize import (
    estimated_kw_from_interval_kwh,
    missing_interval_count,
    peak_interval,
    summarize_intervals,
    total_kwh,
    top_intervals,
)


class TestKestrelSummarize:
    def test_total_kwh(self) -> None:
        rows = [
            EnergyInterval(
                provider=PROVIDER_SMART_METER_TEXAS,
                start_ts="2026-06-01T05:00:00+00:00",
                end_ts="2026-06-01T05:15:00+00:00",
                kwh=0.4,
            ),
            EnergyInterval(
                provider=PROVIDER_SMART_METER_TEXAS,
                start_ts="2026-06-01T05:15:00+00:00",
                end_ts="2026-06-01T05:30:00+00:00",
                kwh=0.6,
            ),
        ]
        assert total_kwh(rows) == pytest.approx(1.0)

    def test_peak_interval_and_estimated_kw(self) -> None:
        rows = [
            EnergyInterval(
                provider=PROVIDER_SMART_METER_TEXAS,
                start_ts="2026-06-01T05:00:00+00:00",
                end_ts="2026-06-01T05:15:00+00:00",
                kwh=0.4,
            ),
            EnergyInterval(
                provider=PROVIDER_SMART_METER_TEXAS,
                start_ts="2026-06-01T05:15:00+00:00",
                end_ts="2026-06-01T05:30:00+00:00",
                kwh=1.25,
            ),
        ]
        peak = peak_interval(rows)
        assert peak is not None
        assert peak.kwh == pytest.approx(1.25)
        assert peak.estimated_peak_kw == pytest.approx(5.0)
        assert estimated_kw_from_interval_kwh(1.25) == pytest.approx(5.0)

    def test_missing_interval_count(self) -> None:
        rows = [
            EnergyInterval(
                provider=PROVIDER_SMART_METER_TEXAS,
                start_ts="2026-06-01T05:00:00+00:00",
                end_ts="2026-06-01T05:15:00+00:00",
                kwh=0.4,
            ),
            EnergyInterval(
                provider=PROVIDER_SMART_METER_TEXAS,
                start_ts="2026-06-01T05:30:00+00:00",
                end_ts="2026-06-01T05:45:00+00:00",
                kwh=0.5,
            ),
        ]
        missing = missing_interval_count(
            rows,
            range_start=datetime(2026, 6, 1, 5, 0, tzinfo=timezone.utc),
            range_end=datetime(2026, 6, 1, 6, 0, tzinfo=timezone.utc),
        )
        assert missing == 2

    def test_summarize_includes_daily_and_top_intervals(self) -> None:
        rows = [
            EnergyInterval(
                provider=PROVIDER_SMART_METER_TEXAS,
                start_ts="2026-06-01T05:00:00+00:00",
                end_ts="2026-06-01T05:15:00+00:00",
                kwh=0.4,
            ),
            EnergyInterval(
                provider=PROVIDER_SMART_METER_TEXAS,
                start_ts="2026-06-01T05:15:00+00:00",
                end_ts="2026-06-01T05:30:00+00:00",
                kwh=2.0,
            ),
        ]
        summary = summarize_intervals(rows, tz_name="America/Chicago", anomaly_top_n=2)
        assert summary.interval_count == 2
        assert summary.total_kwh == pytest.approx(2.4)
        assert summary.estimated_peak_kw == pytest.approx(8.0)
        assert "2026-06-01" in summary.daily_totals
        assert len(top_intervals(rows, 2)) == 2
