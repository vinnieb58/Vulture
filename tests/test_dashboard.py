"""
tests/test_dashboard.py

Lightweight tests for Vulture Dashboard v0.2 (read-only FastAPI app).
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

DASHBOARD_DIR = Path(__file__).resolve().parent.parent / "dashboard"
sys.path.insert(0, str(DASHBOARD_DIR))

from parsers import (  # noqa: E402
    parse_container_names,
    parse_df_human,
    parse_docker_ps_format,
    parse_findmnt_line,
    parse_free_human,
    parse_loadavg,
    parse_mountinfo,
    parse_systemctl_failed,
    pick_lan_ipv4,
)
import app as dashboard_app  # noqa: E402
from db_readers import read_db_snapshot  # noqa: E402
from host_status import (  # noqa: E402
    ServiceStatus,
    _check_service,
    get_docker_snapshot,
    get_raven_health,
    get_storage_status,
)
from log_readers import read_log_snapshot  # noqa: E402
from vulture_runtime import (  # noqa: E402
    _evaluate_scheduler_health,
    _format_runtime_detail,
    _parse_log_timestamp,
    _parse_timer_datetime,
    _parse_timer_schedule,
    _scheduler_freshness,
)
from host_status import ServiceStatus  # noqa: E402


SAMPLE_DF = """\
Filesystem      Size  Used Avail Use% Mounted on
/dev/mmcblk0p2   58G   18G   38G  32% /host/root
/dev/sda1       128G   40G   82G  33% /mnt/storage/portable_beast
"""

SAMPLE_DOCKER_PS = """\
vulture-dashboard\tUp 2 hours\t0.0.0.0:8088->8088/tcp
portainer\tUp 3 days\t0.0.0.0:9443->9443/tcp
"""

SAMPLE_SYSTEMCTL_FAILED = """\
UNIT               LOAD   ACTIVE SUB    DESCRIPTION
failed.service     loaded failed failed Example failed unit

1 loaded units listed.
"""

SAMPLE_FREE = """\
              total        used        free      shared  buff/cache   available
Mem:           7.6Gi       2.1Gi       3.2Gi       120Mi       2.3Gi       5.2Gi
Swap:             0B          0B          0B
"""

SAMPLE_IP = """\
lo               UNKNOWN        127.0.0.1/8 ::1/128
eth0             UP             192.168.1.143/24
tailscale0       UNKNOWN        100.82.1.18/32
"""


class TestParsers:
    def test_parse_df_human(self):
        entries = parse_df_human(SAMPLE_DF)
        by_mount = {e.mount: e for e in entries}
        assert by_mount["/host/root"].percent_used == 32.0
        assert by_mount["/mnt/storage/portable_beast"].size == "128G"

    def test_parse_docker_ps_format(self):
        rows = parse_docker_ps_format(SAMPLE_DOCKER_PS)
        assert len(rows) == 2
        assert rows[0].name == "vulture-dashboard"
        assert "8088" in rows[0].ports

    def test_parse_container_names(self):
        text = "alpha\nbeta\n"
        assert parse_container_names(text) == ["alpha", "beta"]

    def test_parse_systemctl_failed(self):
        units = parse_systemctl_failed(SAMPLE_SYSTEMCTL_FAILED)
        assert units == ["failed.service"]

    def test_parse_free_human(self):
        mem = parse_free_human(SAMPLE_FREE)
        assert mem is not None
        assert mem.total == "7.6Gi"
        assert mem.used == "2.1Gi"

    def test_parse_loadavg(self):
        assert parse_loadavg("0.52 0.48 0.41 2/341 999") == "0.52 / 0.48 / 0.41 (1/5/15 min)"

    def test_pick_lan_ipv4(self):
        assert pick_lan_ipv4(SAMPLE_IP) == "192.168.1.143"

    def test_parse_mountinfo(self):
        text = (
            "24 1 8:2 / / rw,relatime shared:1 - ext4 /dev/sda2 rw\n"
            "36 24 179:1 / /mnt/storage/microsd rw,relatime shared:2 - ext4 /dev/mmcblk0p1 rw\n"
        )
        mounts = parse_mountinfo(text)
        assert mounts["/"] == ("/dev/sda2", "ext4")
        assert mounts["/mnt/storage/microsd"] == ("/dev/mmcblk0p1", "ext4")

    def test_parse_findmnt_line(self):
        assert parse_findmnt_line("/dev/mmcblk0p1 ext4 ff481ad2-e9bd-4868-8c8c-6729a461e4b4") == (
            "/dev/mmcblk0p1",
            "ext4",
            "ff481ad2-e9bd-4868-8c8c-6729a461e4b4",
        )


class TestDashboardHTTP:
    @pytest.fixture
    def client(self, tmp_path, monkeypatch):
        db_path = tmp_path / "missing.db"
        log_path = tmp_path / "missing.log"
        monkeypatch.setattr(dashboard_app, "DB_PATH", db_path)
        monkeypatch.setattr(dashboard_app, "LOG_PATH", log_path)
        monkeypatch.setattr("db_readers.DB_PATH", db_path)
        monkeypatch.setattr("log_readers.LOG_PATH", log_path)
        monkeypatch.setattr("vulture_runtime.LOG_PATH", log_path)
        return TestClient(dashboard_app.app)

    def _stub_host(self, client, *, active="unavailable", systemctl="unavailable"):
        """Helper: GET / with host commands stubbed out."""
        with patch("host_status.run_host_command", return_value=(False, active)):
            with patch("host_status.run_systemctl", return_value=(False, systemctl)):
                return client.get("/")

    # ── Nest Overview (/) ────────────────────────────────────────────

    def test_nest_home_missing_db_and_log_returns_200(self, client):
        response = self._stub_host(client)
        assert response.status_code == 200
        assert "Nest" in response.text
        assert "read-only" not in response.text  # overview has no "read-only" badge

    def test_nest_home_shows_raven_health_card(self, client):
        response = self._stub_host(client)
        assert response.status_code == 200
        assert "Raven Health" in response.text

    def test_nest_home_shows_storage_card(self, client):
        response = self._stub_host(client)
        assert response.status_code == 200
        assert "Storage" in response.text

    def test_nest_home_shows_vulture_card(self, client):
        response = self._stub_host(client)
        assert response.status_code == 200
        assert "Vulture" in response.text

    def test_nest_home_shows_network_card(self, client):
        response = self._stub_host(client)
        assert response.status_code == 200
        assert "Network" in response.text

    def test_nest_home_shows_navigation(self, client):
        response = self._stub_host(client)
        assert response.status_code == 200
        assert "/storage" in response.text
        assert "/vulture" in response.text
        assert "/advanced" in response.text

    # ── Advanced Ops (/advanced) ──────────────────────────────────

    def test_advanced_missing_db_and_log_returns_200(self, client):
        with patch("host_status.run_host_command", return_value=(False, "unavailable")):
            with patch("host_status.run_systemctl", return_value=(False, "unavailable")):
                response = client.get("/advanced")
        assert response.status_code == 200
        assert "Raven Ops" in response.text
        assert "read-only" in response.text

    def test_advanced_when_commands_fail_returns_200(self, client):
        with patch("host_status.run_host_command", return_value=(False, "timed out")):
            with patch("host_status.run_systemctl", return_value=(False, "timed out")):
                response = client.get("/advanced")
        assert response.status_code == 200
        assert "Raven Health" in response.text

    def test_advanced_when_docker_unavailable_returns_200(self, client):
        with patch("host_status.run_host_command", return_value=(False, "cannot connect")):
            with patch("host_status.systemctl_is_active", return_value=(True, "active")):
                with patch("host_status.systemctl_is_enabled", return_value=(True, "enabled")):
                    with patch("host_status.systemctl_unit_exists", return_value=True):
                        docker = get_docker_snapshot()
        assert docker.warning is not None
        with patch("host_status.run_host_command", return_value=(False, "cannot connect")):
            with patch("host_status.run_systemctl", return_value=(False, "unavailable")):
                response = client.get("/advanced")
        assert response.status_code == 200
        assert "Docker" in response.text

    # ── Storage detail (/storage) ─────────────────────────────────

    def test_storage_page_returns_200(self, client):
        with patch("host_status.run_host_command", return_value=(False, "unavailable")):
            with patch("host_status.run_systemctl", return_value=(False, "unavailable")):
                response = client.get("/storage")
        assert response.status_code == 200
        assert "Storage" in response.text

    def test_storage_page_shows_drives(self, client):
        with patch("host_status.run_host_command", return_value=(False, "unavailable")):
            with patch("host_status.run_systemctl", return_value=(False, "unavailable")):
                response = client.get("/storage")
        assert response.status_code == 200
        # Toshiba EXT and MicroSD should appear from storage_config defaults
        assert "Toshiba" in response.text or "toshiba" in response.text.lower()

    # ── Vulture detail (/vulture) ─────────────────────────────────

    def test_vulture_page_returns_200(self, client):
        response = self._stub_host(client)
        # /vulture doesn't call host_status directly, but stub ensures no issues
        with patch("host_status.run_host_command", return_value=(False, "unavailable")):
            response = client.get("/vulture")
        assert response.status_code == 200
        assert "Vulture" in response.text

    # ── Resilience: missing optional storage must not crash any page ──

    def test_missing_optional_storage_does_not_crash_home(self, client):
        """Missing optional drives (Pelican, NVME) must not crash the Nest home page."""
        with patch("storage_probe.probe_expected_drive", side_effect=RuntimeError("boom")):
            with patch("host_status.run_host_command", return_value=(False, "unavailable")):
                with patch("host_status.run_systemctl", return_value=(False, "unavailable")):
                    response = client.get("/")
        assert response.status_code == 200

    def test_missing_optional_storage_does_not_crash_storage_page(self, client):
        with patch("storage_probe.probe_expected_drive", side_effect=RuntimeError("boom")):
            response = client.get("/storage")
        assert response.status_code == 200


class TestDefensiveReaders:
    def test_read_db_snapshot_missing_db(self, tmp_path, monkeypatch):
        missing = tmp_path / "nope.db"
        monkeypatch.setattr("db_readers.DB_PATH", missing)
        snap = read_db_snapshot()
        assert snap["warning"] is not None
        assert snap["hunts"] == []

    def test_read_log_snapshot_missing_log(self, tmp_path, monkeypatch):
        missing = tmp_path / "nope.log"
        monkeypatch.setattr("log_readers.LOG_PATH", missing)
        snap = read_log_snapshot()
        assert snap["warning"] is not None
        assert snap["lines"] == []

    def test_raven_health_never_raises_on_command_failures(self):
        with patch("host_status.run_host_command", return_value=(False, "fail")):
            with patch("host_status.run_systemctl", return_value=(False, "fail")):
                health = get_raven_health()
        assert health["hostname"]
        assert isinstance(health["warnings"], list)


class TestStorageResilience:
    def test_get_storage_status_never_raises_on_probe_failure(self):
        with patch("storage_probe.probe_expected_drive", side_effect=RuntimeError("boom")):
            mounts = get_storage_status()
        assert mounts
        assert all(m.status == "ERROR" for m in mounts)


class TestDockerComposeStorageMounts:
    COMPOSE_PATH = Path(__file__).resolve().parent.parent / "docker-compose.dashboard.yml"

    def test_no_fragile_optional_drive_bind_mounts(self):
        text = self.COMPOSE_PATH.read_text(encoding="utf-8")
        fragile = (
            "/mnt/storage/microsd:/mnt/storage/microsd",
            "/mnt/storage/portable_beast:/mnt/storage/portable_beast",
            "/mnt/storage/toshiba_ext:/mnt/storage/toshiba_ext",
            "/mnt/storage/pelican_backup:/mnt/storage/pelican_backup",
            "/mnt/storage/raven_nvme:/mnt/storage/raven_nvme",
            "/mnt/storage/roost_spinning_0:/mnt/storage/roost_spinning_0",
        )
        for bind in fragile:
            assert bind not in text, f"fragile bind mount must be removed: {bind}"
        assert "/mnt/storage:/mnt/storage:ro" in text


class TestHostCommands:
    def test_service_check_uses_systemctl_states(self):
        with patch("host_status.systemctl_unit_exists", return_value=True):
            with patch("host_status.systemctl_is_active", return_value=(True, "active")):
                with patch("host_status.systemctl_is_enabled", return_value=(True, "enabled")):
                    svc = _check_service("SSH", ("ssh.service",))
        assert svc.active == "active"
        assert svc.enabled == "enabled"
        assert svc.warning is None

    def test_service_not_found_when_unit_missing(self):
        with patch("host_status.systemctl_unit_exists", return_value=False):
            svc = _check_service("vulture-bot", ("vulture-bot.service",))
        assert svc.active == "not found"
        assert svc.enabled == "not configured"

    def test_runtime_detail_hides_broken_systemctl(self):
        svc = ServiceStatus("vulture-bot", "vulture-bot.service", "unknown", "unknown")
        detail = _format_runtime_detail(svc, True, "python main.py")
        assert "command not found" not in detail
        assert "process:" in detail

    def test_scheduler_freshness_from_journal(self):
        journal = [
            "2026-06-05T21:55:07-0500 python[47497]: 2026-06-05 21:55:07,163 [INFO] Done hunt 'ddr4_desktop_ram'",
        ]
        timer_svc = ServiceStatus("vulture-scheduler timer", "vulture-scheduler.timer", "active", "enabled")
        service_svc = ServiceStatus("vulture-scheduler service", "vulture-scheduler.service", "inactive", "disabled")
        with patch("vulture_runtime._check_service", return_value=timer_svc):
            with patch("vulture_runtime._check_scheduler_service", return_value=service_svc):
                with patch("vulture_runtime._list_timer_next_run", return_value="Mon 2026-06-07 12:00:00 UTC"):
                    with patch("vulture_runtime._journal_lines", return_value=journal):
                        with patch(
                            "vulture_runtime.datetime",
                            wraps=datetime,
                        ) as mock_dt:
                            mock_dt.now.return_value = datetime(2026, 6, 6, 3, 0, 0, tzinfo=timezone.utc)
                            result = _scheduler_freshness([])
        assert result["status"] in ("fresh", "stale", "seen")
        assert "journal" in result["detail"]


class TestTimerScheduleParsing:
    SAMPLE_LIST_TIMERS = (
        "NEXT                        LEFT          LAST                        PASSED       UNIT                     ACTIVATES\n"
        "Thu 2026-06-11 17:00:00 UTC  23min left    Thu 2026-06-11 11:19:00 UTC  5h ago       vulture-scheduler.timer  vulture-scheduler.service\n"
        "Fri 2026-06-12 00:00:00 UTC  6h left       Thu 2026-06-11 00:00:00 UTC  16h ago      logrotate.timer          logrotate.service\n"
    )

    def test_next_run_keeps_full_timestamp_not_just_weekday(self):
        schedule = _parse_timer_schedule(self.SAMPLE_LIST_TIMERS, "vulture-scheduler.timer")
        assert schedule["next_run"] == "Thu 2026-06-11 17:00:00 UTC"
        assert schedule["last_run"] == "Thu 2026-06-11 11:19:00 UTC"

    def test_next_run_na_returns_none(self):
        out = (
            "NEXT  LEFT  LAST                        PASSED   UNIT                     ACTIVATES\n"
            "n/a   n/a   Thu 2026-06-11 11:19:00 UTC  5h ago   vulture-scheduler.timer  vulture-scheduler.service\n"
        )
        schedule = _parse_timer_schedule(out, "vulture-scheduler.timer")
        assert schedule["next_run"] is None

    def test_parse_timer_datetime_utc(self):
        dt = _parse_timer_datetime("Thu 2026-06-11 17:00:00 UTC")
        assert dt == datetime(2026, 6, 11, 17, 0, 0, tzinfo=timezone.utc)


class TestLogTimestampParsing:
    def test_naive_line_assumed_utc(self):
        dt = _parse_log_timestamp("2026-06-11 11:35:06,318 [WARNING] Swappa: zero model slugs")
        assert dt == datetime(2026, 6, 11, 11, 35, 6, tzinfo=timezone.utc)

    def test_journal_offset_normalised_to_utc(self):
        # journalctl short-iso prefix carries an explicit offset; it must be honoured
        # rather than treated as naive UTC (which would mis-age by the offset).
        dt = _parse_log_timestamp(
            "2026-06-05T21:55:07-0500 python[1]: 2026-06-05 21:55:07,163 [INFO] Done hunt"
        )
        assert dt == datetime(2026, 6, 6, 2, 55, 7, tzinfo=timezone.utc)


class TestNestCardComputation:
    """Unit tests for the Nest overview card summary logic in app.py."""

    def test_raven_card_ok_when_no_issues(self):
        from app import _compute_raven_card
        from host_status import DockerSnapshot
        raven = {
            "hostname": "raven",
            "uptime": "3 days",
            "failed_units": [],
            "internet_ok": True,
            "load_average": "0.2 / 0.3 / 0.4 (1/5/15 min)",
            "memory": None,
            "warnings": [],
        }
        docker = DockerSnapshot(daemon_active=True, daemon_state="active", warning=None, running_count=2, stopped_count=0)
        card = _compute_raven_card(raven, [], docker)
        assert card["status"] == "OK"
        assert "healthy" in card["headline"].lower()

    def test_raven_card_fail_when_units_failed(self):
        from app import _compute_raven_card
        from host_status import DockerSnapshot
        raven = {
            "hostname": "raven",
            "uptime": "3 days",
            "failed_units": ["myservice.service"],
            "internet_ok": True,
            "load_average": None,
            "memory": None,
            "warnings": [],
        }
        docker = DockerSnapshot(daemon_active=True, daemon_state="active", warning=None, running_count=1, stopped_count=0)
        card = _compute_raven_card(raven, [], docker)
        assert card["status"] == "FAIL"
        assert "failed" in card["headline"].lower()

    def test_raven_card_warn_when_internet_down(self):
        from app import _compute_raven_card
        from host_status import DockerSnapshot
        raven = {
            "hostname": "raven",
            "uptime": "1 day",
            "failed_units": [],
            "internet_ok": False,
            "load_average": None,
            "memory": None,
            "warnings": [],
        }
        docker = DockerSnapshot(daemon_active=True, daemon_state="active", warning=None, running_count=0, stopped_count=0)
        card = _compute_raven_card(raven, [], docker)
        assert card["status"] == "WARN"

    def test_storage_card_ok_when_all_mounted_with_low_usage(self):
        from app import _compute_storage_card
        from storage_probe import StorageStatus
        mounts = [
            StorageStatus(name="MicroSD", path="/mnt/storage/microsd", required=True,
                          status="OK", percent_used=30.0, used="30G", size="100G", available="70G"),
            StorageStatus(name="Toshiba EXT", path="/mnt/storage/toshiba_ext", required=True,
                          status="OK", percent_used=50.0, used="500G", size="1T", available="500G"),
        ]
        card = _compute_storage_card(mounts)
        assert card["status"] == "OK"
        assert "healthy" in card["headline"].lower()

    def test_storage_card_warn_when_optional_missing(self):
        from app import _compute_storage_card
        from storage_probe import StorageStatus
        mounts = [
            StorageStatus(name="MicroSD", path="/mnt/storage/microsd", required=True,
                          status="OK", percent_used=20.0),
            StorageStatus(name="Pelican Backup", path="/mnt/storage/pelican_backup", required=False,
                          status="NOT_MOUNTED"),
        ]
        card = _compute_storage_card(mounts)
        assert card["status"] == "WARN"

    def test_storage_card_fail_when_required_missing(self):
        from app import _compute_storage_card
        from storage_probe import StorageStatus
        mounts = [
            StorageStatus(name="MicroSD", path="/mnt/storage/microsd", required=True,
                          status="NOT_MOUNTED"),
        ]
        card = _compute_storage_card(mounts)
        assert card["status"] == "FAIL"

    def test_storage_card_warn_when_high_disk_usage(self):
        from app import _compute_storage_card
        from storage_probe import StorageStatus
        mounts = [
            StorageStatus(name="Toshiba EXT", path="/mnt/storage/toshiba_ext", required=True,
                          status="OK", percent_used=87.0, used="870G", size="1T", available="130G"),
        ]
        card = _compute_storage_card(mounts)
        assert card["status"] == "WARN"
        assert "87%" in card["headline"] or "Toshiba" in card["headline"]

    def test_storage_card_legacy_drives_ignored_when_missing(self):
        from app import _compute_storage_card
        from storage_probe import StorageStatus
        mounts = [
            StorageStatus(name="portable_beast", path="/mnt/storage/portable_beast",
                          required=False, legacy=True, status="NOT_MOUNTED"),
        ]
        card = _compute_storage_card(mounts)
        # Legacy unmounted drives should not push overall status to WARN
        assert card["status"] == "OK"

    def test_vulture_card_ok_when_scheduler_fresh(self):
        from app import _compute_vulture_card
        vulture = {
            "scheduler_freshness": {
                "status": "fresh",
                "detail": "timer: active · service: inactive",
                "next_run": "Mon 12:00",
                "last_success": "Mon 11:50",
            },
            "processes": [],
        }
        db = {"hunt_counts": {"active": 3, "total": 5, "paused": 1, "ended": 1}}
        card = _compute_vulture_card(vulture, db)
        assert card["status"] == "OK"
        assert "active" in card["headline"].lower()

    def test_vulture_card_fail_when_unhealthy(self):
        from app import _compute_vulture_card
        vulture = {
            "scheduler_freshness": {
                "status": "unhealthy",
                "detail": "timer missing",
                "next_run": None,
                "last_success": None,
            },
            "processes": [],
        }
        db = {"hunt_counts": {"active": 0, "total": 2, "paused": 0, "ended": 2}}
        card = _compute_vulture_card(vulture, db)
        assert card["status"] == "FAIL"

    def test_network_card_ok_when_all_present(self):
        from app import _compute_network_card
        raven = {"lan_ip": "192.168.1.10", "tailscale_ip": "100.x.y.z", "internet_ok": True}
        card = _compute_network_card(raven)
        assert card["status"] == "OK"
        assert card["lan_ip"] == "192.168.1.10"

    def test_network_card_warn_when_tailscale_missing(self):
        from app import _compute_network_card
        raven = {"lan_ip": "192.168.1.10", "tailscale_ip": None, "internet_ok": True}
        card = _compute_network_card(raven)
        assert card["status"] == "WARN"


class TestSchedulerHealth:
    def _evaluate(
        self,
        *,
        timer_active: str = "active",
        timer_unit: str | None = "vulture-scheduler.timer",
        service_active: str = "inactive",
        journal: list[str] | None = None,
        next_run: str | None = "Mon 2026-06-07 12:00:00 UTC",
        now: datetime | None = None,
    ):
        timer_svc = ServiceStatus(
            "vulture-scheduler timer",
            timer_unit,
            timer_active,
            "enabled" if timer_unit else "not configured",
        )
        service_warning = "Scheduler service failed" if service_active == "failed" else None
        service_svc = ServiceStatus(
            "vulture-scheduler service",
            "vulture-scheduler.service",
            service_active,
            "disabled",
            warning=service_warning,
        )
        now = now or datetime(2026, 6, 7, 12, 0, 0, tzinfo=timezone.utc)
        with patch("vulture_runtime._check_service", return_value=timer_svc):
            with patch("vulture_runtime._check_scheduler_service", return_value=service_svc):
                with patch("vulture_runtime._list_timer_next_run", return_value=next_run):
                    with patch("vulture_runtime._journal_lines", return_value=journal or []):
                        with patch("vulture_runtime.datetime", wraps=datetime) as mock_dt:
                            mock_dt.now.return_value = now
                            return _evaluate_scheduler_health([])

    def test_timer_active_service_inactive_success_healthy(self):
        journal = [
            "2026-06-07T11:50:00+0000 python[1]: 2026-06-07 11:50:00,000 [INFO] Hunt cycle completed",
        ]
        result = self._evaluate(service_active="inactive", journal=journal)
        assert result["status"] == "fresh"
        assert result["warning"] is None
        assert result["timer_active"] == "active"
        assert result["service_active"] == "inactive"

    def test_timer_missing_service_inactive_unhealthy(self):
        result = self._evaluate(timer_active="not found", timer_unit=None)
        assert result["status"] == "unhealthy"
        assert result["warning"] == "Scheduler timer missing/inactive"

    def test_timer_active_stale_logs_not_stale_when_run_scheduled(self):
        # Timer is healthy (active, has an upcoming run); an old log tail and ordinary
        # adapter warnings must not flag the scheduler as stale.
        journal = [
            "2026-06-07T10:00:00+0000 python[1]: 2026-06-07 10:00:00,000 [INFO] Hunt cycle completed",
        ]
        result = self._evaluate(service_active="inactive", journal=journal)
        assert result["status"] == "fresh"
        assert result["warning"] is None

    def test_fresh_adapter_warning_does_not_make_scheduler_stale(self):
        # A fresh "zero model slugs" adapter warning is NOT scheduler activity; with the
        # timer active and a run scheduled the scheduler must stay healthy.
        log_lines = [
            "2026-06-07 11:59:00,318 [WARNING] Swappa: zero model slugs for query "
            "'DDR4 desktop RAM' - query may match nothing",
        ]
        timer_svc = ServiceStatus(
            "vulture-scheduler timer", "vulture-scheduler.timer", "active", "enabled"
        )
        service_svc = ServiceStatus(
            "vulture-scheduler service", "vulture-scheduler.service", "inactive", "disabled"
        )
        now = datetime(2026, 6, 7, 12, 0, 0, tzinfo=timezone.utc)
        with patch("vulture_runtime._check_service", return_value=timer_svc):
            with patch("vulture_runtime._check_scheduler_service", return_value=service_svc):
                with patch(
                    "vulture_runtime._list_timer_next_run",
                    return_value="Sun 2026-06-07 12:30:00 UTC",
                ):
                    with patch("vulture_runtime._journal_lines", return_value=[]):
                        with patch("vulture_runtime.datetime", wraps=datetime) as mock_dt:
                            mock_dt.now.return_value = now
                            result = _evaluate_scheduler_health(log_lines)
        assert result["status"] == "fresh"
        assert result["warning"] is None

    def test_stale_when_no_upcoming_run_and_no_recent_activity(self):
        # No upcoming run scheduled and nothing recent in the journal/log -> stale.
        result = self._evaluate(service_active="inactive", journal=[], next_run=None)
        assert result["status"] == "stale"
        assert result["warning"] is not None

    def test_stale_when_no_upcoming_run_and_old_cycle(self):
        # Last cycle is old and the timer reports no upcoming run -> genuinely stale.
        journal = [
            "2026-06-07T08:00:00+0000 python[1]: 2026-06-07 08:00:00,000 [INFO] Hunt cycle completed",
        ]
        result = self._evaluate(service_active="inactive", journal=journal, next_run=None)
        assert result["status"] == "stale"
        assert result["warning"] is not None

    def test_next_run_display_includes_full_timestamp_and_relative(self):
        result = self._evaluate(
            service_active="inactive",
            next_run="Sun 2026-06-07 12:30:00 UTC",
            now=datetime(2026, 6, 7, 12, 0, 0, tzinfo=timezone.utc),
        )
        assert result["next_run"] is not None
        assert "2026-06-07 12:30:00" in result["next_run"]
        assert "in 30 min" in result["next_run"]

    def test_service_failed_unhealthy(self):
        result = self._evaluate(service_active="failed")
        assert result["status"] == "unhealthy"
        assert result["service"].warning == "Scheduler service failed"

    def test_service_active_running(self):
        result = self._evaluate(service_active="active")
        assert result["status"] == "running"
        assert result["warning"] is None
        assert "hunt cycle in progress" in result["detail"]

    def test_timer_active_oneshot_inactive_no_logs_no_warning(self):
        # Oneshot service is idle between runs; timer is active with next run scheduled.
        # No journal entries at all; timer health is the sole indicator.
        result = self._evaluate(service_active="inactive", journal=[])
        assert result["warning"] is None
        assert result["timer_active"] == "active"
        assert result["service_active"] == "inactive"
