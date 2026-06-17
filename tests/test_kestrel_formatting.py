"""Tests for Kestrel dashboard display formatting."""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

DASHBOARD_DIR = Path(__file__).resolve().parent.parent / "dashboard"
sys.path.insert(0, str(DASHBOARD_DIR))

from kestrel_formatting import (  # noqa: E402
    format_daily_total_display,
    format_kestrel_card_display,
    format_range_display,
    format_timestamp_friendly,
    format_top_interval_display,
)


CHICAGO = ZoneInfo("America/Chicago")
FIXED_NOW = datetime(2026, 6, 16, 12, 0, tzinfo=CHICAGO)


class TestKestrelFormatting:
    def test_format_timestamp_friendly_long_form(self):
        label = format_timestamp_friendly(
            "2026-06-10T18:00:00+00:00",
            now=FIXED_NOW,
        )
        assert label == "Wed Jun 10, 1:00 PM"

    def test_format_timestamp_friendly_yesterday(self):
        label = format_timestamp_friendly(
            "2026-06-16T00:00:00+00:00",
            now=FIXED_NOW,
        )
        assert label == "Yesterday 7:00 PM"

    def test_format_range_display(self):
        label = format_range_display(
            "2026-06-09T00:00:00+00:00",
            "2026-06-16T00:00:00+00:00",
            now=FIXED_NOW,
        )
        assert label == "Mon Jun 8, 7:00 PM → Yesterday 7:00 PM"

    def test_format_top_interval_display(self):
        label = format_top_interval_display(
            {
                "start_ts": "2026-06-15T18:00:00+00:00",
                "end_ts": "2026-06-15T18:15:00+00:00",
                "kwh": 1.56,
                "estimated_peak_kw": 6.25,
            }
        )
        assert label == "Mon 6/15, 1:00–1:15 PM — 1.56 kWh / est. 6.25 kW"

    def test_format_daily_total_display(self):
        label = format_daily_total_display("2026-06-15", 71.97)
        assert label == "Mon 6/15 — 71.97 kWh"

    def test_format_kestrel_card_rounds_to_two_decimals(self):
        display = format_kestrel_card_display(
            {
                "range_start": "2026-06-09T00:00:00+00:00",
                "range_end": "2026-06-16T00:00:00+00:00",
                "total_kwh": 42.5,
                "missing_interval_count": 3,
            },
            {
                "recent_daily_totals": [{"day": "2026-06-15", "kwh": 6.25}],
                "avg_daily_7": {"kwh": 6.25, "day_count": 1, "requested_days": 7},
                "avg_daily_30": {"kwh": 6.25, "day_count": 1, "requested_days": 30},
            },
            now=FIXED_NOW,
        )
        assert display["total_kwh"] == "42.50"
        assert display["missing_interval_count"] == "3"
        assert display["recent_daily_totals"][0]["display"] == "Mon 6/15 — 6.25 kWh"
        assert display["avg_daily_7"]["value"] == "6.25"
        assert display["avg_daily_7"]["label"] == "Avg daily (last 1 day)"

    def test_format_kestrel_card_no_raw_iso_in_display_fields(self):
        display = format_kestrel_card_display(
            {
                "range_start": "2026-06-09T00:00:00+00:00",
                "range_end": "2026-06-16T00:00:00+00:00",
                "generated_at": "2026-06-16T12:00:00+00:00",
            },
            {
                "recent_daily_totals": [{"day": "2026-06-15", "kwh": 6.25}],
            },
            now=FIXED_NOW,
        )
        display_fields = [
            display["range"],
            display["generated_at"],
            display["recent_daily_totals"][0]["display"],
        ]
        rendered = " | ".join(display_fields)
        assert "2026-06-15T18:00:00+00:00" not in rendered
        assert "2026-06-16T12:00:00+00:00" not in rendered
        assert "→" in display["range"]
