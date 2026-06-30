"""
Kestrel energy analysis engine.

Pure functions for testability — accepts pre-loaded data, returns structured
dicts. No file I/O here; callers (e.g. app.py) handle data loading.

Conventions
-----------
* All timestamps stored/compared in UTC; display formatting happens elsewhere.
* kWh is the canonical energy unit.
* Average kW over an SMT interval: kwh × (60 / interval_minutes).
* "Tuya measured load" = sum of all 4 CT appliance channels.
  Tuya does NOT have a whole-home CT, so source-agreement comparison to SMT
  whole-home billing data is unavailable for exact meter matching.
* HVAC load = ac_compressor + furnace_air_handler channels.
* Compressor-only = ac_compressor channel.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SMT_INTERVAL_MINUTES: int = 15
KW_FROM_KWH_FACTOR: float = 60.0 / SMT_INTERVAL_MINUTES  # 4.0 for 15-min intervals

TUYA_ALL_KEYS: tuple[str, ...] = (
    "ac_compressor",
    "furnace_air_handler",
    "dryer",
    "dishwasher",
)
TUYA_HVAC_KEYS: tuple[str, ...] = ("ac_compressor", "furnace_air_handler")
TUYA_COMPRESSOR_KEY: str = "ac_compressor"
TUYA_NON_HVAC_KEYS: tuple[str, ...] = ("dryer", "dishwasher")

# User-facing labels for each circuit channel.
# These are the only labels that should appear in the UI — never "whole home".
TUYA_CHANNEL_LABELS: dict[str, str] = {
    "ac_compressor": "AC Compressor",
    "furnace_air_handler": "Furnace / Air Handler",
    "dryer": "Dryer",
    "dishwasher": "Dishwasher",
}
# Label for the sum of all monitored circuits (not whole-home).
TUYA_MONITORED_TOTAL_LABEL: str = "Monitored Circuits Total"

TUYA_EXPECTED_POLL_SECONDS: int = 60
NEST_EXPECTED_POLL_MINUTES: int = 5

# HVAC cycle grouping: samples closer than this are bridged into one cycle.
HVAC_CYCLE_GAP_TOLERANCE_MINUTES: int = 15
SHORT_CYCLE_MINUTES: int = 10   # cycles shorter than this are "short cycles"
LONG_CYCLE_MINUTES: int = 90    # cycles longer than this are "long cycles"

# Peak analysis: peaks within this window of each other are the same event.
PEAK_COOLDOWN_MINUTES: int = 30
MAX_PEAKS: int = 5

# Source agreement thresholds (absolute percentage difference).
AGREEMENT_GOOD_PCT: float = 5.0
AGREEMENT_ACCEPTABLE_PCT: float = 10.0

# Coverage thresholds.
COVERAGE_MINIMUM_PCT: float = 50.0  # below this → "insufficient data"

# Donut chart validity thresholds — both sources must meet these for a
# meaningful percentage breakdown to be shown.
DONUT_SMT_COVERAGE_THRESHOLD: float = 90.0   # % of calendar day covered by SMT
DONUT_TUYA_CHANNEL_THRESHOLD: float = 95.0   # % each Tuya channel must cover
DONUT_TUYA_EDGE_TOLERANCE_SECONDS: int = 15 * 60  # first/last sample within 15 min of window edge
DONUT_TUYA_MAX_GAP_SECONDS: int = 5 * 60          # no internal gap > 5 min per channel

# Donut slice colors — kept in sync with kestrel_energy_timeline.js
DONUT_CHANNEL_COLORS: dict[str, str] = {
    "ac_compressor":       "#da3633",
    "furnace_air_handler": "#f0883e",
    "dryer":               "#d29922",
    "dishwasher":          "#3fb950",
    "unmonitored":         "#8b949e",
}

NEST_ACTION_COOLING: str = "COOLING"
NEST_ACTION_HEATING: str = "HEATING"

KESTREL_TIMEZONE: str = "America/Chicago"


# ---------------------------------------------------------------------------
# SMT helpers
# ---------------------------------------------------------------------------

def smt_kwh_to_avg_kw(kwh: float, interval_minutes: int = SMT_INTERVAL_MINUTES) -> float:
    """Convert an SMT interval kWh value to average kW over that interval."""
    if interval_minutes <= 0:
        raise ValueError("interval_minutes must be positive")
    return round(kwh * (60.0 / interval_minutes), 4)


def smt_intervals_to_avg_kw_series(
    rows: list[dict[str, Any]],
    *,
    interval_minutes: int = SMT_INTERVAL_MINUTES,
) -> list[dict[str, Any]]:
    """Return SMT rows enriched with avg_kw field."""
    result = []
    for row in rows:
        kwh = float(row.get("kwh") or 0)
        result.append(
            {
                "start_ts": row["start_ts"],
                "end_ts": row["end_ts"],
                "kwh": kwh,
                "avg_kw": smt_kwh_to_avg_kw(kwh, interval_minutes),
            }
        )
    return result


def smt_coverage_pct(
    rows: list[dict[str, Any]],
    *,
    window_start: datetime,
    window_end: datetime,
    interval_minutes: int = SMT_INTERVAL_MINUTES,
) -> float:
    """
    Percentage of the window covered by SMT intervals.

    Expected intervals = window length / interval_minutes.
    """
    window_seconds = (window_end - window_start).total_seconds()
    if window_seconds <= 0:
        return 0.0
    expected = window_seconds / (interval_minutes * 60)
    if expected < 1:
        return 0.0
    actual = sum(
        1
        for row in rows
        if _parse_ts(str(row["start_ts"])) >= window_start
        and _parse_ts(str(row["start_ts"])) < window_end
    )
    return round(100.0 * actual / expected, 1)


def smt_total_kwh(
    rows: list[dict[str, Any]],
    *,
    window_start: datetime,
    window_end: datetime,
) -> float:
    """Sum SMT kWh for rows whose start_ts falls within [window_start, window_end)."""
    total = 0.0
    for row in rows:
        ts = _parse_ts(str(row["start_ts"]))
        if window_start <= ts < window_end:
            total += float(row.get("kwh") or 0)
    return round(total, 4)


# ---------------------------------------------------------------------------
# Tuya integration helpers
# ---------------------------------------------------------------------------

def integrate_tuya_energy(
    records: list[Any],  # TuyaPowerHistoryRecord
    appliance_keys: tuple[str, ...] | list[str],
    *,
    window_start: datetime,
    window_end: datetime,
) -> float:
    """
    Integrate Tuya appliance power over a time window (rectangular rule).

    Returns kWh. Uses the power reading at each sample point multiplied by the
    duration until the next sample (or end of window). Gaps > 5 minutes are
    not bridged to avoid fabricating energy during outages.
    """
    MAX_GAP_SECONDS = 300  # 5 minutes — don't bridge longer gaps

    def _has_valid_reading(record: Any) -> bool:
        return any(
            isinstance((entry := record.appliances.get(key)), dict)
            and entry.get("power_w") is not None
            for key in appliance_keys
        )

    in_window = sorted(
        (r for r in records if window_start <= r.timestamp < window_end and _has_valid_reading(r)),
        key=lambda r: r.timestamp,
    )
    if not in_window:
        return 0.0

    total_kwh = 0.0
    for i, record in enumerate(in_window):
        if i + 1 < len(in_window):
            next_ts = in_window[i + 1].timestamp
        else:
            next_ts = min(window_end, record.timestamp + timedelta(seconds=TUYA_EXPECTED_POLL_SECONDS * 2))

        gap_seconds = (next_ts - record.timestamp).total_seconds()
        if gap_seconds > MAX_GAP_SECONDS:
            continue

        power_w = sum(
            float(entry["power_w"])
            for key in appliance_keys
            if isinstance((entry := record.appliances.get(key)), dict)
            and entry.get("power_w") is not None
        )
        total_kwh += power_w * gap_seconds / 3_600_000  # W·s → kWh

    return round(total_kwh, 4)


def tuya_coverage_pct(
    records: list[Any],  # TuyaPowerHistoryRecord
    *,
    window_start: datetime,
    window_end: datetime,
    expected_poll_seconds: int = TUYA_EXPECTED_POLL_SECONDS,
) -> float:
    """Percentage of window covered by Tuya samples."""
    window_seconds = (window_end - window_start).total_seconds()
    if window_seconds <= 0:
        return 0.0
    expected = window_seconds / expected_poll_seconds
    if expected < 1:
        return 0.0
    actual = sum(1 for r in records if window_start <= r.timestamp < window_end)
    return round(min(100.0, 100.0 * actual / expected), 1)


def tuya_channel_coverage_pct(
    records: list[Any],  # TuyaPowerHistoryRecord
    appliance_key: str,
    *,
    window_start: datetime,
    window_end: datetime,
    expected_poll_seconds: int = TUYA_EXPECTED_POLL_SECONDS,
) -> float:
    """
    Coverage % for a single Tuya channel based on records that have a
    non-None power_w reading for that channel.

    A record where the channel entry is absent or power_w is None does NOT
    count as coverage — it means the device was polled but the circuit was
    not reporting (e.g. CT disconnected).  A genuine 0.0 W reading counts.
    """
    window_seconds = (window_end - window_start).total_seconds()
    if window_seconds <= 0:
        return 0.0
    expected = window_seconds / expected_poll_seconds
    if expected < 1:
        return 0.0
    actual = sum(
        1
        for r in records
        if window_start <= r.timestamp < window_end
        and isinstance(r.appliances.get(appliance_key), dict)
        and r.appliances[appliance_key].get("power_w") is not None
    )
    return round(min(100.0, 100.0 * actual / expected), 1)


def validate_tuya_donut_channels(
    records: list[Any],
    *,
    window_start: datetime,
    window_end: datetime,
    channel_keys: tuple[str, ...] = TUYA_ALL_KEYS,
    coverage_threshold: float = DONUT_TUYA_CHANNEL_THRESHOLD,
    edge_tolerance_seconds: int = DONUT_TUYA_EDGE_TOLERANCE_SECONDS,
    max_gap_seconds: int = DONUT_TUYA_MAX_GAP_SECONDS,
) -> list[str]:
    """
    Return a list of validation failure messages for donut eligibility.

    Empty list means all per-channel coverage, edge-alignment, and gap checks
    passed for the shared local-day window.
    """
    failures: list[str] = []

    for key in channel_keys:
        label = TUYA_CHANNEL_LABELS[key]
        cov = tuya_channel_coverage_pct(
            records, key, window_start=window_start, window_end=window_end
        )
        if cov < coverage_threshold:
            failures.append(
                f"{label}: {cov:.1f}% coverage (need ≥ {coverage_threshold:.0f}%)"
            )

        channel_samples = sorted(
            r.timestamp
            for r in records
            if window_start <= r.timestamp < window_end
            and isinstance(r.appliances.get(key), dict)
            and r.appliances[key].get("power_w") is not None
        )
        if not channel_samples:
            failures.append(f"{label}: no samples in window")
            continue

        first_offset = (channel_samples[0] - window_start).total_seconds()
        if first_offset > edge_tolerance_seconds:
            failures.append(
                f"{label}: first sample {first_offset / 60:.0f} min after window start "
                f"(need ≤ {edge_tolerance_seconds // 60} min)"
            )

        last_offset = (window_end - channel_samples[-1]).total_seconds()
        if last_offset > edge_tolerance_seconds:
            failures.append(
                f"{label}: last sample {last_offset / 60:.0f} min before window end "
                f"(need ≤ {edge_tolerance_seconds // 60} min)"
            )

        for i in range(len(channel_samples) - 1):
            gap = (channel_samples[i + 1] - channel_samples[i]).total_seconds()
            if gap > max_gap_seconds:
                failures.append(
                    f"{label}: internal gap of {gap / 60:.0f} min (max {max_gap_seconds // 60} min)"
                )
                break

    return failures


def build_tuya_kw_series(
    records: list[Any],  # TuyaPowerHistoryRecord
    appliance_keys: tuple[str, ...] | list[str],
    *,
    window_start: datetime,
    window_end: datetime,
) -> list[dict[str, Any]]:
    """Build a summed kW time series for a set of appliance channels.

    Only sums channels that are present with a non-None power_w reading.
    Records where no requested channel has data are still included with kw=0
    only when at least one channel entry exists as a dict.
    """
    in_window = sorted(
        (r for r in records if window_start <= r.timestamp <= window_end),
        key=lambda r: r.timestamp,
    )
    result = []
    for record in in_window:
        values = [
            float(entry["power_w"])
            for key in appliance_keys
            if isinstance((entry := record.appliances.get(key)), dict)
            and entry.get("power_w") is not None
        ]
        if not values:
            continue  # no channel data in this record — skip
        result.append(
            {
                "timestamp": record.timestamp.isoformat(),
                "kw": round(sum(values) / 1000.0, 4),
            }
        )
    return result


def build_tuya_channel_series(
    records: list[Any],  # TuyaPowerHistoryRecord
    appliance_key: str,
    *,
    window_start: datetime,
    window_end: datetime,
) -> list[dict[str, Any]]:
    """Build a kW time series for a single Tuya circuit channel.

    Only includes records where the channel has a valid (non-None) power_w
    reading.  Records where the channel is absent or its reading is None are
    omitted to avoid producing false zero values.  An actual 0.0 W reading IS
    included.
    """
    in_window = sorted(
        (r for r in records if window_start <= r.timestamp <= window_end),
        key=lambda r: r.timestamp,
    )
    result = []
    for record in in_window:
        entry = record.appliances.get(appliance_key)
        if not isinstance(entry, dict):
            continue  # channel absent from this record
        power_w = entry.get("power_w")
        if power_w is None:
            continue  # no reading for this channel
        result.append(
            {
                "timestamp": record.timestamp.isoformat(),
                "kw": round(float(power_w) / 1000.0, 4),
            }
        )
    return result


def build_all_channel_series(
    records: list[Any],  # TuyaPowerHistoryRecord
    *,
    window_start: datetime,
    window_end: datetime,
) -> dict[str, list[dict[str, Any]]]:
    """Build per-channel kW series for all four configured Tuya circuits.

    Returns a dict keyed by appliance key.  Channels with no data in the
    window are omitted from the dict rather than mapped to an empty list,
    so callers can distinguish 'no data' from 'all readings are zero'.
    """
    result: dict[str, list[dict[str, Any]]] = {}
    for key in TUYA_ALL_KEYS:
        series = build_tuya_channel_series(
            records, key, window_start=window_start, window_end=window_end
        )
        if series:
            result[key] = series
    return result


# ---------------------------------------------------------------------------
# Analysis window selection
# ---------------------------------------------------------------------------

@dataclass
class AnalysisWindow:
    label: str
    start: datetime
    end: datetime
    basis: str  # "today", "latest_24h", "latest_smt_day", "fallback"
    has_smt: bool = False
    has_tuya: bool = False
    has_nest: bool = False

    @property
    def duration_hours(self) -> float:
        return (self.end - self.start).total_seconds() / 3600.0


def _parse_ts(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _format_local_time(ts: datetime, tz_name: str) -> str:
    """Format a UTC datetime as local 12-hour time, e.g. '2:35 PM'."""
    local = ts.astimezone(ZoneInfo(tz_name))
    h = local.hour % 12 or 12
    m = local.minute
    period = "AM" if local.hour < 12 else "PM"
    return f"{h}:{m:02d} {period}"


def _format_local_datetime(ts: datetime, tz_name: str) -> str:
    """Format a UTC datetime as local date+time, e.g. 'Jun 30, 2:35 PM'."""
    local = ts.astimezone(ZoneInfo(tz_name))
    h = local.hour % 12 or 12
    m = local.minute
    period = "AM" if local.hour < 12 else "PM"
    return f"{local.strftime('%b %-d')}, {h}:{m:02d} {period}"


def _today_bounds(now: datetime, tz_name: str) -> tuple[datetime, datetime]:
    """Return (local midnight today UTC, now UTC)."""
    tz = ZoneInfo(tz_name)
    local_now = now.astimezone(tz)
    midnight_local = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight_local.astimezone(timezone.utc), now


def _smt_latest_day_bounds(
    rows: list[dict[str, Any]],
    tz_name: str,
) -> tuple[datetime, datetime] | None:
    """Return bounds for the most recent complete SMT day."""
    if not rows:
        return None

    tz = ZoneInfo(tz_name)
    days: dict[date, float] = {}
    for row in rows:
        ts = _parse_ts(str(row["start_ts"])).astimezone(tz)
        days.setdefault(ts.date(), 0.0)
        days[ts.date()] += float(row.get("kwh") or 0)

    if not days:
        return None

    # Find the most recent day that has a reasonable number of intervals (>= 48 = 12h)
    sorted_days = sorted(days.keys(), reverse=True)
    for day in sorted_days:
        day_rows = [
            r for r in rows
            if _parse_ts(str(r["start_ts"])).astimezone(tz).date() == day
        ]
        if len(day_rows) >= 48:
            midnight = datetime(day.year, day.month, day.day, 0, 0, tzinfo=tz)
            next_midnight = midnight + timedelta(days=1)
            return midnight.astimezone(timezone.utc), next_midnight.astimezone(timezone.utc)

    # Fallback: use the day with the most rows
    best_day = max(days.keys())
    day_dt = datetime(best_day.year, best_day.month, best_day.day, 0, 0, tzinfo=tz)
    return day_dt.astimezone(timezone.utc), (day_dt + timedelta(days=1)).astimezone(timezone.utc)


def _has_tuya_in_window(
    tuya_records: list[Any],
    *,
    start: datetime,
    end: datetime,
    min_samples: int = 5,
) -> bool:
    count = sum(1 for r in tuya_records if start <= r.timestamp < end)
    return count >= min_samples


def _has_nest_in_window(
    nest_records: list[Any],
    *,
    start: datetime,
    end: datetime,
    min_samples: int = 3,
) -> bool:
    count = sum(1 for r in nest_records if start <= r.timestamp < end)
    return count >= min_samples


def select_analysis_window(
    smt_rows: list[dict[str, Any]],
    tuya_records: list[Any],
    nest_records: list[Any],
    *,
    now: datetime | None = None,
    tz_name: str = KESTREL_TIMEZONE,
) -> AnalysisWindow:
    """
    Choose the best analysis window:

    1. Today so far, when all sources have useful overlap.
    2. Latest complete overlapping 24 hours.
    3. Latest complete SMT day.
    4. Fallback: whatever range SMT has.
    """
    ts_now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)

    # Window 1: today so far
    today_start, today_end = _today_bounds(ts_now, tz_name)
    today_smt = [r for r in smt_rows if today_start <= _parse_ts(str(r["start_ts"])) < today_end]
    today_has_smt = len(today_smt) >= 2
    today_has_tuya = _has_tuya_in_window(tuya_records, start=today_start, end=today_end)
    today_has_nest = _has_nest_in_window(nest_records, start=today_start, end=today_end)

    if today_has_smt and (today_has_tuya or today_has_nest):
        return AnalysisWindow(
            label="Today so far",
            start=today_start,
            end=today_end,
            basis="today",
            has_smt=today_has_smt,
            has_tuya=today_has_tuya,
            has_nest=today_has_nest,
        )

    # Window 2: latest 24 hours
    h24_start = ts_now - timedelta(hours=24)
    h24_smt = [r for r in smt_rows if h24_start <= _parse_ts(str(r["start_ts"])) < ts_now]
    h24_has_smt = len(h24_smt) >= 4
    h24_has_tuya = _has_tuya_in_window(tuya_records, start=h24_start, end=ts_now)
    h24_has_nest = _has_nest_in_window(nest_records, start=h24_start, end=ts_now)

    if h24_has_smt and (h24_has_tuya or h24_has_nest):
        return AnalysisWindow(
            label="Latest 24 hours",
            start=h24_start,
            end=ts_now,
            basis="latest_24h",
            has_smt=h24_has_smt,
            has_tuya=h24_has_tuya,
            has_nest=h24_has_nest,
        )

    # Window 3: latest complete SMT day
    if smt_rows:
        bounds = _smt_latest_day_bounds(smt_rows, tz_name)
        if bounds:
            d_start, d_end = bounds
            return AnalysisWindow(
                label="Latest complete SMT day",
                start=d_start,
                end=d_end,
                basis="latest_smt_day",
                has_smt=True,
                has_tuya=_has_tuya_in_window(tuya_records, start=d_start, end=d_end),
                has_nest=_has_nest_in_window(nest_records, start=d_start, end=d_end),
            )

    # Fallback: no data
    return AnalysisWindow(
        label="No data",
        start=ts_now - timedelta(hours=24),
        end=ts_now,
        basis="fallback",
        has_smt=False,
        has_tuya=False,
        has_nest=False,
    )


# ---------------------------------------------------------------------------
# Source agreement
# ---------------------------------------------------------------------------

"""
Tuya channel-to-circuit mapping (from kestrel/tuya_power.py CHANNEL_MAPPING):

  Meter 1, Channel 1  →  ac_compressor        (AC compressor CT clamp)
  Meter 1, Channel 2  →  furnace_air_handler   (Furnace / air handler CT clamp)
  Meter 2, Channel 1  →  dryer                 (Dryer CT clamp)
  Meter 2, Channel 2  →  dishwasher            (Dishwasher CT clamp)

There is NO whole-home or panel-main CT.  Each channel measures one specific
appliance circuit.  The sum of all four channels is a "monitored circuits"
total, not a whole-home total.

Because no whole-home Tuya channel exists, meter-to-meter comparison against
SMT (which bills the entire premises) is not calculable.  The source-agreement
function always returns available=False with an explanation.  Individual
channels are still used for HVAC attribution and peak labeling.
"""


def compute_source_agreement(
    smt_rows: list[dict[str, Any]],
    tuya_records: list[Any],
    *,
    window_start: datetime,
    window_end: datetime,
) -> dict[str, Any]:
    """
    Attempt SMT vs Tuya source agreement.

    Because the Tuya installation measures four individual appliance circuits
    (ac_compressor, furnace_air_handler, dryer, dishwasher) and does NOT
    include a whole-home or panel-main CT, meter-to-meter comparison with
    SMT whole-home billing data is not valid.

    This function always returns available=False and provides the raw circuit
    and SMT totals for diagnostic context only.  No SMT-vs-Tuya percentage is
    calculated or returned.
    """
    smt_kwh_val = smt_total_kwh(smt_rows, window_start=window_start, window_end=window_end)
    smt_cov = smt_coverage_pct(smt_rows, window_start=window_start, window_end=window_end)

    circuit_kwh = integrate_tuya_energy(
        tuya_records,
        TUYA_ALL_KEYS,
        window_start=window_start,
        window_end=window_end,
    )
    hvac_circuit_kwh = integrate_tuya_energy(
        tuya_records,
        TUYA_HVAC_KEYS,
        window_start=window_start,
        window_end=window_end,
    )
    tuya_cov = tuya_coverage_pct(tuya_records, window_start=window_start, window_end=window_end)

    return {
        "available": False,
        "classification": "no_whole_home_ct",
        "classification_label": "Not comparable — no whole-home CT",
        "note": (
            "SMT measures whole-home consumption (utility billing reference). "
            "Tuya monitors four individual circuits via CT clamps: "
            "AC compressor, furnace/air handler, dryer, dishwasher. "
            "No panel-main or whole-home CT is installed, so meter-to-meter "
            "comparison is not valid."
        ),
        # Diagnostic totals — shown for context, not as a comparison
        "smt_kwh": round(smt_kwh_val, 4) if smt_kwh_val > 0 else None,
        "circuit_kwh": round(circuit_kwh, 4) if circuit_kwh > 0 else None,
        "hvac_circuit_kwh": round(hvac_circuit_kwh, 4) if hvac_circuit_kwh > 0 else None,
        # These fields are None because the comparison is not calculable
        "tuya_kwh": None,
        "tuya_hvac_kwh": None,
        "diff_kwh": None,
        "diff_pct": None,
        "tuya_fraction_pct": None,
        "smt_coverage_pct": smt_cov,
        "tuya_coverage_pct": tuya_cov,
        "window_start": window_start.isoformat(),
        "window_end": window_end.isoformat(),
    }


# ---------------------------------------------------------------------------
# HVAC cycle detection
# ---------------------------------------------------------------------------

@dataclass
class HvacCycle:
    zone: str
    start: datetime
    end: datetime
    sample_count: int

    @property
    def duration_minutes(self) -> float:
        return (self.end - self.start).total_seconds() / 60.0


def detect_hvac_cycles(
    nest_records: list[Any],  # NestHistoryRecord
    *,
    zone: str,
    window_start: datetime,
    window_end: datetime,
    gap_tolerance_minutes: int = HVAC_CYCLE_GAP_TOLERANCE_MINUTES,
) -> list[HvacCycle]:
    """
    Detect HVAC cooling cycles in a time window for a given zone.

    Consecutive COOLING samples within gap_tolerance_minutes of each other
    are grouped into one cycle.
    """
    records_in_window = sorted(
        (r for r in nest_records if window_start <= r.timestamp < window_end),
        key=lambda r: r.timestamp,
    )
    if not records_in_window:
        return []

    gap = timedelta(minutes=gap_tolerance_minutes)
    cycles: list[HvacCycle] = []
    current_start: datetime | None = None
    current_end: datetime | None = None
    count = 0

    def _action(record: Any) -> str | None:
        entry = record.thermostats.get(zone)
        if not isinstance(entry, dict):
            return None
        action = entry.get("action")
        return str(action) if action else None

    def _close_cycle() -> None:
        nonlocal current_start, current_end, count
        if current_start is not None and current_end is not None:
            cycles.append(HvacCycle(
                zone=zone,
                start=current_start,
                end=current_end,
                sample_count=count,
            ))
        current_start = None
        current_end = None
        count = 0

    for record in records_in_window:
        action = _action(record)
        if action == NEST_ACTION_COOLING:
            if current_start is None:
                current_start = record.timestamp
                current_end = record.timestamp
                count = 1
            else:
                # Bridge gap if short enough
                if record.timestamp - current_end <= gap:
                    current_end = record.timestamp
                    count += 1
                else:
                    _close_cycle()
                    current_start = record.timestamp
                    current_end = record.timestamp
                    count = 1
        else:
            # Not cooling — close current cycle if gap exceeded
            if current_start is not None and current_end is not None:
                if record.timestamp - current_end > gap:
                    _close_cycle()

    _close_cycle()
    return cycles


def compute_hvac_cycle_stats(
    cycles: list[HvacCycle],
    tuya_records: list[Any],
    *,
    window_start: datetime,
    window_end: datetime,
    short_cycle_minutes: int = SHORT_CYCLE_MINUTES,
    long_cycle_minutes: int = LONG_CYCLE_MINUTES,
) -> dict[str, Any]:
    """Compute HVAC performance metrics from detected cycles and Tuya compressor data."""
    if not cycles:
        return {
            "available": False,
            "cycle_count": 0,
            "total_runtime_minutes": 0.0,
            "avg_cycle_minutes": None,
            "longest_cycle_minutes": None,
            "short_cycle_count": 0,
            "long_cycle_count": 0,
            "avg_compressor_kw": None,
            "hvac_energy_kwh": 0.0,
            "short_cycle_threshold_minutes": short_cycle_minutes,
            "long_cycle_threshold_minutes": long_cycle_minutes,
        }

    total_minutes = sum(c.duration_minutes for c in cycles)
    avg_minutes = total_minutes / len(cycles) if cycles else 0.0
    longest = max(c.duration_minutes for c in cycles)
    short = sum(1 for c in cycles if c.duration_minutes < short_cycle_minutes)
    long_count = sum(1 for c in cycles if c.duration_minutes > long_cycle_minutes)

    # Compute average compressor power during cooling cycles
    compressor_watts_samples: list[float] = []
    for cycle in cycles:
        in_cycle = [
            r for r in tuya_records
            if cycle.start <= r.timestamp <= cycle.end
        ]
        for record in in_cycle:
            entry = record.appliances.get(TUYA_COMPRESSOR_KEY)
            if isinstance(entry, dict):
                pw = entry.get("power_w")
                if isinstance(pw, (int, float)) and pw > 0:
                    compressor_watts_samples.append(float(pw))

    avg_compressor_kw: float | None = None
    if compressor_watts_samples:
        avg_compressor_kw = round(sum(compressor_watts_samples) / len(compressor_watts_samples) / 1000.0, 3)

    # Estimate HVAC energy from Tuya compressor during cycles
    hvac_kwh = integrate_tuya_energy(
        tuya_records,
        TUYA_HVAC_KEYS,
        window_start=window_start,
        window_end=window_end,
    )

    return {
        "available": True,
        "cycle_count": len(cycles),
        "total_runtime_minutes": round(total_minutes, 1),
        "total_runtime_hours": round(total_minutes / 60.0, 2),
        "avg_cycle_minutes": round(avg_minutes, 1),
        "longest_cycle_minutes": round(longest, 1),
        "short_cycle_count": short,
        "long_cycle_count": long_count,
        "avg_compressor_kw": avg_compressor_kw,
        "hvac_energy_kwh": hvac_kwh,
        "short_cycle_threshold_minutes": short_cycle_minutes,
        "long_cycle_threshold_minutes": long_cycle_minutes,
    }


# ---------------------------------------------------------------------------
# Peak analysis
# ---------------------------------------------------------------------------

@dataclass
class PeakEvent:
    timestamp: datetime
    total_kw: float  # Tuya measured total kW at peak
    compressor_kw: float
    hvac_kw: float  # compressor + air handler
    non_hvac_kw: float  # dryer + dishwasher
    nest_action: str | None  # dominant action across zones
    explanation: str
    source: str  # "tuya_instantaneous" or "smt_interval"


def _dominant_nest_action(
    nest_records: list[Any],
    *,
    at: datetime,
    tolerance_minutes: int = 10,
) -> str | None:
    """Find the most recent Nest action near a given timestamp."""
    tol = timedelta(minutes=tolerance_minutes)
    candidates = [
        r for r in nest_records
        if abs((r.timestamp - at).total_seconds()) < tol.total_seconds()
    ]
    if not candidates:
        return None
    nearest = min(candidates, key=lambda r: abs((r.timestamp - at).total_seconds()))
    actions = []
    for zone_data in nearest.thermostats.values():
        if isinstance(zone_data, dict):
            action = zone_data.get("action")
            if action:
                actions.append(str(action))
    if not actions:
        return None
    # COOLING > HEATING > anything else
    if NEST_ACTION_COOLING in actions:
        return NEST_ACTION_COOLING
    if NEST_ACTION_HEATING in actions:
        return NEST_ACTION_HEATING
    return actions[0]


def _smt_avg_kw_at(
    smt_rows: list[dict[str, Any]],
    ts: datetime,
) -> float | None:
    """Return the SMT average kW for the 15-min interval containing ``ts``.

    Returns None when no interval contains the timestamp.  Used to compute
    the unmonitored remainder (SMT avg kW minus sum of Tuya circuits).
    """
    for row in smt_rows:
        start = _parse_ts(str(row["start_ts"]))
        end = _parse_ts(str(row["end_ts"]))
        if start <= ts < end:
            return smt_kwh_to_avg_kw(float(row.get("kwh") or 0))
    return None


def _classify_peak(
    *,
    hvac_kw: float,
    non_hvac_kw: float,
    total_kw: float,
    nest_action: str | None,
    has_tuya: bool,
) -> str:
    if not has_tuya or total_kw <= 0:
        return "Unknown — no Tuya data"

    hvac_frac = hvac_kw / total_kw if total_kw > 0 else 0
    non_hvac_frac = non_hvac_kw / total_kw if total_kw > 0 else 0

    if nest_action == NEST_ACTION_COOLING and hvac_frac >= 0.6:
        if non_hvac_frac > 0.2:
            return "HVAC + other significant load"
        return "HVAC only"
    if hvac_frac >= 0.5:
        return "HVAC only"
    if non_hvac_frac >= 0.3:
        return "Non-HVAC load"
    if nest_action is None:
        return "Unknown — missing Nest data"
    return "Mixed load"


def find_demand_peaks(
    tuya_records: list[Any],
    smt_rows: list[dict[str, Any]],
    nest_records: list[Any],
    *,
    window_start: datetime,
    window_end: datetime,
    top_n: int = MAX_PEAKS,
    cooldown_minutes: int = PEAK_COOLDOWN_MINUTES,
    tz_name: str = KESTREL_TIMEZONE,
) -> list[dict[str, Any]]:
    """
    Find top demand peaks in the analysis window.

    Peaks are found from Tuya instantaneous power readings. Nearby samples
    within cooldown_minutes are grouped into one peak event (highest reading
    from each group is reported).
    """
    in_window = sorted(
        (r for r in tuya_records if window_start <= r.timestamp < window_end),
        key=lambda r: r.timestamp,
    )
    if not in_window:
        return _find_peaks_from_smt(
            smt_rows, nest_records,
            window_start=window_start, window_end=window_end,
            top_n=top_n, tz_name=tz_name,
        )

    # Build per-record data: track each circuit individually.
    # Only channels with a non-None power_w contribute; absent/None channels
    # are omitted (not coerced to zero).
    DataPoint = tuple[datetime, dict[str, float], float]  # ts, chan_kw, monitored_total
    data_points: list[DataPoint] = []
    for record in in_window:
        chan_kw: dict[str, float] = {}
        for key in TUYA_ALL_KEYS:
            entry = record.appliances.get(key)
            if isinstance(entry, dict):
                pw = entry.get("power_w")
                if pw is not None:
                    chan_kw[key] = round(float(pw) / 1000.0, 4)
        if not chan_kw:
            continue  # record has no circuit data — skip
        monitored_total = round(sum(chan_kw.values()), 4)
        data_points.append((record.timestamp, chan_kw, monitored_total))

    if not data_points:
        return []

    # Group consecutive samples into peak events using cooldown window.
    cooldown = timedelta(minutes=cooldown_minutes)
    groups: list[list[DataPoint]] = []
    current_group: list[DataPoint] = []
    last_ts: datetime | None = None

    for point in data_points:
        ts = point[0]
        if last_ts is None or (ts - last_ts) > cooldown:
            if current_group:
                groups.append(current_group)
            current_group = [point]
        else:
            current_group.append(point)
        last_ts = ts
    if current_group:
        groups.append(current_group)

    # Take the highest-total sample from each group as the peak representative.
    peak_events: list[DataPoint] = [
        max(group, key=lambda p: p[2]) for group in groups
    ]
    peak_events.sort(key=lambda p: p[2], reverse=True)
    peak_events = peak_events[:top_n]

    results = []
    for ts, chan_kw, monitored_total in peak_events:
        nest_action = _dominant_nest_action(nest_records, at=ts)
        hvac_kw = round(
            sum(chan_kw.get(k, 0.0) for k in TUYA_HVAC_KEYS), 2
        )
        explanation = _classify_peak(
            hvac_kw=hvac_kw,
            non_hvac_kw=0.0,          # not derived as a single figure
            total_kw=monitored_total,
            nest_action=nest_action,
            has_tuya=True,
        )
        # Unmonitored remainder: SMT whole-home avg kW minus monitored circuits.
        # Only computed when an SMT interval contains this timestamp.
        smt_kw = _smt_avg_kw_at(smt_rows, ts)
        unmonitored = (
            round(max(smt_kw - monitored_total, 0.0), 2)
            if smt_kw is not None else None
        )
        results.append({
            "timestamp": ts.isoformat(),
            "timestamp_display": _format_local_datetime(ts, tz_name),
            "time_display": _format_local_time(ts, tz_name),
            # Per-circuit readings at this sample
            "channels": chan_kw,
            # Derived aggregates
            "monitored_total_kw": round(monitored_total, 2),
            "hvac_kw": hvac_kw,
            "compressor_kw": chan_kw.get(TUYA_COMPRESSOR_KEY),
            # SMT-derived remainder (None when SMT not aligned)
            "smt_whole_home_kw": round(smt_kw, 2) if smt_kw is not None else None,
            "unmonitored_remainder_kw": unmonitored,
            # Legacy field kept for backward compat with classify/story code
            "total_kw": round(monitored_total, 2),
            "non_hvac_kw": None,  # replaced by individual channels
            "nest_action": nest_action,
            "explanation": explanation,
            "source": "tuya_instantaneous",
        })

    results.sort(key=lambda r: r["total_kw"], reverse=True)
    return results


def _find_peaks_from_smt(
    smt_rows: list[dict[str, Any]],
    nest_records: list[Any],
    *,
    window_start: datetime,
    window_end: datetime,
    top_n: int,
    tz_name: str = KESTREL_TIMEZONE,
) -> list[dict[str, Any]]:
    """Fallback: find peaks from SMT 15-min intervals when no Tuya data."""
    in_window = sorted(
        (r for r in smt_rows if window_start <= _parse_ts(str(r["start_ts"])) < window_end),
        key=lambda r: _parse_ts(str(r["start_ts"])),
    )
    if not in_window:
        return []

    peaks = sorted(in_window, key=lambda r: float(r.get("kwh") or 0), reverse=True)[:top_n]
    results = []
    for row in peaks:
        ts = _parse_ts(str(row["start_ts"]))
        avg_kw = smt_kwh_to_avg_kw(float(row.get("kwh") or 0))
        nest_action = _dominant_nest_action(nest_records, at=ts)
        explanation = _classify_peak(
            hvac_kw=0,
            non_hvac_kw=0,
            total_kw=avg_kw,
            nest_action=nest_action,
            has_tuya=False,
        )
        results.append({
            "timestamp": ts.isoformat(),
            "timestamp_display": _format_local_datetime(ts, tz_name),
            "time_display": _format_local_time(ts, tz_name),
            "total_kw": avg_kw,
            "monitored_total_kw": None,     # no Tuya data
            "channels": {},                 # no per-circuit readings
            "compressor_kw": None,
            "hvac_kw": None,
            "non_hvac_kw": None,
            "smt_whole_home_kw": avg_kw,    # SMT IS the whole-home reading here
            "unmonitored_remainder_kw": None,  # nothing to subtract
            "nest_action": nest_action,
            "explanation": explanation,
            "source": "smt_interval",
        })
    return results


# ---------------------------------------------------------------------------
# Energy breakdown
# ---------------------------------------------------------------------------

def compute_energy_breakdown(
    smt_rows: list[dict[str, Any]],
    tuya_records: list[Any],
    *,
    window_start: datetime,
    window_end: datetime,
) -> dict[str, Any]:
    """
    Break down energy consumption for the analysis window.

    Shows each Tuya circuit individually.  Components are reconciled against
    SMT as the utility billing reference.  The unmonitored remainder is
    SMT total minus the sum of all monitored circuits; it represents lighting,
    small appliances, EV charging, and anything else not CT-clamped.

    Tuya-derived values are estimates.  Do not label any Tuya total as
    'whole home' — no whole-home CT is installed.
    """
    smt_kwh_val = smt_total_kwh(smt_rows, window_start=window_start, window_end=window_end)

    # Per-circuit energy integration (each circuit independently)
    channel_kwh: dict[str, float] = {}
    for key in TUYA_ALL_KEYS:
        kwh = integrate_tuya_energy(
            tuya_records, (key,), window_start=window_start, window_end=window_end
        )
        if kwh > 0:
            channel_kwh[key] = round(kwh, 4)

    monitored_total_kwh = round(sum(channel_kwh.values()), 4)

    # Unmonitored remainder: only when both SMT and circuit data are present.
    unmonitored_kwh: float | None = None
    if smt_kwh_val > 0 and monitored_total_kwh > 0:
        unmonitored_kwh = round(max(smt_kwh_val - monitored_total_kwh, 0.0), 4)

    # HVAC composite (for cycle stats and story — not shown separately unless
    # individual channels are also shown)
    hvac_kwh = round(sum(channel_kwh.get(k, 0.0) for k in TUYA_HVAC_KEYS), 4)
    hvac_pct = round(100.0 * hvac_kwh / smt_kwh_val, 1) if smt_kwh_val > 0 and hvac_kwh > 0 else None

    return {
        "smt_kwh": smt_kwh_val,
        # Per-circuit estimates (key → kWh; absent keys had zero readings)
        "channel_kwh": channel_kwh,
        "monitored_total_kwh": monitored_total_kwh,
        "unmonitored_remainder_kwh": unmonitored_kwh,
        # Aggregates kept for backward compat with HVAC cycle stats / story
        "hvac_kwh": hvac_kwh,
        "compressor_kwh": channel_kwh.get(TUYA_COMPRESSOR_KEY, 0.0),
        "non_hvac_measured_kwh": round(
            sum(channel_kwh.get(k, 0.0) for k in TUYA_NON_HVAC_KEYS), 4
        ),
        # Legacy alias
        "tuya_total_kwh": monitored_total_kwh,
        "hvac_pct_of_smt": hvac_pct,
        "has_smt": smt_kwh_val > 0,
        "has_tuya": monitored_total_kwh > 0,
    }


# ---------------------------------------------------------------------------
# Daily trends (7-day view)
# ---------------------------------------------------------------------------

def compute_daily_trends(
    smt_rows: list[dict[str, Any]],
    tuya_records: list[Any],
    nest_records: list[Any],
    *,
    days: int = 7,
    now: datetime | None = None,
    tz_name: str = KESTREL_TIMEZONE,
) -> list[dict[str, Any]]:
    """
    Compute per-day energy metrics for the past N days.

    Days with inadequate coverage are marked missing rather than shown as zero.
    """
    ts_now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    tz = ZoneInfo(tz_name)
    today_local = ts_now.astimezone(tz).date()

    results = []
    for days_ago in range(days - 1, -1, -1):
        local_day = today_local - timedelta(days=days_ago)
        day_start = datetime(local_day.year, local_day.month, local_day.day, 0, 0, tzinfo=tz)
        day_end = day_start + timedelta(days=1)
        day_start_utc = day_start.astimezone(timezone.utc)
        day_end_utc = day_end.astimezone(timezone.utc)

        day_smt = [
            r for r in smt_rows
            if day_start_utc <= _parse_ts(str(r["start_ts"])) < day_end_utc
        ]
        smt_kwh = sum(float(r.get("kwh") or 0) for r in day_smt)
        smt_cov = smt_coverage_pct(
            day_smt, window_start=day_start_utc, window_end=day_end_utc
        )

        tuya_kwh = integrate_tuya_energy(
            tuya_records, TUYA_ALL_KEYS, window_start=day_start_utc, window_end=day_end_utc
        )
        hvac_kwh = integrate_tuya_energy(
            tuya_records, TUYA_HVAC_KEYS, window_start=day_start_utc, window_end=day_end_utc
        )
        tuya_cov = tuya_coverage_pct(
            tuya_records, window_start=day_start_utc, window_end=day_end_utc
        )

        # Cooling runtime for this day (across all zones)
        day_nest = [r for r in nest_records if day_start_utc <= r.timestamp < day_end_utc]
        cooling_minutes = 0.0
        for record in day_nest:
            for zone_data in record.thermostats.values():
                if isinstance(zone_data, dict) and zone_data.get("action") == NEST_ACTION_COOLING:
                    cooling_minutes += NEST_EXPECTED_POLL_MINUTES
                    break  # count household level, not per-zone

        # Peak kW for the day (from SMT or Tuya)
        peak_kw: float | None = None
        if day_smt:
            peak_kw = max(smt_kwh_to_avg_kw(float(r.get("kwh") or 0)) for r in day_smt)

        # Coverage flag
        is_today = local_day == today_local
        adequate_smt = smt_cov >= COVERAGE_MINIMUM_PCT or (is_today and len(day_smt) >= 1)

        results.append({
            "date": local_day.isoformat(),
            "date_label": local_day.strftime("%a %b %-d"),
            "is_today": is_today,
            "smt_kwh": round(smt_kwh, 2) if adequate_smt else None,
            "tuya_kwh": round(tuya_kwh, 2) if tuya_cov >= COVERAGE_MINIMUM_PCT else None,
            "hvac_kwh": round(hvac_kwh, 2) if tuya_cov >= COVERAGE_MINIMUM_PCT else None,
            "cooling_minutes": round(cooling_minutes, 0) if day_nest else None,
            "peak_kw": round(peak_kw, 2) if peak_kw is not None else None,
            "smt_coverage_pct": smt_cov,
            "tuya_coverage_pct": tuya_cov,
            "adequate_coverage": adequate_smt,
        })

    return results


# ---------------------------------------------------------------------------
# Energy story (plain-language findings)
# ---------------------------------------------------------------------------

def generate_energy_story(
    window: AnalysisWindow,
    breakdown: dict[str, Any],
    peaks: list[dict[str, Any]],
    hvac_stats: dict[str, Any],
    agreement: dict[str, Any],
    *,
    primary_zone: str = "downstairs",
    tz_name: str = KESTREL_TIMEZONE,
) -> list[str]:
    """
    Generate 3–5 plain-language findings from available data.

    Only reports findings supported by actual data; does not fabricate
    appliance identification.
    """
    findings: list[str] = []

    # Peak demand finding
    if peaks:
        top_peak = peaks[0]
        time_str = top_peak.get("time_display") or ""
        kw = top_peak["total_kw"]
        if kw > 0 and time_str:
            source_note = "" if top_peak.get("source") != "smt_interval" else " (SMT 15-min avg)"
            findings.append(f"Peak demand was {kw:.1f} kW at {time_str}{source_note}.")

    # HVAC % of total energy (primary story finding — more meaningful than peak fraction)
    hvac_pct_total = breakdown.get("hvac_pct_of_smt")
    if hvac_pct_total is not None and breakdown.get("has_smt") and breakdown.get("has_tuya"):
        findings.append(
            f"HVAC used approximately {hvac_pct_total:.0f}% of total household energy."
        )

    # Cooling runtime
    if hvac_stats.get("available") and hvac_stats.get("total_runtime_minutes", 0) > 0:
        total_min = hvac_stats["total_runtime_minutes"]
        hours = int(total_min // 60)
        mins = int(total_min % 60)
        if hours > 0 and mins > 0:
            findings.append(
                f"Cooling ran for {hours} hr {mins} min "
                f"across {hvac_stats['cycle_count']} cycle{'s' if hvac_stats['cycle_count'] != 1 else ''}."
            )
        elif hours > 0:
            findings.append(
                f"Cooling ran for {hours} hour{'s' if hours != 1 else ''} "
                f"({hvac_stats['cycle_count']} cycle{'s' if hvac_stats['cycle_count'] != 1 else ''})."
            )
        else:
            findings.append(f"Cooling ran for {mins} minute{'s' if mins != 1 else ''}.")

    # No SMT/Tuya fraction finding: Tuya monitors circuits, not whole-home,
    # so any SMT-vs-Tuya percentage would be misleading.

    # Unmonitored remainder at peak (when SMT and Tuya are both aligned)
    if peaks:
        top = peaks[0]
        unmon = top.get("unmonitored_remainder_kw")
        time_str = top.get("time_display") or ""
        if unmon is not None and unmon > 0.5 and time_str:
            findings.append(
                f"Estimated unmonitored load at peak: {unmon:.1f} kW "
                f"(SMT whole-home minus measured circuits)."
            )

    return findings[:5]


# ---------------------------------------------------------------------------
# Combined timeline data for JS chart
# ---------------------------------------------------------------------------

def build_combined_timeline(
    smt_rows: list[dict[str, Any]],
    tuya_records: list[Any],
    nest_records: list[Any],
    *,
    window_start: datetime,
    window_end: datetime,
    tz_name: str = KESTREL_TIMEZONE,
) -> dict[str, Any]:
    """
    Build all data series for the combined energy + HVAC timeline chart.

    Returns a dict that is serialised to JSON and consumed by
    kestrel_energy_timeline.js.
    """
    # SMT bars
    smt_bars = []
    for row in smt_rows:
        ts = _parse_ts(str(row["start_ts"]))
        if window_start <= ts < window_end:
            kwh = float(row.get("kwh") or 0)
            smt_bars.append({
                "start_ts": ts.isoformat(),
                "end_ts": row["end_ts"],
                "kwh": kwh,
                "avg_kw": smt_kwh_to_avg_kw(kwh),
            })
    smt_bars.sort(key=lambda r: r["start_ts"])

    # Per-circuit series (each channel individually — do not aggregate before
    # passing to the frontend so users can see each circuit separately)
    channels = build_all_channel_series(
        tuya_records, window_start=window_start, window_end=window_end
    )

    # Monitored circuits total (sum of all present channels per timestamp)
    tuya_monitored_total = build_tuya_kw_series(
        tuya_records, TUYA_ALL_KEYS, window_start=window_start, window_end=window_end
    )

    # HVAC composite series (compressor + air handler together — kept for
    # the chart's HVAC highlight line)
    tuya_hvac = build_tuya_kw_series(
        tuya_records, TUYA_HVAC_KEYS, window_start=window_start, window_end=window_end
    )

    # Legacy aliases kept for backward compatibility
    tuya_compressor = channels.get(TUYA_COMPRESSOR_KEY, [])
    tuya_measured = tuya_monitored_total  # was the sum; now correctly labeled

    # Nest cooling bands (per-zone and household-level)
    tz = ZoneInfo(tz_name)
    in_window_nest = sorted(
        (r for r in nest_records if window_start <= r.timestamp < window_end),
        key=lambda r: r.timestamp,
    )

    nest_samples = []
    for record in in_window_nest:
        sample: dict[str, Any] = {"timestamp": record.timestamp.isoformat()}
        for zone, data in record.thermostats.items():
            if isinstance(data, dict):
                sample[f"{zone}_action"] = data.get("action")
                sample[f"{zone}_temp_f"] = data.get("temperature")
                sample[f"{zone}_setpoint_f"] = data.get("setpoint")
        nest_samples.append(sample)

    # Build cooling periods as bands for shading
    cooling_bands: list[dict[str, Any]] = []
    _band_start: datetime | None = None
    _band_end: datetime | None = None
    gap = timedelta(minutes=HVAC_CYCLE_GAP_TOLERANCE_MINUTES)

    for record in in_window_nest:
        any_cooling = any(
            isinstance(d, dict) and d.get("action") == NEST_ACTION_COOLING
            for d in record.thermostats.values()
        )
        if any_cooling:
            if _band_start is None:
                _band_start = record.timestamp
                _band_end = record.timestamp
            elif record.timestamp - _band_end <= gap:
                _band_end = record.timestamp
            else:
                cooling_bands.append({
                    "start": _band_start.isoformat(),
                    "end": _band_end.isoformat(),
                })
                _band_start = record.timestamp
                _band_end = record.timestamp
        elif _band_start is not None and record.timestamp - _band_end > gap:
            cooling_bands.append({
                "start": _band_start.isoformat(),
                "end": _band_end.isoformat(),
            })
            _band_start = None
            _band_end = None

    if _band_start is not None and _band_end is not None:
        cooling_bands.append({
            "start": _band_start.isoformat(),
            "end": _band_end.isoformat(),
        })

    return {
        "window_start": window_start.isoformat(),
        "window_end": window_end.isoformat(),
        "smt_bars": smt_bars,
        # Individual circuit series — these drive the four separate lines
        "channels": channels,
        "channel_labels": {k: TUYA_CHANNEL_LABELS[k] for k in channels},
        # Derived series
        "tuya_monitored_total": tuya_monitored_total,
        "tuya_hvac": tuya_hvac,
        # Legacy aliases (kept for backward compat with existing JS + tests)
        "tuya_measured": tuya_measured,
        "tuya_compressor": tuya_compressor,
        "nest_samples": nest_samples,
        "cooling_bands": cooling_bands,
        "has_smt": bool(smt_bars),
        "has_tuya": bool(channels) or bool(tuya_monitored_total),
        "has_nest": bool(nest_samples),
        # Metadata for the frontend legend
        "monitored_total_label": TUYA_MONITORED_TOTAL_LABEL,
    }


# ---------------------------------------------------------------------------
# Energy donut breakdown
# ---------------------------------------------------------------------------

def compute_energy_donut(
    smt_rows: list[dict[str, Any]],
    tuya_records: list[Any],
    *,
    now: datetime | None = None,
    tz_name: str = KESTREL_TIMEZONE,
    smt_threshold: float = DONUT_SMT_COVERAGE_THRESHOLD,
    tuya_channel_threshold: float = DONUT_TUYA_CHANNEL_THRESHOLD,
) -> dict[str, Any]:
    """
    Find the most recent complete calendar day where SMT AND all four Tuya
    channels each have adequate coverage, then compute percentage slices.

    Validity requirements (all must pass):
    - Day must be fully elapsed in local time (yesterday or earlier).
    - SMT and Tuya share the exact same local-day window.
    - SMT coverage ≥ smt_threshold % of the calendar day.
    - Every configured Tuya channel must have ≥ tuya_channel_threshold %
      coverage based on non-None power_w readings (not just record presence).
    - First Tuya sample for each channel within 15 min of window start.
    - Last Tuya sample for each channel within 15 min of window end.
    - No internal Tuya gap > 5 min for any required channel.
    - The monitored-circuit total must not exceed the SMT total (which would
      indicate a calibration issue and produce a negative remainder).

    When no valid day is found within 14 days the function returns
    available=False with diagnostics for the unavailable state display.
    """
    ts_now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    tz = ZoneInfo(tz_name)
    today_local = ts_now.astimezone(tz).date()

    # Track best individual days for diagnostics shown in unavailable state.
    best_smt_day: str | None = None
    best_smt_cov: float = 0.0
    best_tuya_day: str | None = None
    best_tuya_min_cov: float = 0.0  # worst-channel coverage across best tuya day
    latest_validation_failures: list[str] = []

    for days_ago in range(1, 15):  # 1 = yesterday; today never complete during use
        local_day = today_local - timedelta(days=days_ago)
        day_start_local = datetime(
            local_day.year, local_day.month, local_day.day, 0, 0, tzinfo=tz
        )
        day_end_local = day_start_local + timedelta(days=1)
        day_start_utc = day_start_local.astimezone(timezone.utc)
        day_end_utc = day_end_local.astimezone(timezone.utc)

        # --- SMT coverage for this day ---
        day_smt = [
            r for r in smt_rows
            if day_start_utc <= _parse_ts(str(r["start_ts"])) < day_end_utc
        ]
        smt_cov = smt_coverage_pct(
            day_smt, window_start=day_start_utc, window_end=day_end_utc
        )
        if smt_cov > best_smt_cov:
            best_smt_cov = smt_cov
            best_smt_day = local_day.isoformat()

        # --- Per-channel Tuya coverage ---
        day_tuya = [
            r for r in tuya_records
            if day_start_utc <= r.timestamp < day_end_utc
        ]
        channel_covs: dict[str, float] = {
            key: tuya_channel_coverage_pct(
                day_tuya, key, window_start=day_start_utc, window_end=day_end_utc
            )
            for key in TUYA_ALL_KEYS
        }
        min_channel_cov = min(channel_covs.values()) if channel_covs else 0.0
        if min_channel_cov > best_tuya_min_cov:
            best_tuya_min_cov = min_channel_cov
            best_tuya_day = local_day.isoformat()

        # --- Collect all validity failures for this day ---
        failures: list[str] = []
        if smt_cov < smt_threshold:
            failures.append(
                f"SMT: {smt_cov:.1f}% coverage (need ≥ {smt_threshold:.0f}%)"
            )
        failures.extend(
            validate_tuya_donut_channels(
                day_tuya,
                window_start=day_start_utc,
                window_end=day_end_utc,
                coverage_threshold=tuya_channel_threshold,
            )
        )

        if days_ago == 1:
            latest_validation_failures = failures

        if failures:
            continue

        smt_kwh_val = smt_total_kwh(
            day_smt, window_start=day_start_utc, window_end=day_end_utc
        )
        if smt_kwh_val <= 0:
            continue

        # --- Per-channel energy integration (only after all validity checks pass) ---
        channel_kwh: dict[str, float] = {}
        for key in TUYA_ALL_KEYS:
            kwh = integrate_tuya_energy(
                day_tuya, (key,), window_start=day_start_utc, window_end=day_end_utc
            )
            channel_kwh[key] = round(kwh, 3)

        monitored_total = round(sum(channel_kwh.values()), 3)

        # Guard: if monitored circuits exceed SMT by more than 5% it signals
        # a calibration issue — do not show a negative or near-zero remainder.
        if monitored_total > smt_kwh_val * 1.05:
            continue

        unmonitored = round(max(smt_kwh_val - monitored_total, 0.0), 3)

        # --- Build slices (all 4 channels always present; 0 kWh is valid) ---
        slices: list[dict[str, Any]] = []
        for key in TUYA_ALL_KEYS:
            kwh = channel_kwh[key]
            pct = round(100.0 * kwh / smt_kwh_val, 1) if smt_kwh_val > 0 else 0.0
            slices.append({
                "key": key,
                "label": TUYA_CHANNEL_LABELS[key],
                "kwh": kwh,
                "pct": pct,
                "color": DONUT_CHANNEL_COLORS[key],
            })
        unmon_pct = round(100.0 * unmonitored / smt_kwh_val, 1) if smt_kwh_val > 0 else 0.0
        slices.append({
            "key": "unmonitored",
            "label": "Estimated Unmonitored",
            "kwh": unmonitored,
            "pct": unmon_pct,
            "color": DONUT_CHANNEL_COLORS["unmonitored"],
        })

        return {
            "available": True,
            "window_label": "Latest complete shared SMT + Tuya day",
            "window_start": day_start_utc.isoformat(),
            "window_end": day_end_utc.isoformat(),
            "window_date": local_day.isoformat(),
            "smt_kwh": round(smt_kwh_val, 2),
            "slices": slices,
            "monitored_total_kwh": round(monitored_total, 2),
            "smt_coverage_pct": smt_cov,
            "channel_coverages": {k: round(v, 1) for k, v in channel_covs.items()},
        }

    # No valid shared day found within 14-day look-back.
    return {
        "available": False,
        "unavailable_reason": (
            "Breakdown unavailable — insufficient complete overlap between SMT and Tuya"
        ),
        "validation_failures": latest_validation_failures,
        "latest_smt_day": best_smt_day,
        "latest_smt_coverage_pct": round(best_smt_cov, 1),
        "latest_tuya_day": best_tuya_day,
        "latest_tuya_channel_coverage_pct": round(best_tuya_min_cov, 1),
    }


# ---------------------------------------------------------------------------
# Data quality and freshness
# ---------------------------------------------------------------------------

def compute_data_quality(
    smt_rows: list[dict[str, Any]],
    tuya_records: list[Any],
    nest_records: list[Any],
    *,
    window_start: datetime,
    window_end: datetime,
    now: datetime | None = None,
    smt_latest_ts: str | None = None,
    tuya_latest_ts: datetime | None = None,
    nest_latest_ts: datetime | None = None,
) -> dict[str, Any]:
    """Aggregate data freshness and coverage diagnostics."""
    ts_now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)

    def _age_minutes(ts: datetime | None) -> int | None:
        if ts is None:
            return None
        return max(0, int((ts_now - ts).total_seconds() // 60))

    smt_cov = smt_coverage_pct(smt_rows, window_start=window_start, window_end=window_end)
    tuya_cov = tuya_coverage_pct(tuya_records, window_start=window_start, window_end=window_end)

    smt_age: int | None = None
    if smt_latest_ts:
        try:
            smt_age = _age_minutes(_parse_ts(smt_latest_ts))
        except Exception:
            pass

    tuya_age = _age_minutes(tuya_latest_ts)
    nest_age = _age_minutes(nest_latest_ts)

    return {
        "smt_coverage_pct": smt_cov,
        "tuya_coverage_pct": tuya_cov,
        "smt_row_count": len([r for r in smt_rows if window_start <= _parse_ts(str(r["start_ts"])) < window_end]),
        "tuya_row_count": sum(1 for r in tuya_records if window_start <= r.timestamp < window_end),
        "nest_row_count": sum(1 for r in nest_records if window_start <= r.timestamp < window_end),
        "smt_age_minutes": smt_age,
        "tuya_age_minutes": tuya_age,
        "nest_age_minutes": nest_age,
        "smt_fresh": smt_age is None or smt_age < 24 * 60,  # SMT updates daily
        "tuya_fresh": tuya_age is None or tuya_age < 5,
        "nest_fresh": nest_age is None or nest_age < 15,
    }


# ---------------------------------------------------------------------------
# Main aggregation entry point
# ---------------------------------------------------------------------------

def compute_kestrel_analysis(
    smt_rows: list[dict[str, Any]],
    tuya_records: list[Any],
    nest_records: list[Any],
    *,
    now: datetime | None = None,
    tz_name: str = KESTREL_TIMEZONE,
    smt_latest_ts: str | None = None,
    tuya_latest_ts: datetime | None = None,
    nest_latest_ts: datetime | None = None,
) -> dict[str, Any]:
    """
    Run all analysis and return a single dict for the /kestrel template.

    This is the main entry point called from app.py. All sub-computations
    are pure and individually testable.
    """
    ts_now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)

    window = select_analysis_window(
        smt_rows, tuya_records, nest_records, now=ts_now, tz_name=tz_name
    )

    # Detect HVAC cycles (downstairs as primary zone)
    primary_zone = "downstairs"
    all_zones = set()
    for r in nest_records:
        all_zones.update(r.thermostats.keys())
    if primary_zone not in all_zones and all_zones:
        primary_zone = sorted(all_zones)[0]

    cycles = detect_hvac_cycles(
        nest_records,
        zone=primary_zone,
        window_start=window.start,
        window_end=window.end,
    )
    hvac_stats = compute_hvac_cycle_stats(
        cycles, tuya_records, window_start=window.start, window_end=window.end
    )

    agreement = compute_source_agreement(
        smt_rows, tuya_records, window_start=window.start, window_end=window.end
    )

    breakdown = compute_energy_breakdown(
        smt_rows, tuya_records, window_start=window.start, window_end=window.end
    )

    peaks = find_demand_peaks(
        tuya_records, smt_rows, nest_records,
        window_start=window.start, window_end=window.end,
    )

    timeline = build_combined_timeline(
        smt_rows, tuya_records, nest_records,
        window_start=window.start, window_end=window.end,
        tz_name=tz_name,
    )

    trends = compute_daily_trends(
        smt_rows, tuya_records, nest_records,
        now=ts_now, tz_name=tz_name,
    )

    quality = compute_data_quality(
        smt_rows, tuya_records, nest_records,
        window_start=window.start, window_end=window.end,
        now=ts_now,
        smt_latest_ts=smt_latest_ts,
        tuya_latest_ts=tuya_latest_ts,
        nest_latest_ts=nest_latest_ts,
    )

    story = generate_energy_story(
        window, breakdown, peaks, hvac_stats, agreement,
        primary_zone=primary_zone,
        tz_name=tz_name,
    )

    donut = compute_energy_donut(
        smt_rows, tuya_records, now=ts_now, tz_name=tz_name
    )

    return {
        "window": {
            "label": window.label,
            "start": window.start.isoformat(),
            "end": window.end.isoformat(),
            "basis": window.basis,
            "has_smt": window.has_smt,
            "has_tuya": window.has_tuya,
            "has_nest": window.has_nest,
        },
        "story": story,
        "hvac_stats": hvac_stats,
        "agreement": agreement,
        "breakdown": breakdown,
        "donut": donut,
        "peaks": peaks,
        "timeline": timeline,
        "trends": trends,
        "quality": quality,
        "primary_zone": primary_zone,
    }
