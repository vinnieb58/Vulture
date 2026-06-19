"""Nest thermostat collection health from JSONL polling history."""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from nest_history import NestHistoryRecord, read_history

NEST_HISTORY_PATH = Path(
    os.environ.get("NEST_HISTORY_PATH", "/app/data/kestrel_nest_history.jsonl")
)

COLLECTION_WINDOW_MINUTES = 30
OK_MAX_AGE_MINUTES = 10
STALE_AFTER_MINUTES = 15
OK_MIN_SAMPLES_IN_WINDOW = 3

_THERMOSTAT_ORDER = ("downstairs", "upstairs")

STATUS_OK = "ok"
STATUS_LIMITED = "limited"
STATUS_STALE = "stale"
STATUS_MISSING = "missing"


@dataclass(frozen=True)
class NestCollectionHealth:
    status: str
    samples_last_30m: int
    latest_sample_at: datetime | None
    age_minutes: int | None
    zones: list[str]

    @property
    def status_label(self) -> str:
        labels = {
            STATUS_OK: "OK",
            STATUS_LIMITED: "Limited",
            STATUS_STALE: "Stale",
            STATUS_MISSING: "Missing",
        }
        return labels.get(self.status, self.status.title())


def _ordered_zones(thermostats: dict[str, Any]) -> list[str]:
    zones = [str(key) for key in thermostats.keys()]
    order_index = {key: index for index, key in enumerate(_THERMOSTAT_ORDER)}
    return sorted(zones, key=lambda zone: (order_index.get(zone, 100), zone))


def _compute_status(*, age_minutes: int | None, samples_last_30m: int) -> str:
    if age_minutes is None:
        return STATUS_MISSING
    if age_minutes > STALE_AFTER_MINUTES:
        return STATUS_STALE
    if age_minutes <= OK_MAX_AGE_MINUTES and samples_last_30m >= OK_MIN_SAMPLES_IN_WINDOW:
        return STATUS_OK
    return STATUS_LIMITED


def compute_collection_health(
    records: list[NestHistoryRecord],
    *,
    now: datetime | None = None,
) -> NestCollectionHealth:
    ts_now = now or datetime.now(timezone.utc)
    if not records:
        return NestCollectionHealth(
            status=STATUS_MISSING,
            samples_last_30m=0,
            latest_sample_at=None,
            age_minutes=None,
            zones=[],
        )

    latest = max(records, key=lambda record: record.timestamp)
    window_start = ts_now - timedelta(minutes=COLLECTION_WINDOW_MINUTES)
    samples_last_30m = sum(
        1 for record in records if window_start <= record.timestamp <= ts_now
    )
    age_minutes = max(0, int((ts_now - latest.timestamp).total_seconds() // 60))
    status = _compute_status(age_minutes=age_minutes, samples_last_30m=samples_last_30m)

    return NestCollectionHealth(
        status=status,
        samples_last_30m=samples_last_30m,
        latest_sample_at=latest.timestamp,
        age_minutes=age_minutes,
        zones=_ordered_zones(latest.thermostats),
    )


def get_nest_collection_health(
    *,
    path: Path | None = None,
    now: datetime | None = None,
) -> NestCollectionHealth:
    """Read Nest history and compute collection health for the dashboard."""
    history_path = path or NEST_HISTORY_PATH
    if not history_path.is_file():
        return NestCollectionHealth(
            status=STATUS_MISSING,
            samples_last_30m=0,
            latest_sample_at=None,
            age_minutes=None,
            zones=[],
        )

    records = read_history(history_path)
    return compute_collection_health(records, now=now)
