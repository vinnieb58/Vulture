"""Tests for Pelican long-term telemetry data discovery and backup."""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from pelican.manifest import ManifestData, TelemetryCoverage, render_manifest  # noqa: E402
from pelican.redaction import assert_manifest_safe  # noqa: E402
from pelican.telemetry_data import (  # noqa: E402
    backup_telemetry_data,
    discover_long_term_data,
    discover_sqlite_databases,
    verify_jsonl_copy,
)


def _touch_db(path: Path, table: str = "t") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute(f"CREATE TABLE {table} (id INTEGER PRIMARY KEY)")
    conn.commit()
    conn.close()


class TestTelemetryDiscovery:
    def test_discovers_sqlite_jsonl_and_snapshots(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        data = repo / "data"
        _touch_db(data / "vulture.db")
        _touch_db(data / "kestrel" / "kestrel.db")
        _touch_db(data / "finch_aliases.db")
        _touch_db(data / "finch_pending_selection.db")
        (data / "kestrel_nest_history.jsonl").write_text('{"ts":"x"}\n', encoding="utf-8")
        (data / "kestrel_tuya_power_history.jsonl").write_text('{"ts":"x"}\n', encoding="utf-8")
        (data / "kestrel" / "kestrel_status.json").write_text("{}", encoding="utf-8")
        (data / "kestrel" / "debug").mkdir(parents=True)
        (data / "kestrel" / "debug" / "page.html").write_text("<html></html>", encoding="utf-8")
        (repo / "devices.json").write_text("{}", encoding="utf-8")

        inventory = discover_long_term_data(repo, primary_db=data / "vulture.db")
        sqlite_names = {path.name for path in inventory.sqlite_files}
        assert sqlite_names == {"vulture.db", "kestrel.db", "finch_aliases.db"}
        assert "finch_pending_selection.db" not in sqlite_names
        assert len(inventory.jsonl_files) == 2
        assert len(inventory.snapshot_files) == 1
        assert len(inventory.config_files) == 1

    def test_excludes_debug_html_from_sqlite_discovery(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        debug_db = repo / "data" / "kestrel" / "debug" / "probe.db"
        _touch_db(debug_db)
        found = discover_sqlite_databases(repo)
        assert debug_db.resolve() not in found


class TestTelemetryBackup:
    def test_backs_up_secondary_sqlite_with_integrity(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        primary = repo / "data" / "vulture.db"
        kestrel = repo / "data" / "kestrel" / "kestrel.db"
        _touch_db(primary)
        _touch_db(kestrel)

        dest_root = tmp_path / "bundle"
        inventory = discover_long_term_data(repo, primary_db=primary)
        result = backup_telemetry_data(repo, dest_root=dest_root, primary_db=primary, inventory=inventory)

        assert result.ok
        assert (dest_root / "database" / "kestrel" / "kestrel.db").is_file()
        assert (dest_root / "database" / "integrity" / "kestrel" / "kestrel.db.txt").read_text() == "ok"
        assert not (dest_root / "database" / "vulture.db").exists()

    def test_jsonl_nonempty_verification_fails_when_empty_copy(self, tmp_path: Path) -> None:
        source = tmp_path / "history.jsonl"
        dest = tmp_path / "empty.jsonl"
        source.write_text('{"a":1}\n', encoding="utf-8")
        dest.write_text("", encoding="utf-8")
        result = verify_jsonl_copy(source, dest)
        assert not result.ok

    def test_jsonl_backup_copies_and_verifies(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        primary = repo / "data" / "vulture.db"
        _touch_db(primary)
        history = repo / "data" / "kestrel_nest_history.jsonl"
        history.write_text('{"ts":"2026-01-01T00:00:00+00:00"}\n', encoding="utf-8")

        dest_root = tmp_path / "bundle"
        inventory = discover_long_term_data(repo, primary_db=primary)
        result = backup_telemetry_data(repo, dest_root=dest_root, primary_db=primary, inventory=inventory)

        assert result.ok
        copied = dest_root / "telemetry" / "history" / "kestrel_nest_history.jsonl"
        assert copied.is_file()
        assert copied.read_text(encoding="utf-8") == history.read_text(encoding="utf-8")


class TestTelemetryManifest:
    def test_manifest_lists_telemetry_coverage(self) -> None:
        manifest = render_manifest(
            ManifestData(
                backup_timestamp="2026-06-29T00:00:00Z",
                hostname="raven",
                archive_name="raven-recovery-20260629T000000Z.tar.gz",
                telemetry_coverage=TelemetryCoverage(
                    sqlite_databases=["/repo/data/vulture.db", "/repo/data/kestrel/kestrel.db"],
                    jsonl_history=["/repo/data/kestrel_nest_history.jsonl"],
                    snapshots=["/repo/data/kestrel/kestrel_status.json"],
                    config_files=["/repo/devices.json"],
                    sqlite_integrity={"/repo/data/vulture.db": "ok"},
                    catalog_lines=["  - [sqlite/required] data/vulture.db — SQLite"],
                ),
            )
        )
        assert_manifest_safe(manifest)
        assert "telemetry_coverage:" in manifest
        assert "sqlite_databases:" in manifest
        assert "jsonl_history:" in manifest
        assert "/repo/data/kestrel/kestrel.db" in manifest
