"""Append-only Tuya appliance power polling history (JSONL)."""

from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

_history_io_lock = threading.Lock()

POLL_INTERVAL_MINUTES = 5
RETENTION_DAYS = 14

DEFAULT_HISTORY_PATH = "data/kestrel_tuya_power_history.jsonl"

_HISTORY_APPLIANCE_FIELDS = (
    "voltage_v",
    "power_w",
    "current_a",
    "energy_forward_kwh",
    "energy_forward_kwh_inferred",
    "online",
    "source",
)


def default_history_path() -> str:
    return (os.getenv("TUYA_HISTORY_PATH") or DEFAULT_HISTORY_PATH).strip()


def _parse_iso(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _compact_appliance(entry: dict[str, Any]) -> dict[str, Any]:
    compact: dict[str, Any] = {}
    for field in _HISTORY_APPLIANCE_FIELDS:
        if field in entry:
            compact[field] = entry[field]
    return compact


def build_history_record(
    snapshot: dict[str, Any],
    *,
    timestamp: str | None = None,
) -> dict[str, Any]:
    """Build one JSONL history record from a Tuya power status snapshot."""
    ts = timestamp or snapshot.get("updated_at")
    if not ts:
        ts = datetime.now(timezone.utc).replace(microsecond=0).isoformat()

    appliances_raw = snapshot.get("appliances")
    appliances: dict[str, Any] = {}
    if isinstance(appliances_raw, dict):
        for appliance_key, entry in appliances_raw.items():
            if isinstance(entry, dict):
                appliances[str(appliance_key)] = _compact_appliance(entry)

    record: dict[str, Any] = {
        "timestamp": str(ts),
        "source": snapshot.get("source"),
        "limited": snapshot.get("limited", False),
        "appliances": appliances,
    }
    return record


@dataclass(frozen=True)
class TuyaPowerHistoryRecord:
    timestamp: datetime
    source: str | None
    limited: bool
    appliances: dict[str, dict[str, Any]]

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TuyaPowerHistoryRecord | None:
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
        return cls(
            timestamp=parsed_ts,
            source=str(source) if source else None,
            limited=limited,
            appliances=appliances,
        )

    def to_json_line(self) -> str:
        payload = {
            "timestamp": self.timestamp.isoformat(),
            "source": self.source,
            "limited": self.limited,
            "appliances": self.appliances,
        }
        return json.dumps(payload, separators=(",", ":"))


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
        record = TuyaPowerHistoryRecord.from_dict(data)
        if record is not None:
            records.append(record)
    return records


def read_history(path: Path | str | None = None) -> list[TuyaPowerHistoryRecord]:
    history_path = Path(path or default_history_path())
    if not history_path.is_file():
        return []
    try:
        return parse_history_lines(history_path.read_text(encoding="utf-8"))
    except OSError:
        return []


def prune_history_records(
    records: list[TuyaPowerHistoryRecord],
    *,
    now: datetime | None = None,
    retention_days: int = RETENTION_DAYS,
) -> list[TuyaPowerHistoryRecord]:
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=retention_days)
    return [record for record in records if record.timestamp >= cutoff]


def _persist_history_records(
    records: list[TuyaPowerHistoryRecord],
    history_path: Path,
) -> None:
    history_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = history_path.with_suffix(history_path.suffix + ".tmp")
    body = "\n".join(record.to_json_line() for record in records)
    if body:
        body += "\n"
    tmp.write_text(body, encoding="utf-8")
    tmp.replace(history_path)


def append_history_from_snapshot(
    snapshot: dict[str, Any],
    *,
    path: Path | str | None = None,
    now: datetime | None = None,
) -> bool:
    """Append one history record and prune old rows. Returns True on success."""
    history_path = Path(path or default_history_path())
    record = TuyaPowerHistoryRecord.from_dict(build_history_record(snapshot))
    if record is None:
        log.warning("Could not build Tuya power history record from snapshot")
        return False

    ts_now = now or record.timestamp
    with _history_io_lock:
        current = prune_history_records(read_history(history_path), now=ts_now)
        current.append(record)
        current.sort(key=lambda item: item.timestamp)
        try:
            _persist_history_records(current, history_path)
        except OSError as exc:
            log.warning("Could not persist Tuya power history: %s", exc)
            return False
    return True
