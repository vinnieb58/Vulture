"""Correlate Smart Meter Texas interval usage with Nest HVAC history."""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from kestrel_metrics import KESTREL_TIMEZONE, fetch_interval_rows, _parse_iso
from nest_history import NestHistoryRecord, read_history
from nest_hvac_runtime import HVAC_ACTION_COOLING, NEST_HISTORY_PATH, _thermostat_action

DEFAULT_CORRELATION_HOURS = 24
HIGH_KWH_THRESHOLD = float(os.environ.get("NEST_HVAC_HIGH_KWH_THRESHOLD", "1.0"))
PREFERRED_ZONES = ("downstairs", "upstairs")


def _format_interval_label(start: datetime, end: datetime, *, tz_name: str) -> str:
    tz = ZoneInfo(tz_name)
    start_local = start.astimezone(tz)
    end_local = end.astimezone(tz)

    def _time(dt: datetime) -> str:
        hour = dt.hour % 12 or 12
        minute = f":{dt.minute:02d}" if dt.minute else ""
        period = "AM" if dt.hour < 12 else "PM"
        return f"{hour}{minute} {period}"

    return f"{_time(start_local)}–{_time(end_local)}"


def _latest_sample_in_interval(
    records: list[NestHistoryRecord],
    *,
    start: datetime,
    end: datetime,
) -> NestHistoryRecord | None:
    candidates = [
        record
        for record in records
        if start <= record.timestamp < end
    ]
    if candidates:
        return max(candidates, key=lambda record: record.timestamp)

    prior = [record for record in records if record.timestamp < end]
    if not prior:
        return None
    nearest = max(prior, key=lambda record: record.timestamp)
    if end - nearest.timestamp > timedelta(minutes=10):
        return None
    return nearest


def _zone_action(record: NestHistoryRecord | None, zone: str) -> str | None:
    if record is None:
        return None
    return _thermostat_action(record, zone)


def _correlation_note(*, kwh: float, any_cooling: bool) -> str | None:
    if any_cooling and kwh >= HIGH_KWH_THRESHOLD:
        return f"High usage ({kwh:.2f} kWh) during cooling"
    return None


def correlate_energy_intervals(
    energy_rows: list[dict[str, Any]],
    nest_records: list[NestHistoryRecord],
    *,
    zones: tuple[str, ...] = PREFERRED_ZONES,
    tz_name: str = KESTREL_TIMEZONE,
) -> list[dict[str, Any]]:
    """Join 15-minute energy intervals with Nest HVAC samples."""
    rows: list[dict[str, Any]] = []
    for energy in energy_rows:
        start = _parse_iso(str(energy["start_ts"]))
        end = _parse_iso(str(energy["end_ts"]))
        kwh = float(energy["kwh"])
        sample = _latest_sample_in_interval(nest_records, start=start, end=end)

        zone_actions: dict[str, str | None] = {
            zone: _zone_action(sample, zone) for zone in zones
        }
        cooling_flags = [
            action == HVAC_ACTION_COOLING
            for action in zone_actions.values()
            if action is not None
        ]
        any_cooling = bool(cooling_flags) and any(cooling_flags)

        rows.append(
            {
                "interval_label": _format_interval_label(start, end, tz_name=tz_name),
                "start_ts": start.isoformat(),
                "end_ts": end.isoformat(),
                "kwh": round(kwh, 4),
                "kwh_display": f"{kwh:.2f}",
                "zone_actions": zone_actions,
                "any_cooling": any_cooling,
                "cooling_display": "yes" if any_cooling else "no",
                "note": _correlation_note(kwh=kwh, any_cooling=any_cooling),
                "nest_sample_at": sample.timestamp.isoformat() if sample else None,
            }
        )
    return rows


def get_energy_hvac_correlation(
    *,
    history_path: Path | None = None,
    hours: int = DEFAULT_CORRELATION_HOURS,
    now: datetime | None = None,
    tz_name: str = KESTREL_TIMEZONE,
) -> dict[str, Any]:
    """Build Energy + HVAC correlation rows for the dashboard."""
    ts_now = now or datetime.now(timezone.utc)
    start_ts = (ts_now - timedelta(hours=hours)).isoformat()

    energy_rows = fetch_interval_rows(start_ts=start_ts, end_ts=ts_now.isoformat())
    nest_records = read_history(history_path or NEST_HISTORY_PATH)

    if not energy_rows:
        return {
            "available": False,
            "warning": "No Smart Meter Texas interval data for correlation window",
            "rows": [],
            "hours": hours,
            "high_kwh_threshold": HIGH_KWH_THRESHOLD,
        }

    if not nest_records:
        return {
            "available": False,
            "warning": "No Nest HVAC history for correlation",
            "rows": [],
            "hours": hours,
            "high_kwh_threshold": HIGH_KWH_THRESHOLD,
        }

    rows = correlate_energy_intervals(energy_rows, nest_records, tz_name=tz_name)
    return {
        "available": True,
        "warning": None,
        "rows": rows,
        "hours": hours,
        "high_kwh_threshold": HIGH_KWH_THRESHOLD,
        "estimate_note": (
            "HVAC actions reflect the latest Nest poll within each 15-minute "
            f"interval ({hours}h window)."
        ),
    }
