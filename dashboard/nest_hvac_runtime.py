"""HVAC runtime summaries from Nest thermostat polling history."""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from nest_history import (
    POLL_INTERVAL_MINUTES,
    NestHistoryRecord,
    prune_history_records,
    read_history,
)

NEST_HISTORY_PATH = Path(
    os.environ.get("NEST_HISTORY_PATH", "/app/data/kestrel_nest_history.jsonl")
)
NEST_TIMEZONE = os.environ.get("KESTREL_TIMEZONE", "America/Chicago")
STALE_AFTER_MINUTES = 15

WINDOW_LAST_24H = "last_24h"
WINDOW_TODAY = "today"
WINDOW_YESTERDAY = "yesterday"

HVAC_ACTION_COOLING = "COOLING"
HVAC_ACTION_HEATING = "HEATING"


@dataclass(frozen=True)
class ThermostatRuntime:
    zone: str
    cooling_minutes: float
    heating_minutes: float
    idle_minutes: float
    percent_time_cooling: float | None
    first_seen: datetime | None
    last_seen: datetime | None
    sample_count: int


@dataclass(frozen=True)
class HouseholdRuntime:
    any_cooling_minutes: float
    both_cooling_minutes: float
    percent_time_any_cooling: float | None
    sample_count: int


@dataclass(frozen=True)
class HvacRuntimeSummary:
    window: str
    window_label: str
    thermostats: list[ThermostatRuntime]
    household: HouseholdRuntime
    sample_count: int
    first_seen: datetime | None
    last_seen: datetime | None
    estimate_note: str


def _window_bounds(
    window: str,
    *,
    now: datetime,
    tz_name: str = NEST_TIMEZONE,
) -> tuple[datetime, datetime] | None:
    tz = ZoneInfo(tz_name)
    local_now = now.astimezone(tz)
    if window == WINDOW_LAST_24H:
        start = now - timedelta(hours=24)
        return start, now

    if window == WINDOW_TODAY:
        start_local = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
        return start_local.astimezone(timezone.utc), now

    if window == WINDOW_YESTERDAY:
        today_start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
        yesterday_start = today_start - timedelta(days=1)
        return yesterday_start.astimezone(timezone.utc), today_start.astimezone(timezone.utc)

    return None


def _window_label(window: str) -> str:
    labels = {
        WINDOW_LAST_24H: "Last 24 Hours",
        WINDOW_TODAY: "Today",
        WINDOW_YESTERDAY: "Yesterday",
    }
    return labels.get(window, window)


def _thermostat_action(record: NestHistoryRecord, zone: str) -> str | None:
    entry = record.thermostats.get(zone)
    if not isinstance(entry, dict):
        return None
    action = entry.get("action")
    return str(action) if action is not None else None


def _records_in_window(
    records: list[NestHistoryRecord],
    *,
    start: datetime,
    end: datetime,
) -> list[NestHistoryRecord]:
    return [record for record in records if start <= record.timestamp < end]


def _zone_names(records: list[NestHistoryRecord]) -> list[str]:
    zones: set[str] = set()
    for record in records:
        zones.update(record.thermostats.keys())
    preferred = ("downstairs", "upstairs")
    ordered = [zone for zone in preferred if zone in zones]
    ordered.extend(sorted(zone for zone in zones if zone not in preferred))
    return ordered


def compute_thermostat_runtime(
    records: list[NestHistoryRecord],
    zone: str,
    *,
    sample_minutes: float = POLL_INTERVAL_MINUTES,
) -> ThermostatRuntime:
    cooling = 0.0
    heating = 0.0
    idle = 0.0
    first_seen: datetime | None = None
    last_seen: datetime | None = None
    sample_count = 0

    for record in records:
        action = _thermostat_action(record, zone)
        if action is None:
            continue
        sample_count += 1
        first_seen = record.timestamp if first_seen is None else min(first_seen, record.timestamp)
        last_seen = record.timestamp if last_seen is None else max(last_seen, record.timestamp)

        if action == HVAC_ACTION_COOLING:
            cooling += sample_minutes
        elif action == HVAC_ACTION_HEATING:
            heating += sample_minutes
        else:
            idle += sample_minutes

    active_minutes = cooling + heating + idle
    percent_cooling = (100.0 * cooling / active_minutes) if active_minutes else None

    return ThermostatRuntime(
        zone=zone,
        cooling_minutes=cooling,
        heating_minutes=heating,
        idle_minutes=idle,
        percent_time_cooling=percent_cooling,
        first_seen=first_seen,
        last_seen=last_seen,
        sample_count=sample_count,
    )


def compute_household_runtime(
    records: list[NestHistoryRecord],
    zones: list[str],
    *,
    sample_minutes: float = POLL_INTERVAL_MINUTES,
) -> HouseholdRuntime:
    any_cooling = 0.0
    both_cooling = 0.0
    sample_count = 0

    for record in records:
        actions = [_thermostat_action(record, zone) for zone in zones]
        if all(action is None for action in actions):
            continue
        sample_count += 1
        cooling_flags = [action == HVAC_ACTION_COOLING for action in actions if action is not None]
        if any(cooling_flags):
            any_cooling += sample_minutes
        if cooling_flags and all(cooling_flags):
            both_cooling += sample_minutes

    percent_any = (100.0 * any_cooling / (sample_count * sample_minutes)) if sample_count else None

    return HouseholdRuntime(
        any_cooling_minutes=any_cooling,
        both_cooling_minutes=both_cooling,
        percent_time_any_cooling=percent_any,
        sample_count=sample_count,
    )


def compute_runtime_summary(
    records: list[NestHistoryRecord],
    *,
    window: str,
    now: datetime | None = None,
    tz_name: str = NEST_TIMEZONE,
) -> HvacRuntimeSummary | None:
    ts_now = now or datetime.now(timezone.utc)
    bounds = _window_bounds(window, now=ts_now, tz_name=tz_name)
    if bounds is None:
        return None
    start, end = bounds
    window_records = _records_in_window(records, start=start, end=end)
    zones = _zone_names(window_records)

    thermostats = [compute_thermostat_runtime(window_records, zone) for zone in zones]
    household = compute_household_runtime(window_records, zones)

    first_seen: datetime | None = None
    last_seen: datetime | None = None
    for record in window_records:
        first_seen = record.timestamp if first_seen is None else min(first_seen, record.timestamp)
        last_seen = record.timestamp if last_seen is None else max(last_seen, record.timestamp)

    return HvacRuntimeSummary(
        window=window,
        window_label=_window_label(window),
        thermostats=thermostats,
        household=household,
        sample_count=len(window_records),
        first_seen=first_seen,
        last_seen=last_seen,
        estimate_note=(
            f"Estimated from {POLL_INTERVAL_MINUTES}-minute Nest polling samples; "
            "not utility-grade runtime."
        ),
    )


def get_hvac_runtime_summaries(
    *,
    path: Path | None = None,
    now: datetime | None = None,
    windows: tuple[str, ...] = (WINDOW_LAST_24H, WINDOW_TODAY, WINDOW_YESTERDAY),
) -> dict[str, Any]:
    """Read Nest history and compute runtime summaries for dashboard display."""
    ts_now = now or datetime.now(timezone.utc)
    history_path = path or NEST_HISTORY_PATH

    if not history_path.is_file():
        return {
            "state": "no_data",
            "warning": "Nest HVAC history not found",
            "summaries": [],
            "latest_sample_at": None,
            "age_minutes": None,
        }

    records = prune_history_records(read_history(history_path), now=ts_now)
    if not records:
        return {
            "state": "no_data",
            "warning": "No Nest HVAC history samples yet",
            "summaries": [],
            "latest_sample_at": None,
            "age_minutes": None,
        }

    latest = max(records, key=lambda record: record.timestamp)
    age_minutes = max(0, int((ts_now - latest.timestamp).total_seconds() // 60))
    state = "available"
    warning: str | None = None
    if age_minutes > STALE_AFTER_MINUTES:
        state = "stale"
        warning = f"Nest HVAC history stale ({age_minutes} min since last sample)"

    summaries = []
    for window in windows:
        summary = compute_runtime_summary(records, window=window, now=ts_now)
        if summary is not None:
            summaries.append(summary)

    return {
        "state": state,
        "warning": warning,
        "summaries": summaries,
        "latest_sample_at": latest.timestamp.isoformat(),
        "age_minutes": age_minutes,
    }
