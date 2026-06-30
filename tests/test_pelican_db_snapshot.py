"""
Tests for Pelican Raven database snapshot backup helpers.
"""

from __future__ import annotations

import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from pelican.db_snapshot_inventory import (  # noqa: E402
    classify_snapshot_sources,
    discover_json_state_files,
    discover_sqlite_sources,
)
from pelican.db_snapshot_naming import (  # noqa: E402
    complete_snapshot_name,
    is_completed_snapshot_filename,
    parse_snapshot_stamp,
)
from pelican.db_snapshot_retention import plan_snapshot_retention  # noqa: E402
from pelican.mount import MountVerification  # noqa: E402
from pelican_db_snapshot import build_context, run_snapshot  # noqa: E402


class TestSnapshotNaming:
    def test_completed_pattern_matching(self):
        assert is_completed_snapshot_filename("raven-db-snapshot-20260619T143000Z.tar.gz")
        assert is_completed_snapshot_filename("raven-db-snapshot-20260619T143000Z.tar.zst")
        assert not is_completed_snapshot_filename("raven-recovery-20260619T143000Z.tar.gz")

    def test_parse_snapshot_stamp(self):
        assert parse_snapshot_stamp("raven-db-snapshot-20260619T143000Z.tar.gz") == "20260619T143000Z"


class TestSnapshotInventory:
    def _make_db(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
        conn.commit()
        conn.close()

    def test_discovers_top_level_and_kestrel_databases(self, tmp_path):
        self._make_db(tmp_path / "data" / "vulture.db")
        self._make_db(tmp_path / "data" / "finch_activity.db")
        self._make_db(tmp_path / "data" / "kestrel" / "kestrel.db")

        sources = discover_sqlite_sources(tmp_path)
        rel_names = {str(rel) for rel, _ in sources}
        assert rel_names == {
            "data/vulture.db",
            "data/finch_activity.db",
            "data/kestrel/kestrel.db",
        }

    def test_includes_small_json_state_files(self, tmp_path):
        status = tmp_path / "data" / "kestrel_nest_status.json"
        status.parent.mkdir(parents=True)
        status.write_text('{"ok": true}\n', encoding="utf-8")

        included, skipped = discover_json_state_files(tmp_path, max_bytes=1024)
        assert [(str(rel), src) for rel, src in included] == [
            ("data/kestrel_nest_status.json", status)
        ]
        assert skipped == []

    def test_skips_large_json_state_files(self, tmp_path):
        history = tmp_path / "data" / "kestrel_nest_history.jsonl"
        history.parent.mkdir(parents=True)
        history.write_bytes(b"x" * 2048)

        included, skipped = discover_json_state_files(tmp_path, max_bytes=1024)
        assert included == []
        assert len(skipped) == 1
        assert "kestrel_nest_history.jsonl" in skipped[0]

    def test_requires_vulture_database(self, tmp_path):
        inventory = classify_snapshot_sources(tmp_path)
        assert inventory.missing_required


class TestSnapshotRetention:
    def _snapshot(self, target: Path, stamp: str) -> Path:
        name = complete_snapshot_name(stamp, prefer_zstd=False)
        path = target / name
        path.write_bytes(b"snapshot")
        return path

    def test_deletes_snapshots_older_than_retention_window(self, tmp_path):
        old_stamp = (datetime.now(timezone.utc) - timedelta(days=20)).strftime("%Y%m%dT%H%M%SZ")
        new_stamp = (datetime.now(timezone.utc) - timedelta(days=2)).strftime("%Y%m%dT%H%M%SZ")
        old = self._snapshot(tmp_path, old_stamp)
        new = self._snapshot(tmp_path, new_stamp)

        plan = plan_snapshot_retention(tmp_path, retention_days=14)
        assert old in plan.delete
        assert new in plan.keep


class TestSnapshotRun:
    def _seed_repo(self, repo_root: Path) -> None:
        repo_root.mkdir(parents=True, exist_ok=True)
        data = repo_root / "data"
        data.mkdir()
        kestrel = data / "kestrel"
        kestrel.mkdir()

        for rel in ("vulture.db", "finch_activity.db", "kestrel/kestrel.db"):
            conn = sqlite3.connect(data / rel)
            conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
            conn.commit()
            conn.close()

        (data / "kestrel_nest_status.json").write_text('{"state":"home"}\n', encoding="utf-8")

    def test_success_publishes_compressed_snapshot(self, tmp_path):
        repo = tmp_path / "repo"
        target = tmp_path / "pelican_backup" / "raven-db-snapshots"
        target.mkdir(parents=True)
        self._seed_repo(repo)

        ctx = build_context(
            repo_root=repo,
            snapshot_target=target,
            retention_days=14,
            max_json_bytes=4096,
            stamp="20260619T150000Z",
            prefer_zstd=False,
        )

        with patch("pelican_db_snapshot.verify_backup_target") as verify_mock:
            verify_mock.return_value = MountVerification(
                ok=True, path=str(target.parent), message="ok", backing_source="/dev/sdb1"
            )
            rc = run_snapshot(ctx, skip_retention=True)

        assert rc == 0
        assert ctx.final_archive.exists()
        assert Path(f"{ctx.final_archive}.sha256").exists()

    def test_sqlite_integrity_failure_does_not_publish(self, tmp_path):
        repo = tmp_path / "repo"
        target = tmp_path / "pelican_backup" / "raven-db-snapshots"
        target.mkdir(parents=True)
        self._seed_repo(repo)

        ctx = build_context(
            repo_root=repo,
            snapshot_target=target,
            retention_days=14,
            max_json_bytes=4096,
            stamp="20260619T160000Z",
            prefer_zstd=False,
        )

        with patch("pelican_db_snapshot.verify_backup_target") as verify_mock, patch(
            "pelican_db_snapshot.backup_and_verify_sqlite"
        ) as sqlite_mock:
            verify_mock.return_value = MountVerification(
                ok=True, path=str(target.parent), message="ok", backing_source="/dev/sdb1"
            )
            sqlite_mock.return_value = type(
                "R",
                (),
                {
                    "ok": False,
                    "integrity_result": "corrupt",
                    "message": "SQLite integrity check failed: corrupt",
                },
            )()

            rc = run_snapshot(ctx, skip_retention=True)

        assert rc != 0
        assert not ctx.final_archive.exists()
        assert not any(target.glob("raven-db-snapshot-*.tar.gz"))

    def test_published_snapshot_contains_backed_up_databases(self, tmp_path):
        repo = tmp_path / "repo"
        target = tmp_path / "pelican_backup" / "raven-db-snapshots"
        target.mkdir(parents=True)
        self._seed_repo(repo)

        ctx = build_context(
            repo_root=repo,
            snapshot_target=target,
            retention_days=14,
            max_json_bytes=4096,
            stamp="20260619T170000Z",
            prefer_zstd=False,
        )

        with patch("pelican_db_snapshot.verify_backup_target") as verify_mock:
            verify_mock.return_value = MountVerification(
                ok=True, path=str(target.parent), message="ok", backing_source="/dev/sdb1"
            )
            rc = run_snapshot(ctx, skip_retention=True)

        assert rc == 0
        proc = subprocess.run(
            ["tar", "-tzf", str(ctx.final_archive)],
            capture_output=True,
            text=True,
            check=True,
        )
        names = proc.stdout
        assert "data/vulture.db" in names
        assert "data/kestrel/kestrel.db" in names
        assert "data/kestrel_nest_status.json" in names
        assert "integrity_checks.txt" in names
