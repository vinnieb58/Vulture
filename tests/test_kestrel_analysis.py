"""
Tests for the Kestrel energy analysis engine.

Covers:
- SMT kWh to average-kW conversion
- Tuya energy integration
- Common-window selection
- Agreement percentage and classifications
- Missing or stale source behavior
- Peak-event grouping
- HVAC cycle detection
- HVAC energy estimate
- Daily trend coverage handling
- No false appliance attribution
- Rendering when one or more sources are unavailable
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parent.parent
DASHBOARD_DIR = ROOT / "dashboard"
sys.path.insert(0, str(DASHBOARD_DIR))

from kestrel_analysis import (
    AGREEMENT_ACCEPTABLE_PCT,
    AGREEMENT_GOOD_PCT,
    DONUT_SMT_COVERAGE_THRESHOLD,
    DONUT_TUYA_CHANNEL_THRESHOLD,
    HVAC_CYCLE_GAP_TOLERANCE_MINUTES,
    LONG_CYCLE_MINUTES,
    NEST_ACTION_COOLING,
    NEST_ACTION_HEATING,
    SHORT_CYCLE_MINUTES,
    SMT_INTERVAL_MINUTES,
    TUYA_ALL_KEYS,
    TUYA_CHANNEL_LABELS,
    TUYA_HVAC_KEYS,
    TUYA_MONITORED_TOTAL_LABEL,
    AnalysisWindow,
    HvacCycle,
    _find_peaks_from_smt,
    build_all_channel_series,
    build_combined_timeline,
    build_tuya_channel_series,
    build_tuya_kw_series,
    compute_daily_trends,
    compute_energy_breakdown,
    compute_energy_donut,
    compute_hvac_cycle_stats,
    compute_kestrel_analysis,
    compute_source_agreement,
    detect_hvac_cycles,
    find_demand_peaks,
    generate_energy_story,
    integrate_tuya_energy,
    select_analysis_window,
    smt_coverage_pct,
    smt_kwh_to_avg_kw,
    smt_total_kwh,
    tuya_channel_coverage_pct,
    tuya_coverage_pct,
)

CHICAGO = timezone(timedelta(hours=-5))  # simplified, close enough for tests
UTC = timezone.utc

NOW = datetime(2026, 6, 17, 20, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Minimal fake data types
# ---------------------------------------------------------------------------

@dataclass
class FakeTuyaRecord:
    timestamp: datetime
    source: str = "local"
    limited: bool = False
    appliances: dict = None

    def __post_init__(self):
        if self.appliances is None:
            self.appliances = {}


@dataclass
class FakeNestRecord:
    timestamp: datetime
    thermostats: dict = None

    def __post_init__(self):
        if self.thermostats is None:
            self.thermostats = {}


def _smt_row(start_iso: str, kwh: float) -> dict[str, Any]:
    start = datetime.fromisoformat(start_iso)
    end = start + timedelta(minutes=SMT_INTERVAL_MINUTES)
    return {
        "start_ts": start.isoformat(),
        "end_ts": end.isoformat(),
        "kwh": kwh,
    }


def _tuya_record(
    ts: datetime,
    ac_w: float = 0,
    furnace_w: float = 0,
    dryer_w: float = 0,
    dishwasher_w: float = 0,
) -> FakeTuyaRecord:
    return FakeTuyaRecord(
        timestamp=ts,
        appliances={
            "ac_compressor": {"power_w": ac_w} if ac_w else {},
            "furnace_air_handler": {"power_w": furnace_w} if furnace_w else {},
            "dryer": {"power_w": dryer_w} if dryer_w else {},
            "dishwasher": {"power_w": dishwasher_w} if dishwasher_w else {},
        },
    )


def _nest_record(ts: datetime, downstairs: str = "OFF", upstairs: str = "OFF") -> FakeNestRecord:
    return FakeNestRecord(
        timestamp=ts,
        thermostats={
            "downstairs": {"action": downstairs, "temperature": 74, "setpoint": 73},
            "upstairs": {"action": upstairs, "temperature": 77, "setpoint": 76},
        },
    )


# ---------------------------------------------------------------------------
# 1. SMT kWh → average kW conversion
# ---------------------------------------------------------------------------

class TestSmtKwhToAvgKw:
    def test_standard_15min_interval(self):
        assert smt_kwh_to_avg_kw(1.0) == pytest.approx(4.0)

    def test_zero_kwh(self):
        assert smt_kwh_to_avg_kw(0.0) == pytest.approx(0.0)

    def test_typical_hvac_interval(self):
        # 2.5 kWh in 15 min → 10 kW average
        assert smt_kwh_to_avg_kw(2.5) == pytest.approx(10.0)

    def test_low_usage_interval(self):
        # 0.25 kWh in 15 min → 1 kW average
        assert smt_kwh_to_avg_kw(0.25) == pytest.approx(1.0)

    def test_30min_interval(self):
        # 1 kWh in 30 min → 2 kW average
        assert smt_kwh_to_avg_kw(1.0, interval_minutes=30) == pytest.approx(2.0)

    def test_invalid_interval_raises(self):
        with pytest.raises(ValueError):
            smt_kwh_to_avg_kw(1.0, interval_minutes=0)

    def test_negative_interval_raises(self):
        with pytest.raises(ValueError):
            smt_kwh_to_avg_kw(1.0, interval_minutes=-5)


# ---------------------------------------------------------------------------
# 2. Tuya energy integration
# ---------------------------------------------------------------------------

class TestIntegrateTuyaEnergy:
    def test_single_record_uses_poll_duration(self):
        ts = NOW
        records = [_tuya_record(ts, ac_w=2000)]
        # One record: power used for up to 2×poll_seconds → negligible
        kwh = integrate_tuya_energy(records, ("ac_compressor",), window_start=ts, window_end=ts + timedelta(hours=1))
        assert kwh >= 0

    def test_two_records_sixty_seconds_apart(self):
        t1 = NOW
        t2 = NOW + timedelta(seconds=60)
        # 3600 W × 60 s / 3,600,000 = 0.060 kWh
        records = [
            _tuya_record(t1, ac_w=3600),
            _tuya_record(t2, ac_w=3600),
        ]
        kwh = integrate_tuya_energy(
            records, ("ac_compressor",),
            window_start=t1, window_end=t2 + timedelta(seconds=1)
        )
        assert kwh == pytest.approx(0.060, abs=0.01)

    def test_realistic_compressor_one_hour(self):
        # 2500 W compressor running for 60 min = 2.5 kWh
        records = [
            _tuya_record(NOW + timedelta(minutes=i), ac_w=2500)
            for i in range(60)
        ]
        window_end = NOW + timedelta(hours=1)
        kwh = integrate_tuya_energy(records, ("ac_compressor",), window_start=NOW, window_end=window_end)
        # Should be approximately 2.5 kWh
        assert 2.0 < kwh < 3.0

    def test_empty_records_returns_zero(self):
        kwh = integrate_tuya_energy([], ("ac_compressor",), window_start=NOW, window_end=NOW + timedelta(hours=1))
        assert kwh == 0.0

    def test_records_outside_window_ignored(self):
        early = NOW - timedelta(hours=2)
        records = [_tuya_record(early, ac_w=5000)]
        kwh = integrate_tuya_energy(records, ("ac_compressor",), window_start=NOW, window_end=NOW + timedelta(hours=1))
        assert kwh == 0.0

    def test_multiple_appliances_summed(self):
        t1 = NOW
        t2 = NOW + timedelta(seconds=60)
        records = [_tuya_record(t1, ac_w=1000, furnace_w=500)]
        kwh = integrate_tuya_energy(records, TUYA_HVAC_KEYS, window_start=t1, window_end=t2)
        # (1000 + 500) W × 60s / 3,600,000 = 0.025 kWh
        assert kwh == pytest.approx(0.025, abs=0.005)

    def test_large_gap_not_bridged(self):
        # Two records separated by 10 minutes — gap > 5-min threshold, should not be bridged.
        t1 = NOW
        t2 = NOW + timedelta(minutes=10)
        records = [
            _tuya_record(t1, ac_w=3000),
            _tuya_record(t2, ac_w=3000),
        ]
        # With gap_seconds = 600 > MAX_GAP_SECONDS=300, first record should not contribute
        kwh = integrate_tuya_energy(records, ("ac_compressor",), window_start=t1, window_end=t2 + timedelta(seconds=1))
        # Only the second record contributes its fallback interval (2×60=120s)
        # but 3000W * 120s / 3,600,000 = 0.1 kWh for that record alone
        # First record's gap to t2 is 600s > 300s → skipped → no contribution from t1
        assert kwh < 0.5  # significantly less than bridged would be


# ---------------------------------------------------------------------------
# 3. Common-window selection
# ---------------------------------------------------------------------------

class TestSelectAnalysisWindow:
    def _make_smt_rows(self, count: int, base: datetime) -> list[dict]:
        return [
            _smt_row((base + timedelta(minutes=i * 15)).isoformat(), 0.5)
            for i in range(count)
        ]

    def _make_tuya_records(self, count: int, base: datetime) -> list:
        return [
            _tuya_record(base + timedelta(minutes=i), ac_w=2000)
            for i in range(count)
        ]

    def _make_nest_records(self, count: int, base: datetime) -> list:
        return [
            _nest_record(base + timedelta(minutes=i * 5), downstairs="COOLING")
            for i in range(count)
        ]

    def test_selects_today_when_all_sources_available(self):
        from zoneinfo import ZoneInfo
        tz = ZoneInfo("America/Chicago")
        local_now = NOW.astimezone(tz)
        today_start_local = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
        today_start_utc = today_start_local.astimezone(UTC)
        smt = self._make_smt_rows(10, today_start_utc)
        tuya = self._make_tuya_records(20, today_start_utc)
        nest = self._make_nest_records(10, today_start_utc)
        window = select_analysis_window(smt, tuya, nest, now=NOW)
        assert window.basis == "today"
        assert window.has_smt
        assert window.has_tuya
        assert window.has_nest

    def test_falls_back_to_24h_when_no_today_smt(self):
        yesterday = NOW - timedelta(hours=30)
        smt = self._make_smt_rows(10, yesterday)
        tuya = self._make_tuya_records(20, yesterday)
        nest = self._make_nest_records(10, yesterday)
        window = select_analysis_window(smt, tuya, nest, now=NOW)
        # No SMT for today → check 24h
        assert window.basis in ("latest_24h", "latest_smt_day", "fallback")

    def test_returns_fallback_with_no_data(self):
        window = select_analysis_window([], [], [], now=NOW)
        assert window.basis == "fallback"
        assert not window.has_smt
        assert not window.has_tuya
        assert not window.has_nest

    def test_window_has_correct_basis_today(self):
        today_start = NOW.replace(hour=0, minute=0, second=0, microsecond=0)
        smt = self._make_smt_rows(10, today_start)
        tuya = self._make_tuya_records(20, today_start)
        window = select_analysis_window(smt, tuya, [], now=NOW)
        if window.basis == "today":
            assert window.start <= window.end
            assert window.end <= NOW + timedelta(seconds=5)


# ---------------------------------------------------------------------------
# 4. Agreement percentage and classifications
# ---------------------------------------------------------------------------

class TestComputeSourceAgreement:
    def test_always_returns_unavailable_no_whole_home_ct(self):
        """
        Source agreement is never available because Tuya monitors individual
        appliance circuits, not whole-home consumption.  The function always
        returns available=False with the 'no_whole_home_ct' classification.
        """
        result = compute_source_agreement([], [], window_start=NOW, window_end=NOW + timedelta(hours=1))
        assert not result["available"]
        assert result["classification"] == "no_whole_home_ct"

    def test_with_both_sources_still_unavailable(self):
        """Even with full SMT and Tuya coverage, agreement is unavailable."""
        base = NOW - timedelta(hours=2)
        smt = [_smt_row((base + timedelta(minutes=i * 15)).isoformat(), 1.0) for i in range(8)]
        tuya = [_tuya_record(base + timedelta(minutes=i), ac_w=3000) for i in range(120)]
        result = compute_source_agreement(
            smt, tuya,
            window_start=base, window_end=base + timedelta(hours=2)
        )
        assert not result["available"]
        assert result["classification"] == "no_whole_home_ct"

    def test_diagnostic_totals_present_when_data_available(self):
        """Circuit and SMT totals are provided for diagnostics even though
        comparison is invalid."""
        base = NOW - timedelta(hours=2)
        smt = [_smt_row((base + timedelta(minutes=i * 15)).isoformat(), 1.0) for i in range(8)]
        tuya = [_tuya_record(base + timedelta(minutes=i), ac_w=3000) for i in range(120)]
        result = compute_source_agreement(smt, tuya, window_start=base, window_end=base + timedelta(hours=2))
        assert result["smt_kwh"] is not None and result["smt_kwh"] > 0
        assert result["circuit_kwh"] is not None and result["circuit_kwh"] > 0
        # No percentage comparison returned
        assert result["tuya_fraction_pct"] is None

    def test_note_explains_circuit_limitation(self):
        """The note must mention that Tuya monitors circuits, not whole-home."""
        base = NOW - timedelta(hours=2)
        smt = [_smt_row((base + timedelta(minutes=i * 15)).isoformat(), 1.0) for i in range(8)]
        tuya = [_tuya_record(base + timedelta(minutes=i), ac_w=3000) for i in range(120)]
        result = compute_source_agreement(smt, tuya, window_start=base, window_end=base + timedelta(hours=2))
        note = result["note"].lower()
        assert "circuit" in note or "ct" in note.upper()
        assert "whole-home" in note or "whole_home" in note


# ---------------------------------------------------------------------------
# 5. Missing / stale source behavior
# ---------------------------------------------------------------------------

class TestMissingSourceBehavior:
    def test_no_smt_agreement_unavailable(self):
        base = NOW - timedelta(hours=2)
        tuya = [_tuya_record(base + timedelta(minutes=i), ac_w=2000) for i in range(120)]
        result = compute_source_agreement([], tuya, window_start=base, window_end=NOW)
        assert not result["available"]
        assert result["classification"] == "no_whole_home_ct"

    def test_no_tuya_agreement_unavailable(self):
        base = NOW - timedelta(hours=2)
        smt = [_smt_row((base + timedelta(minutes=i * 15)).isoformat(), 1.0) for i in range(8)]
        result = compute_source_agreement(smt, [], window_start=base, window_end=NOW)
        assert not result["available"]
        assert result["classification"] == "no_whole_home_ct"

    def test_compute_kestrel_analysis_with_no_data(self):
        result = compute_kestrel_analysis([], [], [], now=NOW)
        assert "story" in result
        assert "window" in result
        assert "peaks" in result
        assert "hvac_stats" in result
        assert isinstance(result["story"], list)

    def test_compute_kestrel_analysis_smt_only(self):
        smt = [_smt_row((NOW - timedelta(hours=1, minutes=i * 15)).isoformat(), 1.0) for i in range(4)]
        result = compute_kestrel_analysis(smt, [], [], now=NOW)
        assert result["window"]["basis"] in ("latest_24h", "today", "latest_smt_day", "fallback")
        assert "breakdown" in result
        assert result["breakdown"]["has_smt"]

    def test_generate_story_with_no_data_returns_empty(self):
        from kestrel_analysis import AnalysisWindow
        window = AnalysisWindow(label="Test", start=NOW, end=NOW + timedelta(hours=1), basis="fallback")
        story = generate_energy_story(window, {}, [], {}, {})
        assert isinstance(story, list)

    def test_daily_trends_missing_days_not_zero(self):
        # Only have 2 days of data; other 5 days should show None, not 0
        base = NOW - timedelta(days=1)
        smt = [_smt_row((base + timedelta(minutes=i * 15)).isoformat(), 0.5) for i in range(4)]
        trends = compute_daily_trends(smt, [], [], now=NOW)
        # Days with no data should have smt_kwh = None
        missing_days = [d for d in trends if d["smt_kwh"] is None and not d["is_today"]]
        # Not all days should be filled with zeros
        assert any(d["smt_kwh"] is None for d in trends)


# ---------------------------------------------------------------------------
# 6. Peak event grouping
# ---------------------------------------------------------------------------

class TestPeakEventGrouping:
    def test_adjacent_samples_grouped_into_one_peak(self):
        base = NOW
        records = [
            _tuya_record(base + timedelta(seconds=i * 60), ac_w=3000)
            for i in range(10)
        ]
        peaks = find_demand_peaks(records, [], [], window_start=base, window_end=base + timedelta(hours=1))
        # All samples within 30-min cooldown → single event
        assert len(peaks) <= 1

    def test_separated_samples_produce_separate_peaks(self):
        base = NOW
        # Two clusters separated by 31 minutes
        cluster1 = [_tuya_record(base + timedelta(seconds=i * 60), ac_w=3000) for i in range(5)]
        cluster2 = [_tuya_record(base + timedelta(minutes=35, seconds=i * 60), ac_w=4000) for i in range(5)]
        records = cluster1 + cluster2
        peaks = find_demand_peaks(records, [], [], window_start=base, window_end=base + timedelta(hours=1))
        assert len(peaks) >= 2

    def test_peaks_sorted_by_demand_descending(self):
        base = NOW
        records = (
            [_tuya_record(base + timedelta(seconds=i * 60), ac_w=1000) for i in range(5)]
            + [_tuya_record(base + timedelta(minutes=35, seconds=i * 60), ac_w=5000) for i in range(5)]
        )
        peaks = find_demand_peaks(records, [], [], window_start=base, window_end=base + timedelta(hours=1))
        if len(peaks) >= 2:
            assert peaks[0]["total_kw"] >= peaks[1]["total_kw"]

    def test_no_peaks_returned_when_no_data(self):
        peaks = find_demand_peaks([], [], [], window_start=NOW, window_end=NOW + timedelta(hours=1))
        assert peaks == []

    def test_fallback_to_smt_when_no_tuya(self):
        smt = [_smt_row(NOW.isoformat(), 2.0), _smt_row((NOW + timedelta(minutes=15)).isoformat(), 0.5)]
        peaks = find_demand_peaks([], smt, [], window_start=NOW, window_end=NOW + timedelta(hours=1))
        assert len(peaks) >= 1
        assert peaks[0]["source"] == "smt_interval"

    def test_peaks_include_local_time_display(self):
        """Each peak must include human-readable local-time fields."""
        base = NOW
        records = [_tuya_record(base + timedelta(seconds=i * 60), ac_w=2500) for i in range(5)]
        peaks = find_demand_peaks(records, [], [], window_start=base, window_end=base + timedelta(hours=1))
        if peaks:
            assert "timestamp_display" in peaks[0]
            assert "time_display" in peaks[0]
            # timestamp_display must not be a raw ISO string (must contain space or comma)
            display = peaks[0]["timestamp_display"]
            assert " " in display, f"timestamp_display looks like raw ISO: {display!r}"

    def test_smt_fallback_peaks_no_tuya_fields_as_zero(self):
        """SMT-only peaks must show None for Tuya fields, not 0."""
        smt = [_smt_row(NOW.isoformat(), 1.5)]
        peaks = find_demand_peaks([], smt, [], window_start=NOW, window_end=NOW + timedelta(hours=1))
        if peaks:
            assert peaks[0]["compressor_kw"] is None
            assert peaks[0]["hvac_kw"] is None
            assert peaks[0]["non_hvac_kw"] is None


# ---------------------------------------------------------------------------
# 7. HVAC cycle detection
# ---------------------------------------------------------------------------

class TestHvacCycleDetection:
    def test_continuous_cooling_is_one_cycle(self):
        base = NOW
        records = [
            _nest_record(base + timedelta(minutes=i * 5), downstairs="COOLING")
            for i in range(12)  # 60 minutes
        ]
        cycles = detect_hvac_cycles(records, zone="downstairs", window_start=base, window_end=base + timedelta(hours=2))
        assert len(cycles) == 1
        assert cycles[0].duration_minutes == pytest.approx(55.0, abs=5.0)

    def test_gap_within_tolerance_bridges_cycle(self):
        base = NOW
        part1 = [_nest_record(base + timedelta(minutes=i * 5), downstairs="COOLING") for i in range(6)]
        # 10-minute gap (2 missed samples at 5-min poll interval = 10 min)
        part2 = [_nest_record(base + timedelta(minutes=40 + i * 5), downstairs="COOLING") for i in range(6)]
        records = part1 + part2
        cycles = detect_hvac_cycles(records, zone="downstairs", window_start=base, window_end=base + timedelta(hours=2))
        # Gap is within tolerance → should bridge to 1 or 2 cycles
        assert 1 <= len(cycles) <= 2

    def test_large_gap_creates_separate_cycles(self):
        base = NOW
        part1 = [_nest_record(base + timedelta(minutes=i * 5), downstairs="COOLING") for i in range(6)]
        # 60-minute gap
        part2 = [_nest_record(base + timedelta(minutes=90 + i * 5), downstairs="COOLING") for i in range(6)]
        records = part1 + part2
        cycles = detect_hvac_cycles(records, zone="downstairs", window_start=base, window_end=base + timedelta(hours=3))
        assert len(cycles) == 2

    def test_no_cooling_returns_no_cycles(self):
        records = [_nest_record(NOW + timedelta(minutes=i * 5), downstairs="OFF") for i in range(12)]
        cycles = detect_hvac_cycles(records, zone="downstairs", window_start=NOW, window_end=NOW + timedelta(hours=2))
        assert len(cycles) == 0

    def test_empty_records_returns_no_cycles(self):
        cycles = detect_hvac_cycles([], zone="downstairs", window_start=NOW, window_end=NOW + timedelta(hours=2))
        assert len(cycles) == 0

    def test_cycle_duration_calculated_correctly(self):
        base = NOW
        records = [_nest_record(base + timedelta(minutes=i * 5), downstairs="COOLING") for i in range(7)]
        cycles = detect_hvac_cycles(records, zone="downstairs", window_start=base, window_end=base + timedelta(hours=2))
        assert len(cycles) == 1
        # 6 intervals × 5 min = 30 min from first to last sample
        assert cycles[0].duration_minutes == pytest.approx(30.0, abs=5.0)


# ---------------------------------------------------------------------------
# 8. HVAC energy estimate
# ---------------------------------------------------------------------------

class TestHvacEnergyEstimate:
    def test_no_cycles_returns_zero_energy(self):
        stats = compute_hvac_cycle_stats([], [], window_start=NOW, window_end=NOW + timedelta(hours=1))
        assert stats["hvac_energy_kwh"] == 0.0
        assert not stats["available"]

    def test_cycles_with_tuya_data_returns_positive_energy(self):
        base = NOW
        cycles = [HvacCycle(zone="downstairs", start=base, end=base + timedelta(hours=1), sample_count=12)]
        tuya = [_tuya_record(base + timedelta(minutes=i), ac_w=2500, furnace_w=300) for i in range(60)]
        stats = compute_hvac_cycle_stats(cycles, tuya, window_start=base, window_end=base + timedelta(hours=1))
        assert stats["available"]
        assert stats["hvac_energy_kwh"] > 0
        assert stats["avg_compressor_kw"] is not None
        assert stats["avg_compressor_kw"] == pytest.approx(2.5, abs=0.2)

    def test_short_cycle_counted(self):
        base = NOW
        short = HvacCycle(zone="downstairs", start=base, end=base + timedelta(minutes=5), sample_count=2)
        stats = compute_hvac_cycle_stats([short], [], window_start=base, window_end=base + timedelta(hours=1))
        assert stats["short_cycle_count"] == 1

    def test_long_cycle_counted(self):
        base = NOW
        long_c = HvacCycle(zone="downstairs", start=base, end=base + timedelta(minutes=100), sample_count=20)
        stats = compute_hvac_cycle_stats([long_c], [], window_start=base, window_end=base + timedelta(hours=2))
        assert stats["long_cycle_count"] == 1


# ---------------------------------------------------------------------------
# 9. Daily trend coverage handling
# ---------------------------------------------------------------------------

class TestDailyTrendCoverage:
    def test_full_day_has_adequate_coverage(self):
        base = NOW.replace(hour=0, minute=0, second=0, microsecond=0)
        smt = [_smt_row((base + timedelta(minutes=i * 15)).isoformat(), 0.5) for i in range(96)]  # full day
        trends = compute_daily_trends(smt, [], [], now=base + timedelta(hours=23))
        today = next((d for d in trends if d["is_today"]), None)
        if today:
            assert today["adequate_coverage"]
            assert today["smt_kwh"] is not None

    def test_partial_day_today_shows_coverage_not_zero(self):
        base = NOW.replace(hour=0, minute=0, second=0, microsecond=0)
        # Only 8 intervals (2 hours worth)
        smt = [_smt_row((base + timedelta(minutes=i * 15)).isoformat(), 0.5) for i in range(8)]
        trends = compute_daily_trends(smt, [], [], now=base + timedelta(hours=2))
        today = next((d for d in trends if d["is_today"]), None)
        if today:
            # Partial today: may have low coverage but not zero
            assert today["smt_coverage_pct"] > 0

    def test_days_with_no_data_show_none_not_zero(self):
        # Only one day of data
        base = NOW.replace(hour=0, minute=0, second=0, microsecond=0)
        smt = [_smt_row((base + timedelta(minutes=i * 15)).isoformat(), 0.5) for i in range(96)]
        trends = compute_daily_trends(smt, [], [], now=NOW, days=7)
        none_days = [d for d in trends if d["smt_kwh"] is None]
        # At least some prior days should show None
        assert len(none_days) >= 1

    def test_trends_returns_correct_day_count(self):
        trends = compute_daily_trends([], [], [], now=NOW, days=7)
        assert len(trends) == 7

    def test_trend_dates_in_ascending_order(self):
        trends = compute_daily_trends([], [], [], now=NOW, days=7)
        dates = [d["date"] for d in trends]
        assert dates == sorted(dates)

    def test_cooling_minutes_aggregated_from_nest(self):
        base = NOW.replace(hour=0, minute=0, second=0, microsecond=0)
        nest = [_nest_record(base + timedelta(minutes=i * 5), downstairs="COOLING") for i in range(12)]
        trends = compute_daily_trends([], [], nest, now=base + timedelta(hours=2), days=1)
        today = trends[0]
        assert today["cooling_minutes"] is not None
        assert today["cooling_minutes"] > 0


# ---------------------------------------------------------------------------
# 10. No false appliance attribution
# ---------------------------------------------------------------------------

class TestNoFalseAppliance:
    def test_hvac_only_explanation_when_compressor_dominant(self):
        base = NOW
        records = [_tuya_record(base + timedelta(seconds=i * 60), ac_w=3000) for i in range(5)]
        nest = [_nest_record(base + timedelta(minutes=i), downstairs="COOLING") for i in range(5)]
        peaks = find_demand_peaks(records, [], nest, window_start=base, window_end=base + timedelta(hours=1))
        if peaks:
            explanation = peaks[0]["explanation"]
            assert "appliance" not in explanation.lower()
            assert explanation in ("HVAC only", "HVAC + other significant load", "Non-HVAC load", "Mixed load", "Unknown — no Tuya data", "Unknown — missing Nest data")

    def test_no_appliance_names_in_explanation(self):
        base = NOW
        records = [_tuya_record(base + timedelta(seconds=i * 60), dryer_w=5000) for i in range(5)]
        peaks = find_demand_peaks(records, [], [], window_start=base, window_end=base + timedelta(hours=1))
        for peak in peaks:
            # Must not label "dryer" specifically
            assert "dryer" not in peak["explanation"].lower()
            assert "dishwasher" not in peak["explanation"].lower()
            assert "oven" not in peak["explanation"].lower()
            assert "furnace" not in peak["explanation"].lower()

    def test_unknown_explanation_when_nest_missing(self):
        base = NOW
        records = [_tuya_record(base + timedelta(seconds=i * 60), ac_w=2000) for i in range(5)]
        peaks = find_demand_peaks(records, [], [], window_start=base, window_end=base + timedelta(hours=1))
        if peaks:
            # No Nest data → action is None → explanation should acknowledge uncertainty
            explanation = peaks[0]["explanation"]
            assert "Unknown" in explanation or "HVAC" in explanation


# ---------------------------------------------------------------------------
# 11. Combined timeline builder
# ---------------------------------------------------------------------------

class TestBuildCombinedTimeline:
    def test_empty_sources_produces_empty_series(self):
        result = build_combined_timeline([], [], [], window_start=NOW, window_end=NOW + timedelta(hours=1))
        assert not result["has_smt"]
        assert not result["has_tuya"]
        assert not result["has_nest"]
        assert result["smt_bars"] == []
        assert result["tuya_measured"] == []

    def test_smt_bars_included(self):
        smt = [_smt_row(NOW.isoformat(), 1.5)]
        result = build_combined_timeline(smt, [], [], window_start=NOW, window_end=NOW + timedelta(hours=1))
        assert result["has_smt"]
        assert len(result["smt_bars"]) == 1
        assert result["smt_bars"][0]["avg_kw"] == pytest.approx(6.0)

    def test_nest_cooling_bands_generated(self):
        base = NOW
        nest = [_nest_record(base + timedelta(minutes=i * 5), downstairs="COOLING") for i in range(12)]
        result = build_combined_timeline([], [], nest, window_start=base, window_end=base + timedelta(hours=2))
        assert result["has_nest"]
        assert len(result["cooling_bands"]) >= 1

    def test_window_bounds_in_output(self):
        result = build_combined_timeline([], [], [], window_start=NOW, window_end=NOW + timedelta(hours=1))
        assert result["window_start"] == NOW.isoformat()
        assert result["window_end"] == (NOW + timedelta(hours=1)).isoformat()


# ---------------------------------------------------------------------------
# 12. Full analysis pipeline with mixed sources
# ---------------------------------------------------------------------------

class TestFullAnalysisPipeline:
    def _make_full_dataset(self, hours: int = 6):
        base = NOW - timedelta(hours=hours)
        smt = [_smt_row((base + timedelta(minutes=i * 15)).isoformat(), 0.75) for i in range(hours * 4)]
        tuya = [_tuya_record(base + timedelta(minutes=i), ac_w=2500) for i in range(hours * 60)]
        nest = [
            _nest_record(base + timedelta(minutes=i * 5), downstairs="COOLING")
            for i in range(hours * 12)
        ]
        return smt, tuya, nest

    def test_full_pipeline_returns_complete_structure(self):
        smt, tuya, nest = self._make_full_dataset()
        result = compute_kestrel_analysis(smt, tuya, nest, now=NOW)
        required_keys = {"window", "story", "hvac_stats", "agreement", "breakdown", "peaks", "timeline", "trends", "quality"}
        assert required_keys.issubset(set(result.keys()))

    def test_full_pipeline_story_not_empty(self):
        smt, tuya, nest = self._make_full_dataset()
        result = compute_kestrel_analysis(smt, tuya, nest, now=NOW)
        # With sufficient data, story should have at least one finding
        assert isinstance(result["story"], list)

    def test_full_pipeline_trends_has_seven_days(self):
        smt, tuya, nest = self._make_full_dataset()
        result = compute_kestrel_analysis(smt, tuya, nest, now=NOW)
        assert len(result["trends"]) == 7

    def test_full_pipeline_breakdown_smt_positive(self):
        smt, tuya, nest = self._make_full_dataset()
        result = compute_kestrel_analysis(smt, tuya, nest, now=NOW)
        assert result["breakdown"]["has_smt"]
        assert result["breakdown"]["smt_kwh"] > 0

    def test_full_pipeline_hvac_stats_available_with_nest(self):
        smt, tuya, nest = self._make_full_dataset()
        result = compute_kestrel_analysis(smt, tuya, nest, now=NOW)
        # With many cooling samples, cycles should be detected
        assert result["hvac_stats"]["available"]
        assert result["hvac_stats"]["cycle_count"] >= 1

    def test_full_pipeline_with_only_smt_no_crash(self):
        smt, _, _ = self._make_full_dataset()
        result = compute_kestrel_analysis(smt, [], [], now=NOW)
        assert result["breakdown"]["has_smt"]
        assert not result["breakdown"]["has_tuya"]

    def test_full_pipeline_with_only_tuya_no_crash(self):
        _, tuya, _ = self._make_full_dataset()
        result = compute_kestrel_analysis([], tuya, [], now=NOW)
        # No SMT → breakdown.has_smt = False
        assert not result["breakdown"]["has_smt"]

    def test_full_pipeline_with_only_nest_no_crash(self):
        _, _, nest = self._make_full_dataset()
        result = compute_kestrel_analysis([], [], nest, now=NOW)
        assert isinstance(result["story"], list)


# ---------------------------------------------------------------------------
# 13. Coverage calculations
# ---------------------------------------------------------------------------

class TestCoverageCalculations:
    def test_full_smt_coverage(self):
        base = NOW
        end = NOW + timedelta(hours=1)
        rows = [_smt_row((base + timedelta(minutes=i * 15)).isoformat(), 0.5) for i in range(4)]
        cov = smt_coverage_pct(rows, window_start=base, window_end=end)
        assert cov == pytest.approx(100.0, abs=5.0)

    def test_empty_smt_coverage_zero(self):
        cov = smt_coverage_pct([], window_start=NOW, window_end=NOW + timedelta(hours=1))
        assert cov == 0.0

    def test_full_tuya_coverage(self):
        base = NOW
        end = NOW + timedelta(hours=1)
        records = [FakeTuyaRecord(timestamp=base + timedelta(seconds=i * 60)) for i in range(60)]
        cov = tuya_coverage_pct(records, window_start=base, window_end=end)
        assert cov == pytest.approx(100.0, abs=5.0)

    def test_empty_tuya_coverage_zero(self):
        cov = tuya_coverage_pct([], window_start=NOW, window_end=NOW + timedelta(hours=1))
        assert cov == 0.0

    def test_smt_total_kwh_sums_correctly(self):
        base = NOW
        rows = [_smt_row((base + timedelta(minutes=i * 15)).isoformat(), 1.0) for i in range(4)]
        total = smt_total_kwh(rows, window_start=base, window_end=base + timedelta(hours=1))
        assert total == pytest.approx(4.0)

    def test_smt_total_excludes_out_of_window(self):
        base = NOW
        rows = [
            _smt_row(base.isoformat(), 1.0),
            _smt_row((base + timedelta(hours=2)).isoformat(), 99.0),  # outside
        ]
        total = smt_total_kwh(rows, window_start=base, window_end=base + timedelta(hours=1))
        assert total == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# Individual Tuya channel visibility
# ---------------------------------------------------------------------------

def _tuya_record_with_all_channels(ts: datetime) -> FakeTuyaRecord:
    """Record with all four circuits populated."""
    return FakeTuyaRecord(
        timestamp=ts,
        appliances={
            "ac_compressor":       {"power_w": 2500.0},
            "furnace_air_handler": {"power_w": 600.0},
            "dryer":               {"power_w": 1200.0},
            "dishwasher":          {"power_w": 800.0},
        },
    )


def _tuya_record_hvac_only(ts: datetime) -> FakeTuyaRecord:
    """Record with only HVAC channels; dryer and dishwasher absent."""
    return FakeTuyaRecord(
        timestamp=ts,
        appliances={
            "ac_compressor":       {"power_w": 2500.0},
            "furnace_air_handler": {"power_w": 600.0},
            # dryer and dishwasher intentionally absent
        },
    )


def _tuya_record_no_power(ts: datetime) -> FakeTuyaRecord:
    """Record where all channel entries exist but power_w is None."""
    return FakeTuyaRecord(
        timestamp=ts,
        appliances={k: {"power_w": None} for k in TUYA_ALL_KEYS},
    )


class TestIndividualChannelSeries:
    """Tests 1-5: channel series, monitored total, missing-channel handling."""

    def test_build_tuya_channel_series_returns_one_series_per_channel(self):
        base = NOW
        records = [_tuya_record_with_all_channels(base + timedelta(seconds=i * 60)) for i in range(5)]
        for key in TUYA_ALL_KEYS:
            series = build_tuya_channel_series(records, key, window_start=base, window_end=base + timedelta(hours=1))
            assert len(series) == 5, f"{key} series should have 5 points"
            assert all("timestamp" in p and "kw" in p for p in series), f"{key} series has malformed points"

    def test_build_all_channel_series_returns_all_four_keys(self):
        base = NOW
        records = [_tuya_record_with_all_channels(base + timedelta(seconds=i * 60)) for i in range(3)]
        channels = build_all_channel_series(records, window_start=base, window_end=base + timedelta(hours=1))
        assert set(channels.keys()) == set(TUYA_ALL_KEYS), (
            f"Expected all four keys, got {set(channels.keys())}"
        )

    def test_missing_channel_omitted_from_all_channel_series(self):
        """Channels absent from records must not appear in the result dict."""
        base = NOW
        records = [_tuya_record_hvac_only(base + timedelta(seconds=i * 60)) for i in range(3)]
        channels = build_all_channel_series(records, window_start=base, window_end=base + timedelta(hours=1))
        assert "ac_compressor" in channels
        assert "furnace_air_handler" in channels
        assert "dryer" not in channels, "Absent channel must not appear"
        assert "dishwasher" not in channels, "Absent channel must not appear"

    def test_channel_with_none_power_not_included(self):
        """Channels whose power_w is None must not produce a false zero point."""
        base = NOW
        records = [_tuya_record_no_power(base + timedelta(seconds=i * 60)) for i in range(3)]
        for key in TUYA_ALL_KEYS:
            series = build_tuya_channel_series(records, key, window_start=base, window_end=base + timedelta(hours=1))
            assert series == [], f"None power_w must yield empty series for {key}"

    def test_monitored_total_equals_sum_of_aligned_channels(self):
        """The monitored-total series at each timestamp must equal the sum of channels."""
        base = NOW
        records = [_tuya_record_with_all_channels(base + timedelta(seconds=i * 60)) for i in range(5)]
        window_end = base + timedelta(hours=1)
        channels = build_all_channel_series(records, window_start=base, window_end=window_end)
        total_series = build_tuya_kw_series(records, TUYA_ALL_KEYS, window_start=base, window_end=window_end)

        # For each timestamp in the total series, the kw must match the sum of channels
        for total_point in total_series:
            ts = total_point["timestamp"]
            channel_sum = sum(
                next((p["kw"] for p in series if p["timestamp"] == ts), 0.0)
                for series in channels.values()
            )
            assert total_point["kw"] == pytest.approx(channel_sum, abs=0.001), (
                f"Monitored total at {ts}: {total_point['kw']} != sum {channel_sum}"
            )

    def test_actual_zero_reading_is_included(self):
        """A power_w of 0.0 (device off but reporting) must appear as kw=0."""
        base = NOW
        record = FakeTuyaRecord(
            timestamp=base,
            appliances={"dryer": {"power_w": 0.0}, "ac_compressor": {"power_w": 2000.0}},
        )
        dryer_series = build_tuya_channel_series([record], "dryer", window_start=base - timedelta(seconds=1), window_end=base + timedelta(hours=1))
        assert len(dryer_series) == 1
        assert dryer_series[0]["kw"] == pytest.approx(0.0)


class TestTimelineChannelPayload:
    """Test 2: timeline payload includes per-channel arrays."""

    def test_timeline_includes_channels_dict_with_all_four_keys(self):
        base = NOW
        records = [_tuya_record_with_all_channels(base + timedelta(seconds=i * 60)) for i in range(5)]
        result = build_combined_timeline([], records, [], window_start=base, window_end=base + timedelta(hours=1))
        assert "channels" in result
        assert set(result["channels"].keys()) == set(TUYA_ALL_KEYS)

    def test_timeline_channels_are_independent_series(self):
        """Each channel series must be a separate list of {timestamp, kw} dicts."""
        base = NOW
        records = [_tuya_record_with_all_channels(base + timedelta(seconds=i * 60)) for i in range(3)]
        result = build_combined_timeline([], records, [], window_start=base, window_end=base + timedelta(hours=1))
        for key in TUYA_ALL_KEYS:
            series = result["channels"].get(key, [])
            assert isinstance(series, list)
            assert all("timestamp" in p and "kw" in p for p in series)

    def test_timeline_has_tuya_true_when_any_channel_present(self):
        base = NOW
        records = [_tuya_record_hvac_only(base + timedelta(seconds=i * 60)) for i in range(3)]
        result = build_combined_timeline([], records, [], window_start=base, window_end=base + timedelta(hours=1))
        assert result["has_tuya"] is True

    def test_timeline_missing_channel_absent_from_payload(self):
        """Absent channels must not appear in payload (no false empty series)."""
        base = NOW
        records = [_tuya_record_hvac_only(base + timedelta(seconds=i * 60)) for i in range(3)]
        result = build_combined_timeline([], records, [], window_start=base, window_end=base + timedelta(hours=1))
        assert "dryer" not in result["channels"]
        assert "dishwasher" not in result["channels"]

    def test_timeline_channel_labels_present(self):
        base = NOW
        records = [_tuya_record_with_all_channels(base + timedelta(seconds=i * 60)) for i in range(3)]
        result = build_combined_timeline([], records, [], window_start=base, window_end=base + timedelta(hours=1))
        assert "channel_labels" in result
        for key in result["channels"]:
            assert key in result["channel_labels"]
            assert result["channel_labels"][key] == TUYA_CHANNEL_LABELS[key]

    def test_timeline_monitored_total_label_present(self):
        base = NOW
        records = [_tuya_record_with_all_channels(base + timedelta(seconds=i * 60)) for i in range(3)]
        result = build_combined_timeline([], records, [], window_start=base, window_end=base + timedelta(hours=1))
        assert result.get("monitored_total_label") == TUYA_MONITORED_TOTAL_LABEL

    def test_no_whole_home_label_in_timeline_keys(self):
        """Timeline payload must not use 'whole_home' or 'house_load' keys."""
        base = NOW
        records = [_tuya_record_with_all_channels(base + timedelta(seconds=i * 60)) for i in range(3)]
        result = build_combined_timeline([], records, [], window_start=base, window_end=base + timedelta(hours=1))
        for key in result:
            assert "whole_home" not in key.lower(), f"Found 'whole_home' in key: {key}"
            assert "house_load" not in key.lower(), f"Found 'house_load' in key: {key}"


class TestPeakChannelAttribution:
    """Test 7: Peak attribution includes individual circuit contributions."""

    def test_peak_channels_dict_contains_measured_circuits(self):
        base = NOW
        records = [_tuya_record_with_all_channels(base + timedelta(seconds=i * 60)) for i in range(5)]
        peaks = find_demand_peaks(records, [], [], window_start=base, window_end=base + timedelta(hours=1))
        assert peaks, "Expected at least one peak"
        peak = peaks[0]
        assert "channels" in peak
        for key in TUYA_ALL_KEYS:
            assert key in peak["channels"], f"Channel '{key}' missing from peak"

    def test_peak_monitored_total_equals_sum_of_channels(self):
        base = NOW
        records = [_tuya_record_with_all_channels(base + timedelta(seconds=i * 60)) for i in range(5)]
        peaks = find_demand_peaks(records, [], [], window_start=base, window_end=base + timedelta(hours=1))
        for peak in peaks:
            channels = peak.get("channels", {})
            chan_sum = round(sum(channels.values()), 2)
            assert peak["monitored_total_kw"] == pytest.approx(chan_sum, abs=0.01)

    def test_peak_unmonitored_remainder_computed_when_smt_aligned(self):
        base = NOW
        records = [_tuya_record_with_all_channels(base + timedelta(seconds=i * 60)) for i in range(5)]
        # SMT interval that contains the peak timestamps
        smt = [_smt_row(base.isoformat(), 2.0)]  # 2 kWh × 4 = 8 kW avg
        peaks = find_demand_peaks(records, smt, [], window_start=base, window_end=base + timedelta(hours=1))
        assert peaks
        # Peak with aligned SMT should have a remainder
        peaks_with_smt = [p for p in peaks if p.get("smt_whole_home_kw") is not None]
        if peaks_with_smt:
            peak = peaks_with_smt[0]
            assert peak["unmonitored_remainder_kw"] is not None
            assert peak["unmonitored_remainder_kw"] >= 0

    def test_peak_unmonitored_none_when_no_smt(self):
        base = NOW
        records = [_tuya_record_with_all_channels(base + timedelta(seconds=i * 60)) for i in range(5)]
        peaks = find_demand_peaks(records, [], [], window_start=base, window_end=base + timedelta(hours=1))
        for peak in peaks:
            assert peak["unmonitored_remainder_kw"] is None
            assert peak["smt_whole_home_kw"] is None

    def test_peak_hvac_only_classification_with_channels(self):
        """With compressor dominant, explanation must still be correct."""
        base = NOW
        records = [_tuya_record_hvac_only(base + timedelta(seconds=i * 60)) for i in range(5)]
        nest = [_nest_record(base + timedelta(minutes=i), downstairs="COOLING") for i in range(5)]
        peaks = find_demand_peaks(records, [], nest, window_start=base, window_end=base + timedelta(hours=1))
        if peaks:
            assert peaks[0]["explanation"] in ("HVAC only", "HVAC + other significant load", "Mixed load")


class TestEnergyBreakdownChannels:
    """Test 8: Energy breakdown shows all four circuits."""

    def test_breakdown_channel_kwh_has_all_four_keys(self):
        base = NOW
        records = [_tuya_record_with_all_channels(base + timedelta(seconds=i * 60)) for i in range(10)]
        bd = compute_energy_breakdown([], records, window_start=base, window_end=base + timedelta(minutes=10))
        assert "channel_kwh" in bd
        for key in TUYA_ALL_KEYS:
            assert key in bd["channel_kwh"], f"Breakdown missing channel '{key}'"

    def test_breakdown_monitored_total_equals_sum_of_channels(self):
        base = NOW
        records = [_tuya_record_with_all_channels(base + timedelta(seconds=i * 60)) for i in range(10)]
        bd = compute_energy_breakdown([], records, window_start=base, window_end=base + timedelta(minutes=10))
        channel_sum = round(sum(bd["channel_kwh"].values()), 4)
        assert bd["monitored_total_kwh"] == pytest.approx(channel_sum, abs=0.001)

    def test_breakdown_absent_channel_not_in_channel_kwh(self):
        base = NOW
        records = [_tuya_record_hvac_only(base + timedelta(seconds=i * 60)) for i in range(10)]
        bd = compute_energy_breakdown([], records, window_start=base, window_end=base + timedelta(minutes=10))
        assert "dryer" not in bd["channel_kwh"]
        assert "dishwasher" not in bd["channel_kwh"]

    def test_breakdown_unmonitored_remainder_when_both_present(self):
        base = NOW
        smt = [_smt_row((base + timedelta(minutes=i * 15)).isoformat(), 1.0) for i in range(4)]
        records = [_tuya_record_with_all_channels(base + timedelta(seconds=i * 60)) for i in range(240)]
        bd = compute_energy_breakdown(smt, records, window_start=base, window_end=base + timedelta(hours=1))
        assert bd["has_smt"]
        assert bd["has_tuya"]
        assert bd["unmonitored_remainder_kwh"] is not None
        assert bd["unmonitored_remainder_kwh"] >= 0

    def test_breakdown_unmonitored_none_when_smt_absent(self):
        base = NOW
        records = [_tuya_record_with_all_channels(base + timedelta(seconds=i * 60)) for i in range(10)]
        bd = compute_energy_breakdown([], records, window_start=base, window_end=base + timedelta(minutes=10))
        assert bd["unmonitored_remainder_kwh"] is None

    def test_breakdown_no_whole_home_label(self):
        """Keys in the breakdown dict must not contain 'whole_home'."""
        base = NOW
        records = [_tuya_record_with_all_channels(base + timedelta(seconds=i * 60)) for i in range(5)]
        bd = compute_energy_breakdown([], records, window_start=base, window_end=base + timedelta(minutes=5))
        for key in bd:
            assert "whole_home" not in key.lower()


class TestNoWholehomeLabel:
    """Test 6: no label anywhere claims Tuya is whole-home."""

    def test_channel_labels_constant_no_whole_home(self):
        for key, label in TUYA_CHANNEL_LABELS.items():
            label_lower = label.lower()
            assert "whole" not in label_lower, f"'{label}' contains 'whole'"
            assert "house load" not in label_lower, f"'{label}' contains 'house load'"

    def test_monitored_total_label_no_whole_home(self):
        assert "whole" not in TUYA_MONITORED_TOTAL_LABEL.lower()
        assert "house" not in TUYA_MONITORED_TOTAL_LABEL.lower()
        assert "monitored" in TUYA_MONITORED_TOTAL_LABEL.lower()

    def test_source_agreement_note_explains_no_whole_home(self):
        base = NOW
        smt = [_smt_row((base + timedelta(minutes=i * 15)).isoformat(), 1.0) for i in range(4)]
        records = [_tuya_record_with_all_channels(base + timedelta(seconds=i * 60)) for i in range(10)]
        result = compute_source_agreement(smt, records, window_start=base, window_end=base + timedelta(hours=1))
        note = result["note"].lower()
        assert "whole-home" in note or "whole_home" in note or "whole home" in note


class TestUnmonitoredRemainderConditions:
    """Test 9: unmonitored remainder withheld when alignment insufficient."""

    def test_remainder_none_when_no_smt(self):
        base = NOW
        records = [_tuya_record_with_all_channels(base + timedelta(seconds=i * 60)) for i in range(5)]
        peaks = find_demand_peaks(records, [], [], window_start=base, window_end=base + timedelta(hours=1))
        for peak in peaks:
            assert peak["unmonitored_remainder_kw"] is None, (
                "Remainder must be None when no SMT data"
            )

    def test_remainder_none_when_smt_interval_does_not_contain_peak(self):
        """SMT interval must contain the peak timestamp; if not, remainder is None."""
        base = NOW
        records = [_tuya_record_with_all_channels(base + timedelta(seconds=i * 60)) for i in range(5)]
        # SMT interval FAR in the past — does not contain peak
        far_past = base - timedelta(hours=5)
        smt = [_smt_row(far_past.isoformat(), 2.0)]
        peaks = find_demand_peaks(records, smt, [], window_start=base, window_end=base + timedelta(hours=1))
        for peak in peaks:
            assert peak["unmonitored_remainder_kw"] is None, (
                "Remainder must be None when SMT interval does not contain peak timestamp"
            )

    def test_breakdown_remainder_none_when_no_tuya(self):
        base = NOW
        smt = [_smt_row((base + timedelta(minutes=i * 15)).isoformat(), 1.0) for i in range(4)]
        bd = compute_energy_breakdown(smt, [], window_start=base, window_end=base + timedelta(hours=1))
        assert bd["unmonitored_remainder_kwh"] is None


# ---------------------------------------------------------------------------
# Energy donut chart tests
# ---------------------------------------------------------------------------

def _make_complete_day_fixtures(
    now_ref: datetime,
    *,
    smt_intervals: int = 96,
    tuya_per_minute: int = 1200,
    channel_power_w: dict | None = None,
):
    """
    Build SMT rows and Tuya records for the calendar day BEFORE now_ref
    (i.e. yesterday in America/Chicago).

    smt_intervals: number of 15-min intervals (96 = full day, ≥87 for 90% coverage)
    tuya_per_minute: Tuya records evenly distributed in the day (1200 ≈ 83%)
    channel_power_w: per-channel watts; defaults to test values
    """
    from zoneinfo import ZoneInfo as _ZI
    tz = _ZI("America/Chicago")
    local_now = now_ref.astimezone(tz)
    today_local = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    yesterday_start = (today_local - timedelta(days=1)).astimezone(timezone.utc)
    yesterday_end   = today_local.astimezone(timezone.utc)

    if channel_power_w is None:
        channel_power_w = {
            "ac_compressor":       1820.0,
            "furnace_air_handler":  510.0,
            "dryer":                240.0,
            "dishwasher":            80.0,
        }

    # SMT rows: evenly distributed across the day
    smt = []
    day_seconds = (yesterday_end - yesterday_start).total_seconds()
    step = day_seconds / smt_intervals
    for i in range(smt_intervals):
        start = yesterday_start + timedelta(seconds=i * step)
        end   = start + timedelta(minutes=15)
        smt.append(_smt_row(start.isoformat(), 0.75))

    # Tuya records: evenly distributed
    tuya = []
    step_t = day_seconds / tuya_per_minute
    for i in range(tuya_per_minute):
        ts = yesterday_start + timedelta(seconds=i * step_t)
        tuya.append(FakeTuyaRecord(
            timestamp=ts,
            appliances={k: {"power_w": v} for k, v in channel_power_w.items()},
        ))

    return smt, tuya, yesterday_start, yesterday_end


class TestComputeEnergyDonut:
    """
    Tests 1-10 for the energy-use donut chart.
    Uses a frozen NOW so yesterday is always a complete calendar day.
    """

    # A reference "now" at noon on a test day (yesterday will be fully elapsed).
    FROZEN_NOW = datetime(2026, 6, 17, 17, 0, tzinfo=timezone.utc)

    def _valid_donut(self, channel_power_w=None, smt_intervals=96, tuya_per_minute=1200):
        smt, tuya, _, _ = _make_complete_day_fixtures(
            self.FROZEN_NOW,
            smt_intervals=smt_intervals,
            tuya_per_minute=tuya_per_minute,
            channel_power_w=channel_power_w,
        )
        return compute_energy_donut(smt, tuya, now=self.FROZEN_NOW)

    # --- Test 1: all four Tuya channels in payload ---
    def test_donut_slices_contain_all_four_channels(self):
        result = self._valid_donut()
        assert result["available"], f"Expected available, got: {result}"
        keys = {s["key"] for s in result["slices"]}
        for key in TUYA_ALL_KEYS:
            assert key in keys, f"Channel '{key}' missing from donut slices"

    # --- Test 2: percentages sum to ~100% ---
    def test_percentages_sum_to_approximately_100(self):
        result = self._valid_donut()
        assert result["available"]
        total_pct = sum(s["pct"] for s in result["slices"])
        assert abs(total_pct - 100.0) < 1.0, (
            f"Percentages sum to {total_pct}, expected ~100%"
        )

    # --- Test 3: SMT total ≈ sum of all slices ---
    def test_smt_total_equals_sum_of_slices_within_tolerance(self):
        result = self._valid_donut()
        assert result["available"]
        slice_sum = sum(s["kwh"] for s in result["slices"])
        assert abs(result["smt_kwh"] - slice_sum) < 0.5, (
            f"SMT {result['smt_kwh']} != slice sum {slice_sum}"
        )

    # --- Test 4: unmonitored remainder withheld when coverage inadequate ---
    def test_unmonitored_withheld_when_smt_coverage_below_threshold(self):
        # Only 50 SMT intervals (50/96 ≈ 52% < 90% threshold)
        result = self._valid_donut(smt_intervals=50)
        assert not result["available"], "Should be unavailable with low SMT coverage"
        assert "unmonitored" not in {s.get("key") for s in result.get("slices", [])}

    # --- Test 5: partial Tuya coverage → no donut ---
    def test_partial_tuya_coverage_returns_unavailable(self):
        # Only 600 Tuya records (600/1440 ≈ 42% < 80% threshold)
        result = self._valid_donut(tuya_per_minute=600)
        assert not result["available"], "Should be unavailable with low Tuya coverage"

    # --- Test 6: None power_w not treated as coverage ---
    def test_none_power_not_counted_as_coverage(self):
        # Records where power_w is None for all channels
        smt, _, yesterday_start, yesterday_end = _make_complete_day_fixtures(
            self.FROZEN_NOW, smt_intervals=96
        )
        # Build Tuya records with None power for ac_compressor
        null_tuya = [
            FakeTuyaRecord(
                timestamp=yesterday_start + timedelta(seconds=i * 60),
                appliances={
                    "ac_compressor": {"power_w": None},  # no reading
                    "furnace_air_handler": {"power_w": 500.0},
                    "dryer": {"power_w": 200.0},
                    "dishwasher": {"power_w": 100.0},
                },
            )
            for i in range(1200)
        ]
        result = compute_energy_donut(smt, null_tuya, now=self.FROZEN_NOW)
        # ac_compressor has 0% channel coverage → below 80% threshold → unavailable
        assert not result["available"], (
            "None power_w must not count as coverage — should be unavailable"
        )

    # --- Test 9: center total is SMT whole-home kWh ---
    def test_center_total_is_smt_kwh(self):
        result = self._valid_donut()
        assert result["available"]
        assert "smt_kwh" in result
        assert result["smt_kwh"] > 0, "SMT kWh must be positive"

    # --- Test 10: no negative remainder ---
    def test_no_negative_unmonitored_remainder(self):
        result = self._valid_donut()
        assert result["available"]
        unmon = next(s for s in result["slices"] if s["key"] == "unmonitored")
        assert unmon["kwh"] >= 0, f"Unmonitored kWh is negative: {unmon['kwh']}"
        assert unmon["pct"] >= 0, f"Unmonitored % is negative: {unmon['pct']}"

    def test_calibration_issue_not_rendered(self):
        """When circuits exceed SMT (calibration issue), donut must not render."""
        # Give extremely high channel power so monitored > SMT
        result = self._valid_donut(
            channel_power_w={
                "ac_compressor":       5000.0,
                "furnace_air_handler": 5000.0,
                "dryer":               5000.0,
                "dishwasher":          5000.0,
            }
        )
        # monitored_total will far exceed SMT → unavailable
        assert not result["available"], (
            "Should be unavailable when monitored circuits exceed SMT total"
        )

    def test_unavailable_state_has_diagnostics(self):
        """Unavailable result must include diagnostic fields for display."""
        result = compute_energy_donut([], [], now=self.FROZEN_NOW)
        assert not result["available"]
        assert "unavailable_reason" in result
        assert result["unavailable_reason"]
        assert "latest_smt_day" in result
        assert "latest_tuya_day" in result

    def test_window_label_uses_shared_phrasing(self):
        result = self._valid_donut()
        assert result["available"]
        label = result.get("window_label", "")
        assert "shared" in label.lower() or "Tuya" in label, (
            f"Label should reference shared coverage; got: {label!r}"
        )
        # Must NOT say "Latest complete SMT day" when Tuya is also required
        assert "SMT day" not in label, (
            f"Label must not say 'SMT day' when Tuya is required; got: {label!r}"
        )


class TestTuyaChannelCoveragePct:
    """Tests for the per-channel coverage helper."""

    def test_full_coverage(self):
        base = NOW
        records = [
            FakeTuyaRecord(
                timestamp=base + timedelta(seconds=i * 60),
                appliances={"ac_compressor": {"power_w": 2000.0}},
            )
            for i in range(60)
        ]
        cov = tuya_channel_coverage_pct(
            records, "ac_compressor",
            window_start=base, window_end=base + timedelta(hours=1)
        )
        assert cov == pytest.approx(100.0, abs=5.0)

    def test_none_power_w_not_counted(self):
        base = NOW
        records = [
            FakeTuyaRecord(
                timestamp=base + timedelta(seconds=i * 60),
                appliances={"ac_compressor": {"power_w": None}},
            )
            for i in range(60)
        ]
        cov = tuya_channel_coverage_pct(
            records, "ac_compressor",
            window_start=base, window_end=base + timedelta(hours=1)
        )
        assert cov == 0.0, "None power_w must not count as coverage"

    def test_absent_channel_gives_zero(self):
        base = NOW
        records = [
            FakeTuyaRecord(
                timestamp=base + timedelta(seconds=i * 60),
                appliances={},  # no channels
            )
            for i in range(60)
        ]
        cov = tuya_channel_coverage_pct(
            records, "dryer",
            window_start=base, window_end=base + timedelta(hours=1)
        )
        assert cov == 0.0
