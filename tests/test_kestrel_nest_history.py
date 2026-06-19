"""Tests for Nest thermostat history append, prune, and record building."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from kestrel.nest_history import (  # noqa: E402
    RETENTION_DAYS,
    NestHistoryRecord,
    append_history_from_snapshot,
    build_history_record,
    parse_history_lines,
    prune_history_records,
    read_history,
)


def _snapshot(
    *,
    updated_at: str = "2026-06-19T12:00:00+00:00",
    downstairs_action: str = "COOLING",
    upstairs_action: str = "OFF",
) -> dict:
    return {
        "updated_at": updated_at,
        "thermostats": {
            "downstairs": {
                "name": "Downstairs",
                "device_name": "enterprises/.../devices/abc",
                "temperature": 72,
                "humidity": 65,
                "mode": "COOL",
                "action": downstairs_action,
                "setpoint": 71,
                "online": True,
            },
            "upstairs": {
                "name": "Upstairs",
                "temperature": 77,
                "humidity": 65,
                "mode": "MANUAL_ECO",
                "action": upstairs_action,
                "setpoint": 76,
                "online": True,
            },
        },
    }


class TestNestHistoryRecordBuilding:
    def test_build_history_record_keeps_compact_fields_only(self) -> None:
        record = build_history_record(_snapshot())
        downstairs = record["thermostats"]["downstairs"]
        assert downstairs == {
            "temperature": 72,
            "humidity": 65,
            "mode": "COOL",
            "action": "COOLING",
            "setpoint": 71,
            "online": True,
        }
        assert "name" not in downstairs
        assert record["timestamp"] == "2026-06-19T12:00:00+00:00"


class TestNestHistoryAppendAndPrune:
    def test_append_writes_jsonl_record(self, tmp_path: Path) -> None:
        path = tmp_path / "history.jsonl"
        snapshot = _snapshot()
        assert append_history_from_snapshot(snapshot, path=path) is True
        lines = path.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 1
        parsed = json.loads(lines[0])
        assert parsed["thermostats"]["downstairs"]["action"] == "COOLING"

    def test_append_multiple_then_prune_old_rows(self, tmp_path: Path) -> None:
        path = tmp_path / "history.jsonl"
        now = datetime(2026, 6, 19, 12, 0, tzinfo=timezone.utc)
        old_ts = (now - timedelta(days=RETENTION_DAYS + 1)).isoformat()
        recent_ts = (now - timedelta(days=1)).isoformat()

        append_history_from_snapshot(_snapshot(updated_at=old_ts), path=path, now=now)
        append_history_from_snapshot(_snapshot(updated_at=recent_ts), path=path, now=now)

        records = read_history(path)
        assert len(records) == 1
        assert records[0].timestamp == datetime.fromisoformat(recent_ts)

    def test_history_append_failure_does_not_raise(self, tmp_path: Path) -> None:
        blocked_dir = tmp_path / "blocked"
        blocked_dir.mkdir()
        blocked_dir.chmod(0o555)
        path = blocked_dir / "history.jsonl"
        try:
            result = append_history_from_snapshot(_snapshot(), path=path)
        finally:
            blocked_dir.chmod(0o755)
        assert result is False

    def test_malformed_lines_ignored(self, tmp_path: Path) -> None:
        path = tmp_path / "history.jsonl"
        good = NestHistoryRecord.from_dict(build_history_record(_snapshot()))
        assert good is not None
        path.write_text(
            "\n".join(["not-json", good.to_json_line(), "{bad"]) + "\n",
            encoding="utf-8",
        )
        records = read_history(path)
        assert len(records) == 1


class TestNestHistoryParsing:
    def test_parse_history_lines_round_trip(self) -> None:
        record = NestHistoryRecord.from_dict(build_history_record(_snapshot()))
        assert record is not None
        text = record.to_json_line()
        parsed = parse_history_lines(text)
        assert len(parsed) == 1
        assert parsed[0].thermostats["downstairs"]["action"] == "COOLING"

    def test_prune_history_records(self) -> None:
        now = datetime(2026, 6, 19, 12, 0, tzinfo=timezone.utc)
        records = [
            NestHistoryRecord(
                timestamp=now - timedelta(days=20),
                thermostats={},
            ),
            NestHistoryRecord(
                timestamp=now - timedelta(days=2),
                thermostats={},
            ),
        ]
        pruned = prune_history_records(records, now=now)
        assert len(pruned) == 1
