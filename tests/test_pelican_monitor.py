"""
Tests for Pelican backup monitor — registry, runner, alerts, and Pelican checks.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from canary.alerting import decide_backup_alert, process_backup_alerts
from canary.checks import check_backup_monitor, run_all_checks
from pelican_monitor.checkers import raven_recovery
from pelican_monitor.definitions import BackupDefinition, enabled_backup_definitions, registered_backup_definitions
from pelican_monitor.results import BackupCheckResult, checker_error_result, combine_status
from pelican_monitor.config import resolve_discord_webhook_url
from pelican_monitor.runner import run_monitor
from pelican_monitor.subprocess_util import set_command_runner
from pelican_monitor.timer_parse import parse_next_elapse_realtime
from zoneinfo import ZoneInfo


def _systemctl_prop(args: list[str]) -> str | None:
    for arg in args:
        if arg.startswith("--property="):
            return arg.split("=", 1)[1]
    return None


def _healthy_result() -> dict:
    return {
        "backup_id": "raven_recovery",
        "display_name": "Pelican backup",
        "status": "ok",
        "reason": "Backup healthy",
        "checked_at": "2026-06-20T12:00:00-05:00",
        "newest_backup_timestamp": "2026-06-20T03:00:15+00:00",
        "backup_age_hours": 12.0,
        "warn_threshold_hours": 30.0,
        "critical_threshold_hours": 36.0,
        "target_available": True,
        "checksum_status": "ok",
        "timer": {"unit": "pelican-backup.timer", "enabled": "enabled", "active": "active", "next_run": "..."},
        "service": {"unit": "pelican-backup.service", "active": "inactive", "result": "success", "exec_main_status": 0},
        "issue_codes": [],
        "details": {"archive": {"latest_name": "raven-recovery-20260620T030015Z.tar.zst"}},
    }


class TestTimerNextRunParsing:
    CHICAGO = ZoneInfo("America/Chicago")

    def test_valid_future_realtime_value(self):
        raw = "Tue 2026-06-23 03:02:08 CDT"
        now = datetime(2026, 6, 22, 12, 0, 0, tzinfo=self.CHICAGO)
        has_future, next_run = parse_next_elapse_realtime(raw, local_tz=self.CHICAGO, now=now)
        assert has_future is True
        assert next_run is not None
        assert "2026-06-23" in next_run

    def test_raven_observed_output_regression(self):
        """Exact NextElapseUSecRealtime observed on Raven (2026-06-22)."""
        raw = "Tue 2026-06-23 03:02:08 CDT"
        now = datetime(2026, 6, 22, 15, 0, 0, tzinfo=self.CHICAGO)
        has_future, next_run = parse_next_elapse_realtime(raw, local_tz=self.CHICAGO, now=now)
        assert has_future is True
        assert next_run == "2026-06-23T03:02:08-05:00"

    def test_na_value(self):
        has_future, next_run = parse_next_elapse_realtime("n/a", local_tz=self.CHICAGO)
        assert has_future is False
        assert next_run is None

    def test_empty_value(self):
        has_future, next_run = parse_next_elapse_realtime("", local_tz=self.CHICAGO)
        assert has_future is False
        assert next_run is None

    def test_zero_value(self):
        has_future, next_run = parse_next_elapse_realtime("0", local_tz=self.CHICAGO)
        assert has_future is False
        assert next_run is None

    def test_past_timestamp(self):
        raw = "Tue 2026-06-23 03:02:08 CDT"
        now = datetime(2026, 6, 24, 12, 0, 0, tzinfo=self.CHICAGO)
        has_future, next_run = parse_next_elapse_realtime(raw, local_tz=self.CHICAGO, now=now)
        assert has_future is False
        assert next_run is not None

    def test_timezone_abbreviation_present(self):
        raw = "Tue 2026-06-23 03:02:08 CDT"
        has_future, next_run = parse_next_elapse_realtime(
            raw,
            local_tz=self.CHICAGO,
            now=datetime(2026, 6, 22, 0, 0, 0, tzinfo=self.CHICAGO),
        )
        assert has_future is True
        assert next_run is not None

    def test_integer_microseconds_still_supported(self):
        future_usec = int(datetime(2026, 6, 23, 8, 2, 8, tzinfo=timezone.utc).timestamp() * 1_000_000)
        now = datetime(2026, 6, 22, 12, 0, 0, tzinfo=timezone.utc)
        has_future, next_run = parse_next_elapse_realtime(
            str(future_usec),
            local_tz=self.CHICAGO,
            now=now,
        )
        assert has_future is True
        assert next_run is not None

    def test_evaluate_timer_healthy_with_raven_realtime_and_monotonic_zero(self):
        """NextElapseUSecMonotonic=0 must not affect health when realtime is valid."""
        raven_realtime = "Tue 2026-06-23 03:02:08 CDT"

        def runner(args, timeout):  # noqa: ARG001
            if args[0] == "systemctl" and "pelican-backup.timer" in args:
                prop = _systemctl_prop(args)
                if prop == "UnitFileState":
                    return True, "enabled"
                if prop == "ActiveState":
                    return True, "active"
                if prop == "NextElapseUSecRealtime":
                    return True, raven_realtime
            return False, "unexpected"

        set_command_runner(runner)
        with patch("pelican_monitor.checkers.raven_recovery.config.DISPLAY_TIMEZONE", "America/Chicago"):
            result = raven_recovery.evaluate_timer_health("pelican-backup.timer")

        assert result["status"] == "ok"
        assert result["has_future_run"] is True
        assert result["next_run"] == "2026-06-23T03:02:08-05:00"
        assert not any(code == "TIMER_NO_FUTURE_RUN" for _, code, _ in result["issues"])


class TestRavenRecoveryChecker:
    def setup_method(self) -> None:
        set_command_runner(None)

    def teardown_method(self) -> None:
        set_command_runner(None)

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
        result = raven_recovery.evaluate_timer_health("pelican-backup.timer")
        assert result["status"] == "critical"
        assert any(code == "TIMER_DISABLED" for _, code, _ in result["issues"])

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
        result = raven_recovery.evaluate_timer_health("pelican-backup.timer")
        assert any(code == "TIMER_NO_FUTURE_RUN" for _, code, _ in result["issues"])

    def test_oneshot_inactive_after_success_is_healthy(self):
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
        result = raven_recovery.evaluate_service_result("pelican-backup.service")
        assert result["status"] == "ok"
        assert result["inactive_between_runs_ok"] is True

    def test_service_failure(self):
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
        result = raven_recovery.evaluate_service_result("pelican-backup.service")
        assert any(code == "SERVICE_LAST_RUN_FAILED" for _, code, _ in result["issues"])

    def test_real_mount_versus_autofs(self, tmp_path: Path):
        mountpoint = str(tmp_path / "pelican_backup")
        Path(mountpoint).mkdir()

        def autofs_runner(args, timeout):  # noqa: ARG001
            if args[0] == "findmnt" and mountpoint in args:
                return True, "systemd-1 autofs\n"
            return False, "unexpected"

        set_command_runner(autofs_runner)
        with (
            patch("pelican_monitor.checkers.raven_recovery.host_path", side_effect=lambda p: p),
            patch("pelican_monitor.checkers.raven_recovery.path_access_check", return_value=(True, None)),
        ):
            result = raven_recovery.evaluate_backup_target_mount(mountpoint)
        assert any(code == "MOUNT_AUTOFS_PLACEHOLDER" for _, code, _ in result["issues"])

    def test_no_completed_archive(self, tmp_path: Path):
        (tmp_path / "raven-recovery-20260620T030015Z.incomplete").mkdir()
        result = raven_recovery.evaluate_latest_archive(tmp_path, stale_hours=36, warn_hours=30)
        assert any(code == "NO_COMPLETED_ARCHIVE" for _, code, _ in result["issues"])

    def test_stale_warning(self, tmp_path: Path):
        stamp = (datetime.now(timezone.utc) - timedelta(hours=31)).strftime("%Y%m%dT%H%M%SZ")
        name = f"raven-recovery-{stamp}.tar.zst"
        archive = tmp_path / name
        archive.write_bytes(b"x")
        Path(f"{archive}.sha256").write_text(f"{'a' * 64}  {name}\n", encoding="utf-8")
        with patch("pelican_monitor.checkers.raven_recovery._compute_sha256", return_value="a" * 64):
            result = raven_recovery.evaluate_latest_archive(tmp_path, stale_hours=36, warn_hours=30)
        assert result["status"] == "warning"
        assert any(code == "BACKUP_APPROACHING_STALE" for _, code, _ in result["issues"])

    def test_stale_critical(self, tmp_path: Path):
        stamp = (datetime.now(timezone.utc) - timedelta(hours=40)).strftime("%Y%m%dT%H%M%SZ")
        name = f"raven-recovery-{stamp}.tar.zst"
        archive = tmp_path / name
        archive.write_bytes(b"x")
        Path(f"{archive}.sha256").write_text(f"{'a' * 64}  {name}\n", encoding="utf-8")
        with patch("pelican_monitor.checkers.raven_recovery._compute_sha256", return_value="a" * 64):
            result = raven_recovery.evaluate_latest_archive(tmp_path, stale_hours=36, warn_hours=30)
        assert result["status"] == "critical"
        assert any(code == "BACKUP_STALE" for _, code, _ in result["issues"])

    def test_missing_checksum(self, tmp_path: Path):
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        name = f"raven-recovery-{stamp}.tar.zst"
        (tmp_path / name).write_bytes(b"x")
        result = raven_recovery.evaluate_latest_archive(tmp_path, stale_hours=36, warn_hours=30)
        assert any(code == "CHECKSUM_MISSING" for _, code, _ in result["issues"])

    def test_invalid_checksum(self, tmp_path: Path):
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        name = f"raven-recovery-{stamp}.tar.zst"
        archive = tmp_path / name
        archive.write_bytes(b"x")
        Path(f"{archive}.sha256").write_text(f"{'b' * 64}  {name}\n", encoding="utf-8")
        with patch("pelican_monitor.checkers.raven_recovery._compute_sha256", return_value="c" * 64):
            result = raven_recovery.evaluate_latest_archive(tmp_path, stale_hours=36, warn_hours=30)
        assert any(code == "CHECKSUM_INVALID" for _, code, _ in result["issues"])

    def test_healthy_backup(self, tmp_path: Path):
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        name = f"raven-recovery-{stamp}.tar.zst"
        archive = tmp_path / name
        archive.write_bytes(b"x")
        digest = "d" * 64
        Path(f"{archive}.sha256").write_text(f"{digest}  {name}\n", encoding="utf-8")
        with patch("pelican_monitor.checkers.raven_recovery._compute_sha256", return_value=digest):
            result = raven_recovery.evaluate_latest_archive(tmp_path, stale_hours=36, warn_hours=30)
        assert result["status"] == "ok"


class TestWebhookConfig:
    def test_discord_webhook_url_fallback(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv("PELICAN_MONITOR_DISCORD_WEBHOOK_URL", raising=False)
        monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://example.test/discord-fallback")
        assert resolve_discord_webhook_url() == "https://example.test/discord-fallback"

    def test_pelican_webhook_overrides_generic(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://example.test/discord-fallback")
        monkeypatch.setenv("PELICAN_MONITOR_DISCORD_WEBHOOK_URL", "https://example.test/pelican-specific")
        assert resolve_discord_webhook_url() == "https://example.test/pelican-specific"

    def test_webhook_not_written_to_status_or_logs(self, tmp_path: Path, caplog):
        secret_url = "https://discord.com/api/webhooks/1234567890/abcdefghijklmnop"
        unhealthy = BackupCheckResult(
            backup_id="raven_recovery",
            display_name="Pelican backup",
            status="critical",
            reason="timer disabled",
            checked_at="now",
            issue_codes=["TIMER_DISABLED"],
        )

        defn = BackupDefinition(
            backup_id="raven_recovery",
            display_name="Pelican backup",
            enabled=True,
            checker=lambda: unhealthy,
            target_path="/tmp",
            archive_pattern=None,
            warn_threshold_hours=30,
            critical_threshold_hours=36,
            checksum_expected=False,
        )

        with (
            patch("pelican_monitor.runner.config.STATUS_PATH", tmp_path / "status.json"),
            patch("pelican_monitor.runner.config.ALERT_STATE_PATH", tmp_path / "alert_state.json"),
            patch("pelican_monitor.runner.config.DISCORD_WEBHOOK_URL", secret_url),
            patch("pelican_monitor.runner.process_backup_alerts") as mock_alerts,
        ):
            mock_alerts.return_value = [{"backup_id": "raven_recovery", "sent": True, "decision": "alert"}]
            payload, _ = run_monitor(definitions=[defn], send_alerts=True)

        status_text = (tmp_path / "status.json").read_text(encoding="utf-8")
        assert secret_url not in status_text
        assert "webhooks/" not in status_text
        assert secret_url not in caplog.text
        assert secret_url not in json.dumps(payload)
        mock_alerts.assert_called_once()
        assert mock_alerts.call_args.kwargs["webhook_url"] == secret_url


class TestRegistryAndRunner:
    def test_registry_includes_raven_recovery(self):
        defs = registered_backup_definitions()
        assert any(d.backup_id == "raven_recovery" for d in defs)

    def test_disabled_backup_skipped(self, tmp_path: Path):
        with patch("pelican_monitor.runner.config.STATUS_PATH", tmp_path / "status.json"), patch(
            "pelican_monitor.runner.process_backup_alerts",
            return_value=[],
        ):
            payload, exit_code = run_monitor(definitions=[], send_alerts=False)
        assert exit_code == 0
        assert payload["backups"] == {}
        assert payload["enabled_backups"] == []

    def test_one_failing_checker_does_not_block_others(self, tmp_path: Path):
        ok = BackupCheckResult(
            backup_id="ok_backup",
            display_name="OK backup",
            status="ok",
            reason="fine",
            checked_at="now",
        )
        bad = BackupCheckResult(
            backup_id="bad_backup",
            display_name="Bad backup",
            status="critical",
            reason="bad",
            checked_at="now",
        )

        def ok_checker() -> BackupCheckResult:
            return ok

        def bad_checker() -> BackupCheckResult:
            return bad

        defs = [
            BackupDefinition(
                backup_id="ok_backup",
                display_name="OK backup",
                enabled=True,
                checker=ok_checker,
                target_path="/tmp",
                archive_pattern=None,
                warn_threshold_hours=30,
                critical_threshold_hours=36,
                checksum_expected=False,
            ),
            BackupDefinition(
                backup_id="bad_backup",
                display_name="Bad backup",
                enabled=True,
                checker=bad_checker,
                target_path="/tmp",
                archive_pattern=None,
                warn_threshold_hours=30,
                critical_threshold_hours=36,
                checksum_expected=False,
            ),
        ]

        with (
            patch("pelican_monitor.runner.config.STATUS_PATH", tmp_path / "status.json"),
            patch("pelican_monitor.runner.process_backup_alerts", return_value=[]),
        ):
            payload, exit_code = run_monitor(definitions=defs, send_alerts=False)

        assert exit_code == 1
        assert payload["backups"]["ok_backup"]["status"] == "ok"
        assert payload["backups"]["bad_backup"]["status"] == "critical"

    def test_checker_exception_becomes_error_not_crash(self, tmp_path: Path):
        def boom() -> BackupCheckResult:
            raise RuntimeError("checker exploded")

        defn = BackupDefinition(
            backup_id="boom",
            display_name="Boom",
            enabled=True,
            checker=boom,
            target_path="/tmp",
            archive_pattern=None,
            warn_threshold_hours=30,
            critical_threshold_hours=36,
            checksum_expected=False,
        )

        with (
            patch("pelican_monitor.runner.config.STATUS_PATH", tmp_path / "status.json"),
            patch("pelican_monitor.runner.process_backup_alerts", return_value=[]),
        ):
            payload, exit_code = run_monitor(definitions=[defn], send_alerts=False)

        assert exit_code == 1
        assert payload["backups"]["boom"]["status"] == "error"
        assert payload["backups"]["boom"]["issue_codes"] == ["CHECKER_ERROR"]

    def test_aggregate_status_includes_all_enabled(self, tmp_path: Path):
        defn = enabled_backup_definitions()[0]

        def mock_checker() -> BackupCheckResult:
            return BackupCheckResult(
                backup_id="raven_recovery",
                display_name="Pelican backup",
                status="ok",
                reason="healthy",
                checked_at="now",
            )

        custom = BackupDefinition(
            backup_id=defn.backup_id,
            display_name=defn.display_name,
            enabled=True,
            checker=mock_checker,
            target_path=defn.target_path,
            archive_pattern=defn.archive_pattern,
            warn_threshold_hours=defn.warn_threshold_hours,
            critical_threshold_hours=defn.critical_threshold_hours,
            checksum_expected=defn.checksum_expected,
            timer_unit=defn.timer_unit,
            service_unit=defn.service_unit,
        )

        with patch("pelican_monitor.runner.config.STATUS_PATH", tmp_path / "status.json"), patch(
            "pelican_monitor.runner.process_backup_alerts",
            return_value=[],
        ):
            payload, exit_code = run_monitor(definitions=[custom], send_alerts=False)

        assert exit_code == 0
        assert "raven_recovery" in payload["backups"]
        assert payload["enabled_backups"] == ["raven_recovery"]


class TestAlerting:
    def test_duplicate_suppression_per_backup_id(self):
        unhealthy = _healthy_result()
        unhealthy["status"] = "critical"
        unhealthy["issue_codes"] = ["BACKUP_STALE"]
        unhealthy["reason"] = "Latest recovery bundle is 40 hours old."

        first = decide_backup_alert(unhealthy, {"severity": "healthy", "fingerprint": "healthy"})
        second = decide_backup_alert(unhealthy, {"severity": "critical", "fingerprint": "BACKUP_STALE"})
        assert first.should_send is True
        assert second.should_send is False

    def test_recovery_alert_per_backup_id(self):
        healthy = _healthy_result()
        decision = decide_backup_alert(healthy, {"severity": "critical", "fingerprint": "BACKUP_STALE"})
        assert decision.should_send is True
        assert decision.kind == "recovery"
        assert "RECOVERED" in decision.message

    def test_generic_alert_formatting(self):
        unhealthy = _healthy_result()
        unhealthy["status"] = "critical"
        unhealthy["issue_codes"] = ["BACKUP_STALE"]
        unhealthy["reason"] = "Latest recovery bundle is 40 hours old."
        decision = decide_backup_alert(unhealthy, {"severity": "healthy", "fingerprint": "healthy"})
        assert "**Raven / Pelican backup CRITICAL**" in decision.message
        assert "40 hours old" in decision.message

    def test_process_backup_alerts_persists_by_backup_id(self, tmp_path: Path):
        unhealthy = _healthy_result()
        unhealthy["status"] = "critical"
        unhealthy["issue_codes"] = ["TIMER_DISABLED"]
        unhealthy["reason"] = "timer disabled"

        state_path = tmp_path / "alert_state.json"
        with patch("canary.alerting.send_discord_message", return_value=True):
            first = process_backup_alerts(
                {"raven_recovery": unhealthy},
                host="raven",
                state_path=state_path,
                webhook_url="https://example.test/webhook",
            )
            second = process_backup_alerts(
                {"raven_recovery": unhealthy},
                host="raven",
                state_path=state_path,
                webhook_url="https://example.test/webhook",
            )

        assert first[0]["sent"] is True
        assert second[0]["sent"] is False
        saved = json.loads(state_path.read_text(encoding="utf-8"))
        assert "raven_recovery" in saved["backups"]

    def test_secret_non_disclosure(self, tmp_path: Path):
        unhealthy = _healthy_result()
        unhealthy["status"] = "critical"
        unhealthy["reason"] = "Missing checksum sidecar"
        unhealthy["details"] = {"archive": {"manifest": "discord_token=secret-value"}}
        decision = decide_backup_alert(unhealthy, {"severity": "healthy", "fingerprint": "healthy"})
        assert "secret-value" not in decision.message
        assert "discord_token" not in decision.message


class TestCanaryIntegration:
    def setup_method(self) -> None:
        set_command_runner(None)

    def teardown_method(self) -> None:
        set_command_runner(None)

    def test_canary_reads_snapshot_without_running_checks(self, tmp_path: Path):
        snapshot = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "overall_status": "ok",
            "enabled_backups": ["raven_recovery"],
            "backups": {"raven_recovery": _healthy_result()},
        }
        status_path = tmp_path / "backup_monitor_status.json"
        status_path.write_text(json.dumps(snapshot), encoding="utf-8")

        with patch("canary.checks.config.BACKUP_MONITOR_STATUS_PATH", status_path):
            result = check_backup_monitor()

        assert result["status"] == "ok"
        assert result["backups"]["raven_recovery"]["display_name"] == "Pelican backup"
        assert "snapshot" in result

    def test_normal_canary_cycle_does_not_run_backup_checks(self, tmp_path: Path):
        backup_check_called = {"count": 0}

        def spy_check():
            backup_check_called["count"] += 1
            return BackupCheckResult(
                backup_id="raven_recovery",
                display_name="Pelican backup",
                status="ok",
                reason="should not run",
                checked_at="now",
            )

        status_path = tmp_path / "backup_monitor_status.json"
        status_path.write_text(
            json.dumps({"generated_at": datetime.now(timezone.utc).isoformat(), "overall_status": "ok", "backups": {}}),
            encoding="utf-8",
        )

        def failing_runner(args, timeout):  # noqa: ARG001
            return False, f"unavailable: {args[0]}"

        set_command_runner(failing_runner)

        with (
            patch("canary.checks.config.BACKUP_MONITOR_STATUS_PATH", status_path),
            patch("canary.checks.config.LOGS_DIR", tmp_path / "logs"),
            patch("canary.checks.config.HOST_ROOT", Path("/")),
            patch("canary.storage.config.FSTAB_PATH", tmp_path / "fstab"),
            patch("canary.storage._read_fstab", return_value={}),
            patch("pelican_monitor.checkers.raven_recovery.check_raven_recovery", side_effect=spy_check),
        ):
            payload = run_all_checks()

        assert backup_check_called["count"] == 0
        assert "backup_monitor" in payload["checks"]
        assert "pelican_backup" not in payload["checks"]
