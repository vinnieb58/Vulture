"""Tests for indefinite long-term telemetry archives vs dashboard rolling retention."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DASHBOARD_DIR = ROOT / "dashboard"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(DASHBOARD_DIR))
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

from raven_metrics_history import (  # noqa: E402
    MetricsSample,
    append_sample,
    prune_samples,
    read_history,
)
from kestrel.nest_history import (  # noqa: E402
    DASHBOARD_RETENTION_DAYS,
    append_history_from_snapshot,
    read_history as read_nest_history,
)
from kestrel.telemetry_retention import (  # noqa: E402
    archive_record_count,
    oldest_archive_timestamp,
    parse_record_timestamp,
)
from kestrel.tuya_power_history import (  # noqa: E402
    append_history_from_snapshot as append_tuya_history,
    read_history as read_tuya_history,
)
from pelican.telemetry_data import discover_long_term_data  # noqa: E402


def _nest_snapshot(updated_at: str) -> dict:
    return {
        "updated_at": updated_at,
        "thermostats": {
            "downstairs": {
                "temperature": 72,
                "humidity": 65,
                "mode": "COOL",
                "action": "COOLING",
                "raw_hvac_status": "COOLING",
                "raw_thermostat_mode": "COOL",
                "eco_mode": "OFF",
                "setpoint": 71,
                "online": True,
            }
        },
    }


def _tuya_snapshot(updated_at: str) -> dict:
    return {
        "updated_at": updated_at,
        "source": "local",
        "limited": False,
        "appliances": {
            "fridge": {
                "voltage_v": 120.0,
                "power_w": 150.0,
                "current_a": 1.2,
                "online": True,
                "source": "local",
            }
        },
    }


class TestNestLongTermArchive:
    def test_dashboard_prunes_but_archive_retains_old_records(self, tmp_path: Path) -> None:
        dashboard = tmp_path / "nest_dashboard.jsonl"
        archive = tmp_path / "telemetry" / "nest_history_archive.jsonl"
        now = datetime(2026, 6, 19, 12, 0, tzinfo=timezone.utc)
        old_ts = (now - timedelta(days=DASHBOARD_RETENTION_DAYS + 1)).isoformat()
        recent_ts = (now - timedelta(days=1)).isoformat()

        assert append_history_from_snapshot(
            _nest_snapshot(old_ts),
            path=dashboard,
            archive_path=archive,
            now=now,
        )
        assert append_history_from_snapshot(
            _nest_snapshot(recent_ts),
            path=dashboard,
            archive_path=archive,
            now=now,
        )

        dashboard_records = read_nest_history(dashboard)
        assert len(dashboard_records) == 1
        assert dashboard_records[0].timestamp == datetime.fromisoformat(recent_ts)

        assert archive_record_count(archive) == 2
        oldest = oldest_archive_timestamp(archive)
        assert oldest == datetime.fromisoformat(old_ts)


class TestTuyaLongTermArchive:
    def test_archive_keeps_records_after_dashboard_prune(self, tmp_path: Path) -> None:
        dashboard = tmp_path / "tuya_dashboard.jsonl"
        archive = tmp_path / "telemetry" / "tuya_history_archive.jsonl"
        now = datetime(2026, 6, 19, 12, 0, tzinfo=timezone.utc)
        old_ts = (now - timedelta(days=15)).isoformat()
        recent_ts = (now - timedelta(hours=1)).isoformat()

        assert append_tuya_history(
            _tuya_snapshot(old_ts),
            path=dashboard,
            archive_path=archive,
            now=now,
        )
        assert append_tuya_history(
            _tuya_snapshot(recent_ts),
            path=dashboard,
            archive_path=archive,
            now=now,
        )

        assert len(read_tuya_history(dashboard)) == 1
        assert archive_record_count(archive) == 2


class TestRavenMetricsLongTermArchive:
    def test_archive_retains_samples_beyond_dashboard_window(self, tmp_path: Path) -> None:
        dashboard = tmp_path / "metrics_dashboard.jsonl"
        archive = tmp_path / "telemetry" / "raven_metrics_history_archive.jsonl"
        now = datetime(2026, 6, 19, 12, 0, tzinfo=timezone.utc)
        old_ts = now - timedelta(hours=72)
        recent_ts = now - timedelta(hours=1)

        old_sample = MetricsSample(
            timestamp=old_ts,
            load_1=1.0,
            load_5=1.0,
            load_15=1.0,
            memory_used_percent=50.0,
            memory_used_bytes=1,
            memory_total_bytes=2,
        )
        recent_sample = MetricsSample(
            timestamp=recent_ts,
            load_1=2.0,
            load_5=2.0,
            load_15=2.0,
            memory_used_percent=55.0,
            memory_used_bytes=1,
            memory_total_bytes=2,
        )

        append_sample(old_sample, path=dashboard, archive_path=archive, now=now)
        append_sample(recent_sample, path=dashboard, archive_path=archive, now=now)

        pruned = prune_samples(read_history(dashboard), now=now, retention_hours=48)
        assert len(pruned) == 1
        assert archive_record_count(archive) == 2


class TestPelicanDiscoversArchives:
    def test_pelican_inventory_includes_archive_jsonl(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        data = repo / "data"
        db = data / "vulture.db"
        db.parent.mkdir(parents=True)
        db.write_bytes(b"sqlite")

        archive = data / "telemetry" / "nest_history_archive.jsonl"
        archive.parent.mkdir(parents=True)
        archive.write_text('{"timestamp":"2020-01-01T00:00:00+00:00"}\n', encoding="utf-8")

        inventory = discover_long_term_data(repo, primary_db=db)
        rel_paths = {entry.rel_path for entry in inventory.catalog}
        assert "data/telemetry/nest_history_archive.jsonl" in rel_paths
        assert archive.resolve() in inventory.jsonl_files


class TestArchiveBootstrap:
    def test_bootstrap_copies_existing_dashboard_rows_once(self, tmp_path: Path) -> None:
        dashboard = tmp_path / "dashboard.jsonl"
        archive = tmp_path / "archive.jsonl"
        line = '{"timestamp":"2026-01-01T00:00:00+00:00","thermostats":{}}'
        dashboard.write_text(line + "\n", encoding="utf-8")

        assert append_history_from_snapshot(
            _nest_snapshot("2026-06-19T12:00:00+00:00"),
            path=dashboard,
            archive_path=archive,
        )
        archive_lines = [line for line in archive.read_text(encoding="utf-8").splitlines() if line.strip()]
        assert len(archive_lines) == 2
        assert parse_record_timestamp(archive_lines[0]) == "2026-01-01T00:00:00+00:00"
