"""Tests for Raven pre-update backup helpers."""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from raven_preupdate_backup import (  # noqa: E402
    BACKUP_DIR_RE,
    collect_source_files,
    list_completed_preupdate_backups,
    plan_preupdate_retention,
    run_preupdate_backup,
)
from pelican.mount import MountVerification  # noqa: E402


def _touch(path: Path, content: str = "x") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_collect_source_files_includes_critical_paths(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _touch(repo / ".env", "SECRET=not-printed\n")
    _touch(repo / "data" / "vulture.db", "db")
    _touch(repo / "data" / "kestrel" / "nest.db", "db")
    _touch(repo / "data" / "discord_tokens.json", "{}")
    _touch(repo / "data" / "trade_ledger.db", "db")
    _touch(repo / "data" / "kestrel_nest_status.json", "{}")
    _touch(repo / "data" / "kestrel_nest_history.jsonl", "{}\n")
    _touch(repo / "data" / "raven_metrics_history.jsonl", "{}\n")
    _touch(repo / "logs" / "app.log", "log")
    _touch(repo / "data" / "__pycache__" / "cache.db", "db")

    rel_paths = {path.relative_to(repo).as_posix() for path in collect_source_files(repo)}

    assert rel_paths == {
        ".env",
        "data/discord_tokens.json",
        "data/kestrel/nest.db",
        "data/kestrel_nest_history.jsonl",
        "data/kestrel_nest_status.json",
        "data/trade_ledger.db",
        "data/vulture.db",
    }


def test_plan_preupdate_retention_keeps_newest_twenty(tmp_path: Path) -> None:
    parent = tmp_path / "raven-preupdate"
    parent.mkdir()
    backups = []
    for day in range(1, 23):
        stamp = f"202506{day:02d}T120000Z"
        backup = parent / f"raven-preupdate-{stamp}"
        backup.mkdir()
        backups.append(backup)

    keep, delete = plan_preupdate_retention(parent, retain_count=20)
    assert len(keep) == 20
    assert len(delete) == 2
    assert delete[0].name == "raven-preupdate-20250601T120000Z"
    assert delete[1].name == "raven-preupdate-20250602T120000Z"
    assert keep[-1].name == "raven-preupdate-20250622T120000Z"


def test_list_completed_preupdate_backups_ignores_unrelated_entries(tmp_path: Path) -> None:
    parent = tmp_path / "raven-preupdate"
    parent.mkdir()
    valid = parent / "raven-preupdate-20250610T120000Z"
    valid.mkdir()
    (parent / "scratch").mkdir()
    (parent / "raven-preupdate-invalid").mkdir()

    backups = list_completed_preupdate_backups(parent)
    assert backups == [valid]
    assert BACKUP_DIR_RE.match(valid.name)


def test_run_preupdate_backup_creates_snapshot_and_prunes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    pelican = tmp_path / "pelican_backup"
    preupdate_parent = pelican / "raven-preupdate"
    preupdate_parent.mkdir(parents=True)

    old_backup = preupdate_parent / "raven-preupdate-20250601T120000Z"
    old_backup.mkdir()
    (old_backup / "marker.txt").write_text("old\n", encoding="utf-8")

    _touch(repo / ".env", "TOKEN=secret\n")
    _touch(repo / "data" / "vulture.db", "db")

    fixed_time = datetime(2025, 6, 23, 12, 34, 56, tzinfo=timezone.utc)

    monkeypatch.setattr(
        "raven_preupdate_backup.verify_backup_target",
        lambda _target: MountVerification(
            ok=True,
            path=str(pelican),
            message="ok",
            backing_source="/dev/sda2",
            backing_fstype="ext4",
        ),
    )

    result = run_preupdate_backup(
        repo,
        pelican_target=pelican,
        retention_count=20,
        timestamp=fixed_time,
    )

    assert result.ok is True
    assert result.files_included == 2
    assert result.pruned_count == 0
    assert result.backup_path == preupdate_parent / "raven-preupdate-20250623T123456Z"
    assert (result.backup_path / ".env").read_text(encoding="utf-8") == "TOKEN=secret\n"
    assert (result.backup_path / "data" / "vulture.db").is_file()


def test_run_preupdate_backup_warns_when_target_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    _touch(repo / ".env", "TOKEN=secret\n")

    monkeypatch.setattr(
        "raven_preupdate_backup.verify_backup_target",
        lambda _target: MountVerification(
            ok=False,
            path="/mnt/storage/pelican_backup",
            message="Pelican drive unavailable",
        ),
    )

    result = run_preupdate_backup(repo, pelican_target=tmp_path / "missing")
    assert result.ok is False
    assert result.files_included == 0
    assert "Pelican drive unavailable" in result.message
