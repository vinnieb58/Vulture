"""Pure functions to summarize Kestrel interval data."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from kestrel.models import EnergyInterval

INTERVAL_MINUTES = 15
KW_ESTIMATE_FACTOR = 60 / INTERVAL_MINUTES  # 4.0 for 15-minute intervals


@dataclass(frozen=True)
class PeakInterval:
    start_ts: str
    end_ts: str
    kwh: float
    estimated_peak_kw: float


@dataclass(frozen=True)
class IntervalSummary:
    interval_count: int
    range_start: str | None
    range_end: str | None
    total_kwh: float
    peak_interval: PeakInterval | None
    estimated_peak_kw: float | None
    daily_totals: dict[str, float]
    hourly_average_kwh: dict[int, float]
    missing_interval_count: int
    anomalies: list[PeakInterval]


def estimated_kw_from_interval_kwh(kwh: float, interval_minutes: int = INTERVAL_MINUTES) -> float:
    """Estimate average kW over a fixed interval (not instantaneous demand)."""
    if interval_minutes <= 0:
        return 0.0
    return kwh * (60.0 / interval_minutes)


def total_kwh(intervals: list[EnergyInterval]) -> float:
    return round(sum(row.kwh for row in intervals), 4)


def peak_interval(intervals: list[EnergyInterval]) -> PeakInterval | None:
    if not intervals:
        return None
    top = max(intervals, key=lambda row: row.kwh)
    return PeakInterval(
        start_ts=top.start_ts,
        end_ts=top.end_ts,
        kwh=round(top.kwh, 4),
        estimated_peak_kw=round(estimated_kw_from_interval_kwh(top.kwh), 4),
    )


def top_intervals(intervals: list[EnergyInterval], n: int = 5) -> list[PeakInterval]:
    ranked = sorted(intervals, key=lambda row: row.kwh, reverse=True)[: max(n, 0)]
    return [
        PeakInterval(
            start_ts=row.start_ts,
            end_ts=row.end_ts,
            kwh=round(row.kwh, 4),
            estimated_peak_kw=round(estimated_kw_from_interval_kwh(row.kwh), 4),
        )
        for row in ranked
    ]


def daily_totals(intervals: list[EnergyInterval], tz_name: str = "America/Chicago") -> dict[str, float]:
    tz = ZoneInfo(tz_name)
    totals: dict[str, float] = defaultdict(float)
    for row in intervals:
        day = _parse_iso(row.start_ts).astimezone(tz).date().isoformat()
        totals[day] += row.kwh
    return {day: round(value, 4) for day, value in sorted(totals.items())}


def hourly_shape(intervals: list[EnergyInterval], tz_name: str = "America/Chicago") -> dict[int, float]:
    """Average kWh per clock hour (0-23) across all days in the sample."""
    tz = ZoneInfo(tz_name)
    buckets: dict[int, list[float]] = defaultdict(list)
    for row in intervals:
        hour = _parse_iso(row.start_ts).astimezone(tz).hour
        buckets[hour].append(row.kwh)
    return {
        hour: round(sum(values) / len(values), 4)
        for hour, values in sorted(buckets.items())
    }


def missing_interval_count(
    intervals: list[EnergyInterval],
    *,
    range_start: datetime | None = None,
    range_end: datetime | None = None,
    interval_minutes: int = INTERVAL_MINUTES,
) -> int:
    if not intervals:
        return 0

    starts = sorted(_parse_iso(row.start_ts) for row in intervals)
    if range_start is None:
        range_start = starts[0]
    if range_end is None:
        range_end = _parse_iso(intervals[-1].end_ts)

    expected = _expected_starts(range_start, range_end, interval_minutes)
    actual = {_parse_iso(row.start_ts) for row in intervals}
    return len(expected - actual)


def find_anomalies(
    intervals: list[EnergyInterval],
    *,
    threshold_kwh: float | None = None,
    top_n: int | None = 5,
) -> list[PeakInterval]:
    """Intervals above threshold and/or in the top N by kWh."""
    results: list[PeakInterval] = []
    seen: set[tuple[str, str]] = set()

    if threshold_kwh is not None:
        for row in intervals:
            if row.kwh >= threshold_kwh:
                key = (row.start_ts, row.end_ts)
                if key not in seen:
                    seen.add(key)
                    results.append(
                        PeakInterval(
                            start_ts=row.start_ts,
                            end_ts=row.end_ts,
                            kwh=round(row.kwh, 4),
                            estimated_peak_kw=round(estimated_kw_from_interval_kwh(row.kwh), 4),
                        )
                    )

    if top_n:
        for peak in top_intervals(intervals, top_n):
            key = (peak.start_ts, peak.end_ts)
            if key not in seen:
                seen.add(key)
                results.append(peak)

    return sorted(results, key=lambda item: item.kwh, reverse=True)


def summarize_intervals(
    intervals: list[EnergyInterval],
    *,
    tz_name: str = "America/Chicago",
    range_start: datetime | None = None,
    range_end: datetime | None = None,
    anomaly_threshold_kwh: float | None = None,
    anomaly_top_n: int = 5,
) -> IntervalSummary:
    peak = peak_interval(intervals)
    return IntervalSummary(
        interval_count=len(intervals),
        range_start=intervals[0].start_ts if intervals else None,
        range_end=intervals[-1].end_ts if intervals else None,
        total_kwh=total_kwh(intervals),
        peak_interval=peak,
        estimated_peak_kw=peak.estimated_peak_kw if peak else None,
        daily_totals=daily_totals(intervals, tz_name),
        hourly_average_kwh=hourly_shape(intervals, tz_name),
        missing_interval_count=missing_interval_count(
            intervals,
            range_start=range_start,
            range_end=range_end,
        ),
        anomalies=find_anomalies(
            intervals,
            threshold_kwh=anomaly_threshold_kwh,
            top_n=anomaly_top_n,
        ),
    )


def _parse_iso(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def _expected_starts(
    range_start: datetime,
    range_end: datetime,
    interval_minutes: int,
) -> set[datetime]:
    step = timedelta(minutes=interval_minutes)
    cursor = range_start.astimezone(timezone.utc).replace(second=0, microsecond=0)
    end = range_end.astimezone(timezone.utc)
    expected: set[datetime] = set()
    while cursor < end:
        expected.add(cursor)
        cursor += step
    return expected
