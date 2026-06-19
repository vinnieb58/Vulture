"""Read-only Kestrel energy metrics from the SQLite interval database."""

from __future__ import annotations

import os
import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

INTERVAL_MINUTES = 15
KW_ESTIMATE_FACTOR = 60.0 / INTERVAL_MINUTES
MAX_FULL_RANGE_CHART_POINTS = 120

KESTREL_DB_PATH = Path(
    os.environ.get("KESTREL_DB_PATH", "/app/data/kestrel/kestrel.db")
)
KESTREL_TIMEZONE = os.environ.get("KESTREL_TIMEZONE", "America/Chicago")


@dataclass(frozen=True)
class IntervalPeak:
    start_ts: str
    end_ts: str
    kwh: float
    estimated_peak_kw: float


@dataclass(frozen=True)
class AverageDailyUsage:
    kwh: float | None
    day_count: int
    requested_days: int


def _parse_iso(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def _local_day(iso_ts: str, tz_name: str = KESTREL_TIMEZONE) -> str:
    return _parse_iso(iso_ts).astimezone(ZoneInfo(tz_name)).date().isoformat()


def _local_hour(iso_ts: str, tz_name: str = KESTREL_TIMEZONE) -> int:
    return _parse_iso(iso_ts).astimezone(ZoneInfo(tz_name)).hour


def _cutoff_iso(days: int, *, tz_name: str = KESTREL_TIMEZONE) -> str:
    tz = ZoneInfo(tz_name)
    start_local = (
        datetime.now(tz).date() - timedelta(days=days - 1)
    )
    start_dt = datetime(
        start_local.year,
        start_local.month,
        start_local.day,
        0,
        0,
        tzinfo=tz,
    )
    return start_dt.astimezone(timezone.utc).isoformat()


def _estimated_kw(kwh: float) -> float:
    return round(kwh * KW_ESTIMATE_FACTOR, 4)


def _db_exists() -> bool:
    return KESTREL_DB_PATH.is_file()


def _connect() -> sqlite3.Connection | None:
    if not _db_exists():
        return None
    try:
        conn = sqlite3.connect(KESTREL_DB_PATH)
        conn.row_factory = sqlite3.Row
        return conn
    except sqlite3.Error:
        return None


def _fetch_interval_rows(
    *,
    start_ts: str | None = None,
    end_ts: str | None = None,
) -> list[dict[str, Any]]:
    conn = _connect()
    if conn is None:
        return []

    clauses: list[str] = []
    params: list[object] = []
    if start_ts:
        clauses.append("start_ts >= ?")
        params.append(start_ts)
    if end_ts:
        clauses.append("start_ts < ?")
        params.append(end_ts)

    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    query = f"""
        SELECT start_ts, end_ts, kwh
        FROM energy_intervals
        {where}
        ORDER BY start_ts ASC
    """

    try:
        rows = conn.execute(query, params).fetchall()
    except sqlite3.Error:
        return []
    finally:
        conn.close()

    return [
        {
            "start_ts": str(row["start_ts"]),
            "end_ts": str(row["end_ts"]),
            "kwh": float(row["kwh"]),
        }
        for row in rows
    ]


def get_range_bounds() -> tuple[str | None, str | None]:
    conn = _connect()
    if conn is None:
        return None, None
    try:
        row = conn.execute(
            """
            SELECT MIN(start_ts) AS range_start, MAX(end_ts) AS range_end
            FROM energy_intervals
            """
        ).fetchone()
    except sqlite3.Error:
        return None, None
    finally:
        conn.close()

    if row is None or row["range_start"] is None:
        return None, None
    return str(row["range_start"]), str(row["range_end"])


def get_daily_totals(*, days: int | None = None) -> dict[str, float]:
    start_ts = _cutoff_iso(days) if days is not None else None
    rows = _fetch_interval_rows(start_ts=start_ts)
    totals: dict[str, float] = defaultdict(float)
    for row in rows:
        totals[_local_day(row["start_ts"])] += row["kwh"]
    return {day: round(value, 4) for day, value in sorted(totals.items())}


def get_average_daily_usage(days: int) -> AverageDailyUsage:
    totals = get_daily_totals(days=days)
    if not totals:
        return AverageDailyUsage(kwh=None, day_count=0, requested_days=days)
    values = list(totals.values())
    return AverageDailyUsage(
        kwh=round(sum(values) / len(values), 4),
        day_count=len(values),
        requested_days=days,
    )


def get_peak_interval(days: int) -> IntervalPeak | None:
    start_ts = _cutoff_iso(days)
    conn = _connect()
    if conn is None:
        return None

    try:
        row = conn.execute(
            """
            SELECT start_ts, end_ts, kwh
            FROM energy_intervals
            WHERE start_ts >= ?
            ORDER BY kwh DESC, start_ts ASC
            LIMIT 1
            """,
            (start_ts,),
        ).fetchone()
    except sqlite3.Error:
        return None
    finally:
        conn.close()

    if row is None:
        return None
    kwh = float(row["kwh"])
    return IntervalPeak(
        start_ts=str(row["start_ts"]),
        end_ts=str(row["end_ts"]),
        kwh=round(kwh, 4),
        estimated_peak_kw=_estimated_kw(kwh),
    )


def get_hourly_average(days: int) -> dict[int, float]:
    start_ts = _cutoff_iso(days)
    rows = _fetch_interval_rows(start_ts=start_ts)
    buckets: dict[int, list[float]] = defaultdict(list)
    for row in rows:
        buckets[_local_hour(row["start_ts"])].append(row["kwh"])
    return {
        hour: round(sum(values) / len(values), 4)
        for hour, values in sorted(buckets.items())
    }


def get_top_intervals(days: int, limit: int = 10) -> list[IntervalPeak]:
    start_ts = _cutoff_iso(days)
    conn = _connect()
    if conn is None:
        return []

    try:
        rows = conn.execute(
            """
            SELECT start_ts, end_ts, kwh
            FROM energy_intervals
            WHERE start_ts >= ?
            ORDER BY kwh DESC, start_ts ASC
            LIMIT ?
            """,
            (start_ts, max(limit, 0)),
        ).fetchall()
    except sqlite3.Error:
        return []
    finally:
        conn.close()

    peaks: list[IntervalPeak] = []
    for row in rows:
        kwh = float(row["kwh"])
        peaks.append(
            IntervalPeak(
                start_ts=str(row["start_ts"]),
                end_ts=str(row["end_ts"]),
                kwh=round(kwh, 4),
                estimated_peak_kw=_estimated_kw(kwh),
            )
        )
    return peaks


def get_monthly_totals() -> dict[str, float]:
    rows = _fetch_interval_rows()
    totals: dict[str, float] = defaultdict(float)
    tz = ZoneInfo(KESTREL_TIMEZONE)
    for row in rows:
        local = _parse_iso(row["start_ts"]).astimezone(tz)
        month_key = f"{local.year:04d}-{local.month:02d}"
        totals[month_key] += row["kwh"]
    return {month: round(value, 4) for month, value in sorted(totals.items())}


def _downsample_daily_totals(totals: dict[str, float]) -> dict[str, float]:
    if len(totals) <= MAX_FULL_RANGE_CHART_POINTS:
        return totals

    weekly: dict[str, float] = defaultdict(float)
    for day, kwh in totals.items():
        parsed = date.fromisoformat(day)
        week_start = parsed - timedelta(days=parsed.weekday())
        weekly[week_start.isoformat()] += kwh
    return {week: round(value, 4) for week, value in sorted(weekly.items())}


def get_chart_daily_series(*, days: int | None = None, downsample: bool = False) -> list[dict[str, Any]]:
    totals = get_daily_totals(days=days)
    if downsample:
        totals = _downsample_daily_totals(totals)
    return [{"day": day, "kwh": kwh} for day, kwh in totals.items()]


def fetch_interval_rows(
    *,
    start_ts: str | None = None,
    end_ts: str | None = None,
) -> list[dict[str, Any]]:
    """Public wrapper for interval row queries used by correlation helpers."""
    return _fetch_interval_rows(start_ts=start_ts, end_ts=end_ts)


def energy_db_exists() -> bool:
    """Return True when the Kestrel SQLite database file is present."""
    return _db_exists()


def get_interval_count() -> int | None:
    """Return total energy interval row count, or None when the DB is unavailable."""
    conn = _connect()
    if conn is None:
        return None
    try:
        row = conn.execute("SELECT COUNT(*) AS count FROM energy_intervals").fetchone()
    except sqlite3.Error:
        return None
    finally:
        conn.close()
    if row is None:
        return None
    return int(row["count"])


def get_home_metrics() -> dict[str, Any]:
    """Metrics subset for the Nest home Kestrel card."""
    avg_7 = get_average_daily_usage(7)
    avg_30 = get_average_daily_usage(30)
    peak_7 = get_peak_interval(7)
    daily = get_daily_totals(days=30)
    recent_days = list(daily.items())[-3:]

    return {
        "available": _db_exists(),
        "avg_daily_7": avg_7,
        "avg_daily_30": avg_30,
        "peak_interval_7": peak_7,
        "recent_daily_totals": [
            {"day": day, "kwh": kwh} for day, kwh in recent_days
        ],
    }


def get_detail_metrics() -> dict[str, Any]:
    """Aggregated metrics for the /kestrel detail page."""
    range_start, range_end = get_range_bounds()
    avg_7 = get_average_daily_usage(7)
    avg_30 = get_average_daily_usage(30)
    peak_7 = get_peak_interval(7)
    monthly = get_monthly_totals()

    return {
        "available": _db_exists(),
        "range_start": range_start,
        "range_end": range_end,
        "total_kwh": round(sum(get_daily_totals().values()), 4) if _db_exists() else None,
        "avg_daily_7": avg_7,
        "avg_daily_30": avg_30,
        "peak_interval_7": peak_7,
        "daily_30": get_chart_daily_series(days=30),
        "daily_full": get_chart_daily_series(downsample=True),
        "hourly_30": [
            {"hour": hour, "kwh": kwh}
            for hour, kwh in sorted(get_hourly_average(30).items())
        ],
        "top_intervals_30": get_top_intervals(30, limit=10),
        "monthly_totals": [{"month": month, "kwh": kwh} for month, kwh in monthly.items()],
        "show_monthly": len(monthly) >= 2,
    }
