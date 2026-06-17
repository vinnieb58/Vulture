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
                "estimated_peak_kw": 10.0,
                "missing_interval_count": 3,
                "interval_count": 96,
                "top_intervals": [
                    {
                        "start_ts": "2026-06-15T18:00:00+00:00",
                        "end_ts": "2026-06-15T18:15:00+00:00",
                        "kwh": 2.5,
                        "estimated_peak_kw": 10.0,
                    }
                ],
                "daily_totals": {"2026-06-15": 6.25},
            },
            now=FIXED_NOW,
        )
        assert display["total_kwh"] == "42.50"
        assert display["estimated_peak_kw"] == "10.00"
        assert display["missing_interval_count"] == "3"
        assert display["interval_count"] == "96"
        assert "1:00–1:15 PM" in display["top_intervals"][0]["display"]
        assert display["daily_totals"][0]["display"] == "Mon 6/15 — 6.25 kWh"

    def test_format_kestrel_card_no_raw_iso_in_display_fields(self):
        display = format_kestrel_card_display(
            {
                "range_start": "2026-06-09T00:00:00+00:00",
                "range_end": "2026-06-16T00:00:00+00:00",
                "generated_at": "2026-06-16T12:00:00+00:00",
                "top_intervals": [
                    {
                        "start_ts": "2026-06-15T18:00:00+00:00",
                        "end_ts": "2026-06-15T18:15:00+00:00",
                        "kwh": 2.5,
                        "estimated_peak_kw": 10.0,
                    }
                ],
                "daily_totals": {"2026-06-15": 6.25},
            },
            now=FIXED_NOW,
        )
        display_fields = [
            display["range"],
            display["generated_at"],
            display["top_intervals"][0]["display"],
            display["daily_totals"][0]["display"],
        ]
        rendered = " | ".join(display_fields)
        assert "2026-06-15T18:00:00+00:00" not in rendered
        assert "2026-06-16T12:00:00+00:00" not in rendered
        assert "→" in display["range"]
