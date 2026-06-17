"""Tests for Kestrel status snapshot builder."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kestrel.config import PROVIDER_SMART_METER_TEXAS
from kestrel.models import EnergyInterval
from kestrel.status_snapshot import build_status_snapshot
from kestrel.summarize import IntervalSummary, PeakInterval, summarize_intervals, top_intervals


def _interval(start: str, end: str, kwh: float) -> EnergyInterval:
    return EnergyInterval(
        provider=PROVIDER_SMART_METER_TEXAS,
        start_ts=start,
        end_ts=end,
        kwh=kwh,
    )


class TestBuildStatusSnapshot:
    def test_includes_all_dashboard_fields(self) -> None:
        rows = [
            _interval("2026-06-01T05:00:00+00:00", "2026-06-01T05:15:00+00:00", 0.4),
            _interval("2026-06-01T05:15:00+00:00", "2026-06-01T05:30:00+00:00", 2.0),
        ]
        summary = summarize_intervals(rows, tz_name="America/Chicago", anomaly_top_n=2)
        top = top_intervals(rows, 2)
        snapshot = build_status_snapshot(
            summary,
            top,
            provider=PROVIDER_SMART_METER_TEXAS,
            last_updated="2026-06-16T12:00:00+00:00",
        )

        assert snapshot["status"] == "available"
        assert snapshot["provider"] == PROVIDER_SMART_METER_TEXAS
        assert snapshot["last_updated"] == "2026-06-16T12:00:00+00:00"
        assert snapshot["range_start"] == rows[0].start_ts
        assert snapshot["range_end"] == rows[-1].end_ts
        assert snapshot["interval_count"] == 2
        assert snapshot["total_kwh"] == pytest.approx(2.4)
        assert snapshot["estimated_peak_kw"] == pytest.approx(8.0)
        assert snapshot["missing_interval_count"] == 0

        peak = snapshot["peak_interval"]
        assert peak["start_ts"] == "2026-06-01T05:15:00+00:00"
        assert peak["end_ts"] == "2026-06-01T05:30:00+00:00"
        assert peak["kwh"] == pytest.approx(2.0)
        assert peak["estimated_kw"] == pytest.approx(8.0)

        assert len(snapshot["top_intervals"]) == 2
        assert snapshot["top_intervals"][0]["estimated_kw"] == pytest.approx(8.0)

        assert snapshot["daily_totals"] == [
            {"date": "2026-06-01", "kwh": pytest.approx(2.4)},
        ]

    def test_no_data_status_when_empty(self) -> None:
        summary = summarize_intervals([])
        snapshot = build_status_snapshot(
            summary,
            [],
            provider=PROVIDER_SMART_METER_TEXAS,
            last_updated="2026-06-16T12:00:00+00:00",
        )
        assert snapshot["status"] == "no_data"
        assert snapshot["interval_count"] == 0
        assert snapshot["peak_interval"] is None
        assert snapshot["top_intervals"] == []
        assert snapshot["daily_totals"] == []

    def test_never_includes_sensitive_fields(self) -> None:
        rows = [
            _interval("2026-06-01T05:00:00+00:00", "2026-06-01T05:15:00+00:00", 1.0),
        ]
        summary = summarize_intervals(rows)
        snapshot = build_status_snapshot(
            summary,
            top_intervals(rows, 1),
            provider=PROVIDER_SMART_METER_TEXAS,
        )
        text = json.dumps(snapshot)
        for forbidden in (
            "account_id",
            "meter_id",
            "esiid",
            "raw_source",
            "db_path",
            "hash",
            "password",
            "username",
        ):
            assert forbidden not in text.lower()

    def test_json_serializable(self) -> None:
        rows = [
            _interval("2026-06-01T05:00:00+00:00", "2026-06-01T05:15:00+00:00", 0.5),
        ]
        summary = summarize_intervals(rows)
        snapshot = build_status_snapshot(
            summary,
            top_intervals(rows, 1),
            provider=PROVIDER_SMART_METER_TEXAS,
        )
        encoded = json.dumps(snapshot)
        decoded = json.loads(encoded)
        assert decoded["status"] == "available"
        assert decoded["provider"] == PROVIDER_SMART_METER_TEXAS

    def test_last_updated_defaults_to_iso_timestamp(self) -> None:
        rows = [
            _interval("2026-06-01T05:00:00+00:00", "2026-06-01T05:15:00+00:00", 0.5),
        ]
        summary = summarize_intervals(rows)
        snapshot = build_status_snapshot(
            summary,
            top_intervals(rows, 1),
            provider=PROVIDER_SMART_METER_TEXAS,
        )
        assert snapshot["last_updated"].endswith("+00:00")

    def test_refresh_fields_optional(self) -> None:
        rows = [
            _interval("2026-06-01T05:00:00+00:00", "2026-06-01T05:15:00+00:00", 0.5),
        ]
        summary = summarize_intervals(rows)
        from kestrel.live_refresh import RefreshMetadata

        snapshot = build_status_snapshot(
            summary,
            top_intervals(rows, 1),
            provider=PROVIDER_SMART_METER_TEXAS,
            refresh=RefreshMetadata(
                attempt_at="2026-06-17T12:00:00+00:00",
                success_at="2026-06-17T12:00:01+00:00",
                source="live_api",
                status="ok",
                message="Fetched 1 intervals",
            ),
        )
        assert snapshot["last_refresh_source"] == "live_api"
        assert snapshot["last_refresh_status"] == "ok"

    def test_partial_refresh_status(self) -> None:
        rows = [
            _interval("2026-06-01T05:00:00+00:00", "2026-06-01T05:15:00+00:00", 0.5),
        ]
        summary = summarize_intervals(rows)
        from kestrel.live_refresh import RefreshMetadata

        snapshot = build_status_snapshot(
            summary,
            top_intervals(rows, 1),
            provider=PROVIDER_SMART_METER_TEXAS,
            refresh=RefreshMetadata(
                attempt_at="2026-06-17T12:00:00+00:00",
                success_at="2026-06-17T12:00:01+00:00",
                source="live_api",
                status="partial",
                message="Fetched 96 intervals; latest day unavailable (likely TDSP lag)",
            ),
        )
        assert snapshot["last_refresh_status"] == "partial"
