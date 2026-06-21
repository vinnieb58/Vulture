"""
Tests for Pelican backup monitoring and Discord alert deduplication.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from canary.alerting import decide_pelican_alert, process_pelican_alerts
from canary.pelican_backup import (
    BACKUP_BUNDLE_RE,
    check_pelican_backup,
    evaluate_backup_target_mount,
    evaluate_latest_archive,
    evaluate_service_result,
    evaluate_timer_health,
    find_latest_completed_archive,
)
from canary.subprocess_util import set_command_runner


def _healthy_pelican_check() -> dict:
    return {
        "status": "ok",
        "issue_codes": [],
        "alerts": [],
        "timer": {
            "unit": "pelican-backup.timer",
            "enabled": "enabled",
            "active": "active",
            "next_run": "2026-06-22T08:00:00+00:00",
            "has_future_run": True,
            "status": "ok",
            "issues": [],
        },
        "service": {
            "unit": "pelican-backup.service",
            "active": "inactive",
            "result": "success",
            "exec_main_status": 0,
            "has_run": True,
            "inactive_between_runs_ok": True,
            "status": "ok",
            "issues": [],
        },
        "mount": {
            "path": "/mnt/storage/pelican_backup",
            "mounted": True,
            "backing_source": "/dev/sdb1",
            "backing_fstype": "ext4",
            "status": "ok",
            "issues": [],
        },
        "archive": {
            "latest_name": "raven-recovery-20260620T030015Z.tar.zst",
            "latest_stamp": "20260620T030015Z",
            "age_hours": 12.0,
            "status": "ok",
            "issues": [],
        },
    }


def _systemctl_prop(args: list[str]) -> str | None:
    for arg in args:
        if arg.startswith("--property="):
            return arg.split("=", 1)[1]
    return None


class TestPelicanBackupChecks:
    def setup_method(self) -> None:
        set_command_runner(None)

    def teardown_method(self) -> None:
        set_command_runner(None)

    def test_backup_bundle_regex_matches_completed_only(self):
        assert BACKUP_BUNDLE_RE.match("raven-recovery-20260620T030015Z.tar.zst")
        assert BACKUP_BUNDLE_RE.match("raven-recovery-20260620T030015Z.tar.gz")
        assert BACKUP_BUNDLE_RE.match("raven-recovery-20260620T030015Z.incomplete") is None
        assert BACKUP_BUNDLE_RE.match("raven-recovery-20260620T030015Z.tar.zst.partial") is None
        assert BACKUP_BUNDLE_RE.match("full-disk-raven.img.zst") is None

    def test_timer_disabled(self):
        def runner(args, timeout):  # noqa: ARG001
            if args[0] == "systemctl" and "pelican-backup.timer" in args:
                prop = _systemctl_prop(args)
                if prop == "UnitFileState":
                    return True, "disabled"
                if prop == "ActiveState":
                    return True, "inactive"
                if prop == "NextElapseUSecRealtime":
                    return True, "0"
            return False, "unexpected"

        set_command_runner(runner)
        result = evaluate_timer_health("pelican-backup.timer")
        assert result["status"] == "critical"
        codes = {code for _, code, _ in result["issues"]}
        assert "TIMER_DISABLED" in codes
        assert "TIMER_INACTIVE" in codes

    def test_timer_active_but_no_future_run(self):
        def runner(args, timeout):  # noqa: ARG001
            if args[0] == "systemctl" and "pelican-backup.timer" in args:
                prop = _systemctl_prop(args)
                if prop == "UnitFileState":
                    return True, "enabled"
                if prop == "ActiveState":
                    return True, "active"
                if prop == "NextElapseUSecRealtime":
                    return True, "0"
            return False, "unexpected"

        set_command_runner(runner)
        result = evaluate_timer_health("pelican-backup.timer")
        assert result["status"] == "critical"
        assert any(code == "TIMER_NO_FUTURE_RUN" for _, code, _ in result["issues"])

    def test_oneshot_service_inactive_after_success_is_healthy(self):
        def runner(args, timeout):  # noqa: ARG001
            if args[0] == "systemctl" and "pelican-backup.service" in args:
                prop = _systemctl_prop(args)
                if prop == "ActiveState":
                    return True, "inactive"
                if prop == "Result":
                    return True, "success"
                if prop == "ExecMainStatus":
                    return True, "0"
                if prop == "ExecMainStartTimestamp":
                    return True, "Sat 2026-06-20 03:00:15 CDT"
            return False, "unexpected"

        set_command_runner(runner)
        result = evaluate_service_result("pelican-backup.service")
        assert result["status"] == "ok"
        assert result["inactive_between_runs_ok"] is True
        assert result["issues"] == []

    def test_most_recent_service_failure(self):
        def runner(args, timeout):  # noqa: ARG001
            if args[0] == "systemctl" and "pelican-backup.service" in args:
                prop = _systemctl_prop(args)
                if prop == "ActiveState":
                    return True, "failed"
                if prop == "Result":
                    return True, "exit-code"
                if prop == "ExecMainStatus":
                    return True, "1"
                if prop == "ExecMainStartTimestamp":
                    return True, "Sat 2026-06-20 03:00:15 CDT"
            return False, "unexpected"

        set_command_runner(runner)
        result = evaluate_service_result("pelican-backup.service")
        assert result["status"] == "critical"
        assert any(code == "SERVICE_LAST_RUN_FAILED" for _, code, _ in result["issues"])

    def test_real_mount_versus_autofs_placeholder(self, tmp_path: Path):
        mountpoint = str(tmp_path / "pelican_backup")
        Path(mountpoint).mkdir()

        def runner(args, timeout):  # noqa: ARG001
            if args[0] == "findmnt" and mountpoint in args:
                return True, "systemd-1 autofs\n"
            return False, "unexpected"

        set_command_runner(runner)
        with (
            patch("canary.pelican_backup.host_path", side_effect=lambda p: p),
            patch("canary.pelican_backup.path_access_check", return_value=(True, None)),
        ):
            result = evaluate_backup_target_mount(mountpoint)
        assert result["status"] == "critical"
        assert any(code == "MOUNT_AUTOFS_PLACEHOLDER" for _, code, _ in result["issues"])

        def real_runner(args, timeout):  # noqa: ARG001
            if args[0] == "findmnt" and mountpoint in args:
                return True, "/dev/sdb1 ext4\n"
            if args[0] == "findmnt" and args[-1] == "/":
                return True, "/dev/nvme0n1p2\n"
            return False, "unexpected"

        set_command_runner(real_runner)
        with (
            patch("canary.pelican_backup.host_path", side_effect=lambda p: p),
            patch("canary.pelican_backup.path_access_check", return_value=(True, None)),
        ):
            result = evaluate_backup_target_mount(mountpoint)
        assert result["status"] == "ok"
        assert result["backing_source"] == "/dev/sdb1"

    def test_no_completed_archive(self, tmp_path: Path):
        (tmp_path / "raven-recovery-20260620T030015Z.incomplete").mkdir()
        (tmp_path / "raven-recovery-20260620T030015Z.tar.zst.partial").write_bytes(b"x")
        (tmp_path / "legacy-full-disk-raven.img").write_bytes(b"x")

        result = evaluate_latest_archive(tmp_path, stale_hours=36, warn_hours=30)
        assert result["status"] == "critical"
        assert any(code == "NO_COMPLETED_ARCHIVE" for _, code, _ in result["issues"])

    def test_stale_archive(self, tmp_path: Path):
        stamp = (datetime.now(timezone.utc) - timedelta(hours=40)).strftime("%Y%m%dT%H%M%SZ")
        name = f"raven-recovery-{stamp}.tar.zst"
        archive = tmp_path / name
        archive.write_bytes(b"backup-bytes")
        Path(f"{archive}.sha256").write_text(
            f"{'a' * 64}  {name}\n",
            encoding="utf-8",
        )

        with patch("canary.pelican_backup._compute_sha256", return_value="a" * 64):
            result = evaluate_latest_archive(tmp_path, stale_hours=36, warn_hours=30)

        assert result["status"] == "critical"
        assert any(code == "BACKUP_STALE" for _, code, _ in result["issues"])

    def test_missing_checksum(self, tmp_path: Path):
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        name = f"raven-recovery-{stamp}.tar.zst"
        (tmp_path / name).write_bytes(b"backup-bytes")

        result = evaluate_latest_archive(tmp_path, stale_hours=36, warn_hours=30)
        assert result["status"] == "critical"
        assert any(code == "CHECKSUM_MISSING" for _, code, _ in result["issues"])

    def test_invalid_checksum(self, tmp_path: Path):
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        name = f"raven-recovery-{stamp}.tar.zst"
        archive = tmp_path / name
        archive.write_bytes(b"backup-bytes")
        Path(f"{archive}.sha256").write_text(
            f"{'b' * 64}  {name}\n",
            encoding="utf-8",
        )

        with patch("canary.pelican_backup._compute_sha256", return_value="c" * 64):
            result = evaluate_latest_archive(tmp_path, stale_hours=36, warn_hours=30)

        assert result["status"] == "critical"
        assert any(code == "CHECKSUM_INVALID" for _, code, _ in result["issues"])

    def test_healthy_backup(self, tmp_path: Path):
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        name = f"raven-recovery-{stamp}.tar.zst"
        archive = tmp_path / name
        archive.write_bytes(b"backup-bytes")
        digest = "d" * 64
        Path(f"{archive}.sha256").write_text(f"{digest}  {name}\n", encoding="utf-8")

        with patch("canary.pelican_backup._compute_sha256", return_value=digest):
            result = evaluate_latest_archive(tmp_path, stale_hours=36, warn_hours=30)

        assert result["status"] == "ok"
        assert result["checksum_valid"] is True
        assert result["issues"] == []

    def test_find_latest_completed_archive_ignores_staging(self, tmp_path: Path):
        old = "raven-recovery-20260619T030015Z.tar.zst"
        new = "raven-recovery-20260620T030015Z.tar.zst"
        (tmp_path / old).write_bytes(b"1")
        (tmp_path / new).write_bytes(b"2")
        (tmp_path / "raven-recovery-20260621T030015Z.incomplete").mkdir()

        latest = find_latest_completed_archive(tmp_path)
        assert latest is not None
        assert latest.name == new

    def test_secret_non_disclosure_in_alert_messages(self, tmp_path: Path):
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        name = f"raven-recovery-{stamp}.tar.zst"
        archive = tmp_path / name
        archive.write_bytes(b"backup-bytes")
        manifest = tmp_path / f"{name}.manifest"
        manifest.write_text("discord_token=super-secret-value\n", encoding="utf-8")

        check = _healthy_pelican_check()
        check["status"] = "critical"
        check["issue_codes"] = ["CHECKSUM_MISSING"]
        check["alerts"] = [
            {
                "severity": "critical",
                "category": "pelican_backup",
                "code": "CHECKSUM_MISSING",
                "volume": "pelican",
                "mount_path": str(tmp_path),
                "message": f"Missing checksum sidecar for {name}",
            }
        ]

        decision = decide_pelican_alert(check, {"severity": "healthy", "fingerprint": "healthy"})
        assert "super-secret-value" not in decision.message
        assert "discord_token" not in decision.message
        assert ".env" not in decision.message


class TestPelicanAlertDedup:
    def test_duplicate_alert_suppression(self):
        unhealthy = _healthy_pelican_check()
        unhealthy["status"] = "critical"
        unhealthy["issue_codes"] = ["BACKUP_STALE"]
        unhealthy["alerts"] = [
            {
                "severity": "critical",
                "category": "pelican_backup",
                "code": "BACKUP_STALE",
                "volume": "pelican",
                "mount_path": "/mnt/storage/pelican_backup",
                "message": "Latest backup is 40.0h old (threshold 36h)",
            }
        ]

        first = decide_pelican_alert(unhealthy, {"severity": "healthy", "fingerprint": "healthy"})
        assert first.should_send is True
        assert first.kind == "alert"

        second = decide_pelican_alert(
            unhealthy,
            {"severity": "critical", "fingerprint": "BACKUP_STALE"},
        )
        assert second.should_send is False
        assert second.kind == "none"

    def test_recovery_alert(self):
        healthy = _healthy_pelican_check()
        decision = decide_pelican_alert(
            healthy,
            {"severity": "critical", "fingerprint": "BACKUP_STALE"},
        )
        assert decision.should_send is True
        assert decision.kind == "recovery"
        assert "RECOVERED" in decision.message

    def test_warning_to_critical_sends_alert(self):
        warning = _healthy_pelican_check()
        warning["status"] = "warning"
        warning["issue_codes"] = ["BACKUP_APPROACHING_STALE"]
        warning["alerts"] = [
            {
                "severity": "warning",
                "category": "pelican_backup",
                "code": "BACKUP_APPROACHING_STALE",
                "volume": "pelican",
                "mount_path": "/mnt/storage/pelican_backup",
                "message": "approaching stale",
            }
        ]

        critical = _healthy_pelican_check()
        critical["status"] = "critical"
        critical["issue_codes"] = ["BACKUP_STALE"]
        critical["alerts"] = [
            {
                "severity": "critical",
                "category": "pelican_backup",
                "code": "BACKUP_STALE",
                "volume": "pelican",
                "mount_path": "/mnt/storage/pelican_backup",
                "message": "stale",
            }
        ]

        escalation = decide_pelican_alert(
            critical,
            {"severity": "warning", "fingerprint": "BACKUP_APPROACHING_STALE"},
        )
        assert escalation.should_send is True
        assert escalation.kind == "alert"

    def test_process_pelican_alerts_persists_state(self, tmp_path: Path):
        unhealthy = _healthy_pelican_check()
        unhealthy["status"] = "critical"
        unhealthy["issue_codes"] = ["TIMER_DISABLED"]
        unhealthy["alerts"] = [
            {
                "severity": "critical",
                "category": "pelican_backup",
                "code": "TIMER_DISABLED",
                "volume": "pelican",
                "mount_path": "/mnt/storage/pelican_backup",
                "message": "timer disabled",
            }
        ]

        state_path = tmp_path / "canary_alert_state.json"
        sent_urls: list[str] = []

        def fake_send(url: str, content: str) -> bool:  # noqa: ARG001
            sent_urls.append(url)
            return True

        with patch("canary.alerting.send_discord_message", side_effect=fake_send):
            first = process_pelican_alerts(
                unhealthy,
                host="raven",
                state_path=state_path,
                webhook_url="https://example.test/webhook",
            )
            second = process_pelican_alerts(
                unhealthy,
                host="raven",
                state_path=state_path,
                webhook_url="https://example.test/webhook",
            )

        assert first["sent"] is True
        assert second["sent"] is False
        assert len(sent_urls) == 1
        saved = json.loads(state_path.read_text(encoding="utf-8"))
        assert saved["pelican_backup"]["severity"] == "critical"
        assert saved["pelican_backup"]["fingerprint"] == "TIMER_DISABLED"


class TestPelicanIntegration:
    def setup_method(self) -> None:
        set_command_runner(None)

    def teardown_method(self) -> None:
        set_command_runner(None)

    def test_check_pelican_backup_end_to_end_healthy(self, tmp_path: Path):
        target = tmp_path / "pelican_backup"
        target.mkdir()
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        name = f"raven-recovery-{stamp}.tar.zst"
        archive = target / name
        archive.write_bytes(b"ok")
        digest = "e" * 64
        Path(f"{archive}.sha256").write_text(f"{digest}  {name}\n", encoding="utf-8")

        future_usec = int((datetime.now(timezone.utc) + timedelta(hours=8)).timestamp() * 1_000_000)

        def runner(args, timeout):  # noqa: ARG001
            if args[0] == "systemctl":
                prop = _systemctl_prop(args)
                if "pelican-backup.timer" in args:
                    if prop == "UnitFileState":
                        return True, "enabled"
                    if prop == "ActiveState":
                        return True, "active"
                    if prop == "NextElapseUSecRealtime":
                        return True, str(future_usec)
                if "pelican-backup.service" in args:
                    if prop == "ActiveState":
                        return True, "inactive"
                    if prop == "Result":
                        return True, "success"
                    if prop == "ExecMainStatus":
                        return True, "0"
                    if prop == "ExecMainStartTimestamp":
                        return True, "Sat 2026-06-20 03:00:15 CDT"
            if args[0] == "findmnt" and str(target) in args:
                return True, "/dev/sdb1 ext4\n"
            if args[0] == "findmnt" and args[-1] == "/":
                return True, "/dev/nvme0n1p2\n"
            return False, f"unexpected {args}"

        set_command_runner(runner)
        with (
            patch("canary.pelican_backup.config.PELICAN_BACKUP_TARGET", str(target)),
            patch("canary.pelican_backup.host_path", side_effect=lambda p: p),
            patch("canary.pelican_backup.path_access_check", return_value=(True, None)),
            patch("canary.pelican_backup._compute_sha256", return_value=digest),
        ):
            result = check_pelican_backup()

        assert result["status"] == "ok"
        assert result["archive"]["checksum_valid"] is True
