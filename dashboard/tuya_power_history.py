"""Tuya appliance power history reader and chart series builder for the dashboard."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

TUYA_HISTORY_PATH = Path(
    os.environ.get("TUYA_HISTORY_PATH", "/app/data/kestrel_tuya_power_history.jsonl")
)

_APPLIANCE_ORDER = (
    "ac_compressor",
    "furnace_air_handler",
    "dryer",
    "dishwasher",
)

_APPLIANCE_LABELS = {
    "ac_compressor": "AC compressor",
    "furnace_air_handler": "Furnace / air handler",
    "dryer": "Dryer",
    "dishwasher": "Dishwasher",
}


@dataclass(frozen=True)
class TuyaPowerHistoryRecord:
    timestamp: datetime
    source: str | None
    limited: bool
    appliances: dict[str, dict[str, Any]]


def _parse_iso(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def parse_history_lines(text: str) -> list[TuyaPowerHistoryRecord]:
    records: list[TuyaPowerHistoryRecord] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(data, dict):
            continue
        record = history_record_from_dict(data)
        if record is not None:
            records.append(record)
    return records


def history_record_from_dict(data: dict[str, Any]) -> TuyaPowerHistoryRecord | None:
    ts_raw = data.get("timestamp")
    if not isinstance(ts_raw, str):
        return None
    parsed_ts = _parse_iso(ts_raw)
    if parsed_ts is None:
        return None

    appliances_raw = data.get("appliances")
    if not isinstance(appliances_raw, dict):
        appliances_raw = {}

    appliances: dict[str, dict[str, Any]] = {}
    for appliance_key, entry in appliances_raw.items():
        if isinstance(entry, dict):
            appliances[str(appliance_key)] = dict(entry)

    source = data.get("source")
    limited = bool(data.get("limited", False))
    return TuyaPowerHistoryRecord(
        timestamp=parsed_ts,
        source=str(source) if source else None,
        limited=limited,
        appliances=appliances,
    )


def read_tuya_power_history(path: Path | str | None = None) -> list[TuyaPowerHistoryRecord]:
    """Load Tuya power history records. Never raises."""
    history_path = Path(path or TUYA_HISTORY_PATH)
    if not history_path.is_file():
        return []
    try:
        return parse_history_lines(history_path.read_text(encoding="utf-8"))
    except OSError:
        return []


def filter_history_records(
    records: list[TuyaPowerHistoryRecord],
    *,
    hours: float,
    now: datetime | None = None,
) -> list[TuyaPowerHistoryRecord]:
    ts_now = now or datetime.now(timezone.utc)
    cutoff = ts_now - timedelta(hours=hours)
    return [record for record in records if record.timestamp >= cutoff]


def build_appliance_power_series(
    records: list[TuyaPowerHistoryRecord],
    *,
    hours: float,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Build multi-series chart payloads (watts over time) for one time window."""
    window_records = filter_history_records(records, hours=hours, now=now)
    window_records = sorted(window_records, key=lambda record: record.timestamp)
    if not window_records:
        return []

    series: list[dict[str, Any]] = []
    for appliance_key in _APPLIANCE_ORDER:
        points: list[dict[str, Any]] = []
        for record in window_records:
            entry = record.appliances.get(appliance_key)
            if not isinstance(entry, dict):
                continue
            power_w = entry.get("power_w")
            if not isinstance(power_w, (int, float)):
                continue
            points.append(
                {
                    "timestamp": record.timestamp.isoformat(),
                    "watts": float(power_w),
                }
            )
        if points:
            series.append(
                {
                    "key": appliance_key,
                    "label": _APPLIANCE_LABELS[appliance_key],
                    "points": points,
                }
            )
    return series
