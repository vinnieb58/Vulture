"""Append-only Nest thermostat polling history (JSONL).

Each poll appends one compact record. History is pruned to a fixed retention
window so the file does not grow forever. Failures to append history must not
prevent writing the latest status snapshot unless there is a serious error.
"""

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

DEFAULT_HISTORY_PATH = "data/kestrel_nest_history.jsonl"

_HISTORY_THERMOSTAT_FIELDS = (
    "temperature",
    "humidity",
    "mode",
    "action",
    "setpoint",
    "online",
)


def default_history_path() -> str:
    return (os.getenv("NEST_HISTORY_PATH") or DEFAULT_HISTORY_PATH).strip()


def _parse_iso(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _compact_thermostat(entry: dict[str, Any]) -> dict[str, Any]:
    compact: dict[str, Any] = {}
    for field in _HISTORY_THERMOSTAT_FIELDS:
        if field in entry:
            compact[field] = entry[field]
    return compact


def build_history_record(
    snapshot: dict[str, Any],
    *,
    timestamp: str | None = None,
) -> dict[str, Any]:
    """Build one JSONL history record from a Nest status snapshot."""
    ts = timestamp or snapshot.get("updated_at")
    if not ts:
        ts = datetime.now(timezone.utc).replace(microsecond=0).isoformat()

    thermostats_raw = snapshot.get("thermostats")
    thermostats: dict[str, Any] = {}
    if isinstance(thermostats_raw, dict):
        for room_key, entry in thermostats_raw.items():
            if isinstance(entry, dict):
                thermostats[str(room_key)] = _compact_thermostat(entry)

    return {
        "timestamp": str(ts),
        "thermostats": thermostats,
    }


@dataclass(frozen=True)
class NestHistoryRecord:
    timestamp: datetime
    thermostats: dict[str, dict[str, Any]]

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> NestHistoryRecord | None:
        ts_raw = data.get("timestamp")
        if not isinstance(ts_raw, str):
            return None
        parsed_ts = _parse_iso(ts_raw)
        if parsed_ts is None:
            return None

        thermostats_raw = data.get("thermostats")
        if not isinstance(thermostats_raw, dict):
            thermostats_raw = {}

        thermostats: dict[str, dict[str, Any]] = {}
        for room_key, entry in thermostats_raw.items():
            if isinstance(entry, dict):
                thermostats[str(room_key)] = dict(entry)

        return cls(timestamp=parsed_ts, thermostats=thermostats)

    def to_json_line(self) -> str:
        payload = {
            "timestamp": self.timestamp.isoformat(),
            "thermostats": self.thermostats,
        }
        return json.dumps(payload, separators=(",", ":"))


def parse_history_lines(text: str) -> list[NestHistoryRecord]:
    records: list[NestHistoryRecord] = []
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
        record = NestHistoryRecord.from_dict(data)
        if record is not None:
            records.append(record)
    return records


def read_history(path: Path | str | None = None) -> list[NestHistoryRecord]:
    history_path = Path(path or default_history_path())
    if not history_path.is_file():
        return []
    try:
        return parse_history_lines(history_path.read_text(encoding="utf-8"))
    except OSError:
        return []


def prune_history_records(
    records: list[NestHistoryRecord],
    *,
    now: datetime | None = None,
    retention_days: int = RETENTION_DAYS,
) -> list[NestHistoryRecord]:
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=retention_days)
    return [record for record in records if record.timestamp >= cutoff]


def _persist_history_records(
    records: list[NestHistoryRecord],
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
    record = NestHistoryRecord.from_dict(build_history_record(snapshot))
    if record is None:
        log.warning("Could not build Nest history record from snapshot")
        return False

    ts_now = now or record.timestamp
    with _history_io_lock:
        current = prune_history_records(read_history(history_path), now=ts_now)
        current.append(record)
        current.sort(key=lambda item: item.timestamp)
        try:
            _persist_history_records(current, history_path)
        except OSError as exc:
            log.warning("Could not persist Nest history: %s", exc)
            return False
    return True
