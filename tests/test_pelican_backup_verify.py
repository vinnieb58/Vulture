"""Tests for Pelican backup dry-run verification script."""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from pelican_backup_verify import run_verify  # noqa: E402


def _touch_db(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
    conn.commit()
    conn.close()


class TestPelicanBackupVerify:
    def test_dry_run_passes_with_required_db(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        db = repo / "data" / "vulture.db"
        _touch_db(db)
        assert run_verify(repo, db_path=db) == 0

    def test_dry_run_fails_without_repo(self, tmp_path: Path) -> None:
        assert run_verify(tmp_path / "missing", db_path=tmp_path / "missing.db") == 1
