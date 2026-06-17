"""
Tests for scripts/prune_ended_hunts.py
"""

from __future__ import annotations

import sqlite3
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine.hunt_repository import create_hunt, init_hunts_table, list_hunts
from models.hunt import Hunt
from scripts.prune_ended_hunts import (
    ENDED_STATUS,
    apply_prune,
    build_report,
    fetch_ended_hunts,
    format_report,
    run,
)


@pytest.fixture()
def temp_db(tmp_path):
    db_path = tmp_path / "vulture.db"
    conn = sqlite3.connect(db_path)
    conn.close()
    return db_path


def _seed_hunts(db_path: Path) -> None:
    import engine.database as db_module

    original = db_module.DB_PATH
    db_module.DB_PATH = db_path
    try:
        init_hunts_table()
        create_hunt(Hunt(name="active_one", status="active"))
        create_hunt(Hunt(name="paused_one", status="paused"))
        create_hunt(Hunt(name="ended_one", status=ENDED_STATUS))
        create_hunt(Hunt(name="ended_two", status=ENDED_STATUS))
    finally:
        db_module.DB_PATH = original


def _insert_hunt_direct(db_path: Path, name: str, status: str) -> None:
    import engine.database as db_module

    original = db_module.DB_PATH
    db_module.DB_PATH = db_path
    try:
        init_hunts_table()
        create_hunt(Hunt(name=name, status=status))
    finally:
        db_module.DB_PATH = original


class TestPruneEndedHunts:
    def test_fetch_ended_hunts_only(self, temp_db):
        _seed_hunts(temp_db)
        conn = sqlite3.connect(temp_db)
        conn.row_factory = sqlite3.Row
        ended = fetch_ended_hunts(conn)
        conn.close()
        names = {h.name for h in ended}
        assert names == {"ended_one", "ended_two"}

    def test_dry_run_does_not_delete(self, temp_db, capsys):
        _seed_hunts(temp_db)
        rc = run(temp_db, apply=False)
        assert rc == 0
        out = capsys.readouterr().out
        assert "DRY-RUN" in out
        assert "ended_one" in out
        assert "ended_two" in out
        assert "Related listing rows affected: 0" in out

        import engine.database as db_module

        original = db_module.DB_PATH
        db_module.DB_PATH = temp_db
        try:
            remaining = list_hunts()
        finally:
            db_module.DB_PATH = original
        assert len(remaining) == 4

    def test_apply_deletes_only_ended(self, temp_db, capsys):
        _seed_hunts(temp_db)
        rc = run(temp_db, apply=True)
        assert rc == 0
        out = capsys.readouterr().out
        assert "Deleted 2 ended hunt(s)" in out

        import engine.database as db_module

        original = db_module.DB_PATH
        db_module.DB_PATH = temp_db
        try:
            remaining = list_hunts()
        finally:
            db_module.DB_PATH = original
        names = {h.name for h in remaining}
        assert names == {"active_one", "paused_one"}

    def test_apply_on_empty_db(self, temp_db, capsys):
        import engine.database as db_module

        original = db_module.DB_PATH
        db_module.DB_PATH = temp_db
        try:
            init_hunts_table()
        finally:
            db_module.DB_PATH = original

        rc = run(temp_db, apply=True)
        assert rc == 0
        assert "Ended hunts found: 0" in capsys.readouterr().out

    def test_build_report_missing_db(self, tmp_path):
        missing = tmp_path / "missing.db"
        report = build_report(missing)
        assert report.count == 0
        text = format_report(report, apply=False)
        assert "Ended hunts found: 0" in text

    def test_apply_prune_returns_deleted_count(self, temp_db):
        _insert_hunt_direct(temp_db, "ended_x", ENDED_STATUS)
        report = build_report(temp_db)
        deleted = apply_prune(temp_db, [h.hunt_id for h in report.ended_hunts])
        assert deleted == 1
