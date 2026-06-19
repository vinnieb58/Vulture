"""Tests for Nest thermostat collection health summary."""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DASHBOARD_DIR = ROOT / "dashboard"
sys.path.insert(0, str(DASHBOARD_DIR))
sys.path.insert(0, str(ROOT))

from kestrel.nest_history import NestHistoryRecord, append_history_from_snapshot  # noqa: E402
from nest_collection_health import (  # noqa: E402
    STATUS_LIMITED,
    STATUS_MISSING,
    STATUS_OK,
    STATUS_STALE,
    compute_collection_health,
    get_nest_collection_health,
)
from nest_hvac_formatting import format_collection_health_display  # noqa: E402


def _record(ts: datetime, *, zones: tuple[str, ...] = ("downstairs", "upstairs")) -> NestHistoryRecord:
    return NestHistoryRecord(
        timestamp=ts,
        thermostats={zone: {"action": "OFF", "online": True} for zone in zones},
    )


def _write_history(path: Path, timestamps: list[datetime]) -> None:
    for ts in timestamps:
        append_history_from_snapshot(
            {
                "updated_at": ts.isoformat(),
                "thermostats": {
                    "downstairs": {"action": "OFF", "online": True},
                    "upstairs": {"action": "OFF", "online": True},
                },
            },
            path=path,
            now=ts,
        )


class TestNestCollectionHealth:
    def test_missing_when_no_history_file(self, tmp_path: Path, monkeypatch) -> None:
        missing = tmp_path / "missing.jsonl"
        monkeypatch.setattr("nest_collection_health.NEST_HISTORY_PATH", missing)
        now = datetime(2026, 6, 19, 12, 0, tzinfo=timezone.utc)

        health = get_nest_collection_health(path=missing, now=now)
        display = format_collection_health_display(now=now)

        assert health.status == STATUS_MISSING
        assert health.samples_last_30m == 0
        assert display["status"] == "Missing"
        assert display["missing"] is True

    def test_ok_when_recent_and_enough_samples(self) -> None:
        now = datetime(2026, 6, 19, 12, 0, tzinfo=timezone.utc)
        records = [
            _record(now - timedelta(minutes=offset))
            for offset in (25, 20, 15, 10, 5, 4)
        ]

        health = compute_collection_health(records, now=now)

        assert health.status == STATUS_OK
        assert health.samples_last_30m == 6
        assert health.age_minutes == 4
        assert health.zones == ["downstairs", "upstairs"]

    def test_limited_when_recent_but_too_few_samples(self) -> None:
        now = datetime(2026, 6, 19, 12, 0, tzinfo=timezone.utc)
        records = [
            _record(now - timedelta(minutes=4)),
            _record(now - timedelta(minutes=9)),
        ]

        health = compute_collection_health(records, now=now)

        assert health.status == STATUS_LIMITED
        assert health.samples_last_30m == 2
        assert health.age_minutes == 4

    def test_limited_when_enough_samples_but_age_between_10_and_15_minutes(self) -> None:
        now = datetime(2026, 6, 19, 12, 0, tzinfo=timezone.utc)
        records = [
            _record(now - timedelta(minutes=offset))
            for offset in (12, 18, 24)
        ]

        health = compute_collection_health(records, now=now)

        assert health.status == STATUS_LIMITED
        assert health.samples_last_30m == 3
        assert health.age_minutes == 12

    def test_stale_when_latest_sample_older_than_15_minutes(self) -> None:
        now = datetime(2026, 6, 19, 12, 0, tzinfo=timezone.utc)
        records = [
            _record(now - timedelta(minutes=20)),
            _record(now - timedelta(minutes=25)),
            _record(now - timedelta(minutes=30)),
        ]

        health = compute_collection_health(records, now=now)

        assert health.status == STATUS_STALE
        assert health.age_minutes == 20

    def test_history_file_integration(self, tmp_path: Path, monkeypatch) -> None:
        path = tmp_path / "history.jsonl"
        monkeypatch.setattr("nest_collection_health.NEST_HISTORY_PATH", path)
        now = datetime(2026, 6, 19, 12, 0, tzinfo=timezone.utc)
        _write_history(
            path,
            [now - timedelta(minutes=offset) for offset in (4, 9, 14, 19, 24, 29)],
        )

        display = format_collection_health_display(now=now)

        assert display["status"] == "OK"
        assert display["samples_last_30m_display"] == "6"
        assert display["latest_age"] == "4 minutes ago"
        assert display["zones"] == "downstairs, upstairs"
        assert display["style"] == "ok"
