"""
Tests for Pelican v1 Raven recovery bundle backup helpers.
"""

from __future__ import annotations

import sqlite3
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from pelican.config import SCRIPT_VERSION  # noqa: E402
from pelican.inventory import (  # noqa: E402
    classify_required_paths,
    copy_optional_file,
    discover_recovery_docs,
    should_exclude_repo_path,
)
from pelican.manifest import ManifestData, render_manifest  # noqa: E402
from pelican.mount import MountVerification, verify_backup_target  # noqa: E402
from pelican.naming import (  # noqa: E402
    complete_bundle_name,
    format_backup_timestamp,
    incomplete_dir_name,
    is_completed_backup_filename,
    is_pelican_managed_name,
    parse_backup_stamp,
)
from pelican.redaction import assert_manifest_safe, is_secret_key, redact_env_line  # noqa: E402
from pelican.retention import plan_retention  # noqa: E402
from pelican.sqlite_backup import backup_and_verify_sqlite, run_integrity_check  # noqa: E402
from pelican_backup import build_context, run_backup  # noqa: E402


class TestBackupNaming:
    def test_format_backup_timestamp_utc(self):
        from datetime import datetime, timezone

        stamp = format_backup_timestamp(datetime(2026, 6, 19, 14, 30, 0, tzinfo=timezone.utc))
        assert stamp == "20260619T143000Z"

    def test_complete_and_incomplete_names(self):
        stamp = "20260619T143000Z"
        assert complete_bundle_name(stamp, prefer_zstd=False).endswith(".tar.gz")
        assert incomplete_dir_name(stamp).endswith(".incomplete")

    def test_completed_pattern_matching(self):
        assert is_completed_backup_filename("raven-recovery-20260619T143000Z.tar.gz")
        assert is_completed_backup_filename("raven-recovery-20260619T143000Z.tar.zst")
        assert not is_completed_backup_filename("random-backup.tar.gz")
        assert is_pelican_managed_name("raven-recovery-20260619T143000Z.incomplete")

    def test_parse_backup_stamp(self):
        assert parse_backup_stamp("raven-recovery-20260619T143000Z.tar.gz") == "20260619T143000Z"


class TestRetentionSelection:
    def _bundle(self, target: Path, stamp: str) -> Path:
        name = complete_bundle_name(stamp, prefer_zstd=False)
        path = target / name
        path.write_bytes(b"bundle")
        return path

    def test_keeps_newest_and_never_deletes_latest(self, tmp_path):
        self._bundle(tmp_path, "20260601T120000Z")
        self._bundle(tmp_path, "20260602T120000Z")
        pending = tmp_path / complete_bundle_name("20260603T120000Z", prefer_zstd=False)
        plan = plan_retention(tmp_path, retain_count=2, pending_new_bundle=pending)
        assert plan.newest == pending
        assert len(plan.keep) == 2
        assert len(plan.delete) == 1
        assert plan.delete[0].name == complete_bundle_name("20260601T120000Z", prefer_zstd=False)

    def test_only_matches_pelican_pattern(self, tmp_path):
        self._bundle(tmp_path, "20260601T120000Z")
        (tmp_path / "notes.txt").write_text("keep me", encoding="utf-8")
        plan = plan_retention(tmp_path, retain_count=1)
        assert plan.delete == []


class TestMountValidation:
    def test_rejects_autofs_placeholder(self, tmp_path):
        mountpoint = tmp_path / "pelican_backup"
        mountpoint.mkdir()

        def fake_findmnt(path, timeout):
            return "systemd-1", "autofs"

        def fake_root(timeout):
            return "/dev/sda2"

        result = verify_backup_target(
            mountpoint,
            access_runner=lambda path, timeout: (True, None),
            findmnt_runner=fake_findmnt,
            root_source_resolver=fake_root,
        )
        assert not result.ok
        assert "Automount placeholder" in result.message

    def test_rejects_root_filesystem_alias(self, tmp_path):
        mountpoint = tmp_path / "pelican_backup"
        mountpoint.mkdir()

        def fake_findmnt(path, timeout):
            return "/dev/sda2", "ext4"

        def fake_root(timeout):
            return "/dev/sda2"

        result = verify_backup_target(
            mountpoint,
            access_runner=lambda path, timeout: (True, None),
            findmnt_runner=fake_findmnt,
            root_source_resolver=fake_root,
        )
        assert not result.ok
        assert "root filesystem" in result.message

    def test_accepts_real_backing_mount(self, tmp_path):
        mountpoint = tmp_path / "pelican_backup"
        mountpoint.mkdir()

        def fake_findmnt(path, timeout):
            return "/dev/sdb1", "ntfs3"

        def fake_root(timeout):
            return "/dev/sda2"

        result = verify_backup_target(
            mountpoint,
            access_runner=lambda path, timeout: (True, None),
            findmnt_runner=fake_findmnt,
            root_source_resolver=fake_root,
        )
        assert result.ok


class TestRequiredOptionalHandling:
    def test_required_missing_failures(self, tmp_path):
        result = classify_required_paths(
            repo_root=tmp_path / "missing-repo",
            db_path=tmp_path / "missing.db",
            env_path=tmp_path / "missing.env",
        )
        assert len(result.required_failures) == 3

    def test_optional_missing_is_recorded_not_fatal(self, tmp_path):
        (tmp_path / "vulture.db").write_bytes(b"db")
        (tmp_path / ".env").write_text("DISCORD_TOKEN=secret\n", encoding="utf-8")
        result = classify_required_paths(
            repo_root=tmp_path,
            db_path=tmp_path / "vulture.db",
            env_path=tmp_path / ".env",
        )
        assert result.required_failures == []

        dest = tmp_path / "dest" / "fstab"
        copy_optional_file(Path("/definitely/missing/fstab"), dest, result)
        assert "/definitely/missing/fstab" in result.missing_optional

    def test_repo_exclusions(self):
        assert should_exclude_repo_path(Path(".venv/bin/python"))
        assert should_exclude_repo_path(Path("logs/bot.log"))
        assert should_exclude_repo_path(Path(".env"))
        assert not should_exclude_repo_path(Path("engine/database.py"))


class TestSecretRedaction:
    def test_secret_keys_redacted(self):
        assert is_secret_key("DISCORD_TOKEN")
        assert is_secret_key("API_KEY")
        redacted = redact_env_line("DISCORD_TOKEN=abc123")
        assert "abc123" not in redacted
        assert "***REDACTED***" in redacted

    def test_manifest_rejects_secret_disclosure(self):
        manifest = render_manifest(
            ManifestData(
                backup_timestamp="2026-06-19T00:00:00Z",
                hostname="raven",
                script_version=SCRIPT_VERSION,
                archive_name="raven-recovery-20260619T120000Z.tar.gz",
                archive_checksum="abc",
            )
        )
        assert_manifest_safe(manifest)
        with pytest.raises(ValueError):
            assert_manifest_safe("DISCORD_TOKEN=super-secret")


class TestSqliteBackup:
    def _make_db(self, path: Path) -> None:
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE sample (id INTEGER PRIMARY KEY, name TEXT)")
        conn.execute("INSERT INTO sample (name) VALUES ('alpha')")
        conn.commit()
        conn.close()

    def test_online_backup_and_integrity(self, tmp_path):
        source = tmp_path / "live.db"
        dest = tmp_path / "backup.db"
        self._make_db(source)
        result = backup_and_verify_sqlite(source, dest)
        assert result.ok
        assert result.integrity_result == "ok"
        assert run_integrity_check(dest) == "ok"

    def test_integrity_failure_aborts_backup(self, tmp_path):
        source = tmp_path / "live.db"
        dest = tmp_path / "backup.db"
        self._make_db(source)

        with patch("pelican.sqlite_backup.run_integrity_check", return_value="corrupt"):
            result = backup_and_verify_sqlite(source, dest)
        assert not result.ok
        assert "integrity check failed" in result.message.lower()


class TestIncompleteBackupsNotPublished:
    def _seed_repo(self, repo_root: Path) -> None:
        repo_root.mkdir(parents=True, exist_ok=True)
        (repo_root / "data").mkdir()
        db = repo_root / "data" / "vulture.db"
        conn = sqlite3.connect(db)
        conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
        conn.commit()
        conn.close()
        (repo_root / ".env").write_text("DISCORD_TOKEN=local-test-token\n", encoding="utf-8")
        (repo_root / "README.md").write_text("# vulture\n", encoding="utf-8")

    def test_failed_sqlite_does_not_publish_archive(self, tmp_path):
        repo = tmp_path / "repo"
        target = tmp_path / "pelican"
        target.mkdir()
        self._seed_repo(repo)

        ctx = build_context(
            repo_root=repo,
            backup_target=target,
            db_path=repo / "data" / "vulture.db",
            env_path=repo / ".env",
            retention_count=14,
            stamp="20260619T120000Z",
            prefer_zstd=False,
        )

        with patch("pelican_backup.verify_backup_target") as verify_mock, patch(
            "pelican_backup.capture_git_recovery", return_value=("main", "abc123")
        ), patch("pelican_backup.capture_repository_tree"), patch(
            "pelican_backup.copy_secrets"
        ), patch(
            "pelican_backup.copy_repo_systemd_defs"
        ), patch(
            "pelican_backup.copy_repo_docker_compose"
        ), patch(
            "pelican_backup.copy_optional_host_config"
        ), patch(
            "pelican_backup.copy_recovery_docs"
        ), patch(
            "pelican_backup.backup_and_verify_sqlite"
        ) as sqlite_mock:
            verify_mock.return_value = MountVerification(
                ok=True, path=str(target), message="ok", backing_source="/dev/sdb1"
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

            rc = run_backup(ctx, skip_retention=True)

        assert rc != 0
        assert not ctx.final_archive.exists()
        assert not Path(f"{ctx.final_archive}.sha256").exists()
        assert not any(target.glob("raven-recovery-*.tar.gz"))

    def test_success_publishes_complete_bundle(self, tmp_path):
        repo = tmp_path / "repo"
        target = tmp_path / "pelican"
        target.mkdir()
        self._seed_repo(repo)

        subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
        subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True)

        ctx = build_context(
            repo_root=repo,
            backup_target=target,
            db_path=repo / "data" / "vulture.db",
            env_path=repo / ".env",
            retention_count=14,
            stamp="20260619T130000Z",
            prefer_zstd=False,
        )

        with patch("pelican_backup.verify_backup_target") as verify_mock, patch(
            "pelican_backup.copy_optional_host_config"
        ):
            verify_mock.return_value = MountVerification(
                ok=True, path=str(target), message="ok", backing_source="/dev/sdb1"
            )
            rc = run_backup(ctx, skip_retention=True)

        assert rc == 0
        assert ctx.final_archive.exists()
        assert Path(f"{ctx.final_archive}.sha256").exists()
        assert Path(f"{ctx.final_archive}.manifest").exists()
        assert not (target / ".pelican-staging" / incomplete_dir_name("20260619T130000Z")).exists()


class TestRecoveryDocDiscovery:
    def test_discovers_pelican_doc(self, tmp_path):
        doc = tmp_path / "docs" / "current" / "PELiCAN_BACKUP.md"
        doc.parent.mkdir(parents=True)
        doc.write_text("# pelican\n", encoding="utf-8")
        found = discover_recovery_docs(tmp_path)
        assert doc in found
