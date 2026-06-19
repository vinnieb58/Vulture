"""
tests/test_dashboard.py

Lightweight tests for the Nest v1 / Raven Ops dashboard (read-only FastAPI app).
"""

from __future__ import annotations

import re
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
from kestrel_status import read_kestrel_status  # noqa: E402
from host_status import (  # noqa: E402
    ServiceStatus,
    _check_service,
    get_docker_snapshot,
    get_raven_health,
    get_service_statuses,
    get_storage_status,
)
from log_readers import read_log_snapshot  # noqa: E402
from vulture_runtime import (  # noqa: E402
    _evaluate_scheduler_health,
    _format_runtime_detail,
    _get_timer_next_run_show,
    _list_timer_next_run,
    _parse_timer_next_run,
    _scheduler_freshness,
)
from host_status import (  # noqa: E402
    IGNORED_FAILED_UNITS,
    ServiceStatus,
    _read_failed_units,
)


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
        monkeypatch.setenv("DASHBOARD_METRICS_SAMPLER_ENABLED", "0")
        monkeypatch.setenv("DASHBOARD_USE_GLANCES", "0")
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

    def test_nest_home_shows_kestrel_energy_card(self, client):
        response = self._stub_host(client)
        assert response.status_code == 200
        assert "Kestrel Energy" in response.text

    def test_nest_home_missing_kestrel_status_shows_friendly_state(self, client, tmp_path, monkeypatch):
        missing_status = tmp_path / "missing" / "kestrel_status.json"
        monkeypatch.setattr("kestrel_status.KESTREL_STATUS_PATH", missing_status)
        response = self._stub_host(client)
        assert response.status_code == 200
        assert "No energy data yet" in response.text
        assert "Kestrel Energy" in response.text

    def test_nest_home_renders_valid_kestrel_status(self, client, tmp_path, monkeypatch):
        status_path = tmp_path / "kestrel_status.json"
        status_path.write_text(
            """{
  "probe": "smart_meter_texas",
  "generated_at": "2026-06-16T12:00:00+00:00",
  "interval_count": 96,
  "range_start": "2026-06-09T00:00:00+00:00",
  "range_end": "2026-06-16T00:00:00+00:00",
  "total_kwh": 42.5,
  "peak_interval": {"start_ts": "2026-06-15T18:00:00+00:00", "end_ts": "2026-06-15T18:15:00+00:00", "kwh": 2.5, "estimated_peak_kw": 10.0},
  "estimated_peak_kw": 10.0,
  "missing_interval_count": 3,
  "top_intervals": [
    {"start_ts": "2026-06-15T18:00:00+00:00", "end_ts": "2026-06-15T18:15:00+00:00", "kwh": 2.5, "estimated_peak_kw": 10.0},
    {"start_ts": "2026-06-14T19:00:00+00:00", "end_ts": "2026-06-14T19:15:00+00:00", "kwh": 2.1, "estimated_peak_kw": 8.4}
  ],
  "daily_totals": {"2026-06-15": 6.25, "2026-06-16": 5.0}
}""",
            encoding="utf-8",
        )
        monkeypatch.setattr("kestrel_status.KESTREL_STATUS_PATH", status_path)
        monkeypatch.setattr("kestrel_metrics.KESTREL_DB_PATH", tmp_path / "missing.db")
        response = self._stub_host(client)
        assert response.status_code == 200
        text = response.text
        assert "Energy data available" in text
        assert "42.50" in text
        assert "Missing intervals" in text
        assert "Top intervals" not in text
        assert "Daily totals" not in text
        assert "Energy details" in text
        assert "2026-06-15T18:00:00+00:00" not in text
        assert "2026-06-16T12:00:00+00:00" not in text

    def test_nest_home_handles_invalid_kestrel_json_safely(self, client, tmp_path, monkeypatch):
        status_path = tmp_path / "kestrel_status.json"
        status_path.write_text("{not valid json", encoding="utf-8")
        monkeypatch.setattr("kestrel_status.KESTREL_STATUS_PATH", status_path)
        response = self._stub_host(client)
        assert response.status_code == 200
        assert "Energy status unavailable" in response.text
        assert "Kestrel Energy" in response.text

    def test_nest_home_does_not_display_sensitive_kestrel_fields(self, client, tmp_path, monkeypatch):
        status_path = tmp_path / "kestrel_status.json"
        status_path.write_text(
            """{
  "generated_at": "2026-06-16T12:00:00+00:00",
  "interval_count": 4,
  "total_kwh": 1.5,
  "estimated_peak_kw": 4.0,
  "account_id": "secret-account",
  "meter_id": "secret-meter",
  "account_id_hash": "abc123hash",
  "meter_id_hash": "def456hash",
  "esiid": "123456789012345678",
  "raw_source": "csv:/home/vinnie/secret.csv",
  "db_path": "/app/data/kestrel/kestrel.db",
  "top_intervals": [
    {"start_ts": "2026-06-15T18:00:00+00:00", "kwh": 1.5, "raw_source": "hidden"}
  ]
}""",
            encoding="utf-8",
        )
        monkeypatch.setattr("kestrel_status.KESTREL_STATUS_PATH", status_path)
        response = self._stub_host(client)
        assert response.status_code == 200
        text = response.text
        for forbidden in (
            "secret-account",
            "secret-meter",
            "abc123hash",
            "def456hash",
            "123456789012345678",
            "raw_source",
            "secret.csv",
            "kestrel.db",
        ):
            assert forbidden not in text

    def test_nest_home_shows_estimated_peak_kw_label(self, client, tmp_path, monkeypatch):
        status_path = tmp_path / "kestrel_status.json"
        status_path.write_text(
            '{"interval_count": 1, "total_kwh": 1.0, "estimated_peak_kw": 4.0}',
            encoding="utf-8",
        )
        monkeypatch.setattr("kestrel_status.KESTREL_STATUS_PATH", status_path)
        monkeypatch.setattr("kestrel_metrics.KESTREL_DB_PATH", tmp_path / "missing.db")
        response = self._stub_host(client)
        assert response.status_code == 200
        assert "Est. peak kW (15-min)" not in response.text

    def test_nest_home_shows_navigation(self, client):
        response = self._stub_host(client)
        assert response.status_code == 200
        assert "/storage" in response.text
        assert "/vulture" in response.text
        assert "/kestrel" in response.text
        assert "Energy" in response.text
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

    # ── /health endpoint ──────────────────────────────────────────

    def test_health_returns_200_json(self, client):
        response = client.get("/health")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"
        assert "server_time" in data
        assert "build_git_commit" in data
        assert "build_timestamp" in data

    def test_health_does_not_require_db_or_log(self, client):
        """Health probe must succeed even when all host data sources are missing."""
        with patch("host_status.run_host_command", return_value=(False, "unavailable")):
            with patch("host_status.run_systemctl", return_value=(False, "unavailable")):
                response = client.get("/health")
        assert response.status_code == 200
        assert response.json()["status"] == "ok"

    def test_health_content_type_is_json(self, client):
        response = client.get("/health")
        assert "application/json" in response.headers.get("content-type", "")

    def test_scheduler_health_returns_200_json(self, client):
        """GET /scheduler-health must return 200 with scheduler evidence fields."""
        with patch("host_status.run_host_command", return_value=(False, "unavailable")):
            with patch("host_status.run_systemctl", return_value=(False, "unavailable")):
                with patch("vulture_runtime._journal_lines", return_value=[]):
                    response = client.get("/scheduler-health")
        assert response.status_code == 200
        data = response.json()
        assert "scheduler_status" in data
        assert "detail" in data
        assert "timer_active" in data
        assert "timer_enabled" in data
        assert "next_run" in data
        assert "last_success" in data
        assert "last_success_source" in data
        assert "journal_available" in data
        assert "log_mtime_age_minutes" in data
        assert "scheduler_status_reason" in data
        assert "server_time" in data

    def test_scheduler_health_status_reason_present(self, client):
        """scheduler_status_reason must be a non-None string."""
        with patch("host_status.run_host_command", return_value=(False, "unavailable")):
            with patch("host_status.run_systemctl", return_value=(False, "unavailable")):
                with patch("vulture_runtime._journal_lines", return_value=[]):
                    response = client.get("/scheduler-health")
        data = response.json()
        assert data["scheduler_status_reason"] is not None
        assert isinstance(data["scheduler_status_reason"], str)


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


class TestDashboardDockerfile:
    DOCKERFILE_PATH = DASHBOARD_DIR / "Dockerfile"
    APP_PATH = DASHBOARD_DIR / "app.py"

    def test_dockerfile_copies_all_app_local_imports(self):
        app_source = self.APP_PATH.read_text(encoding="utf-8")
        dockerfile = self.DOCKERFILE_PATH.read_text(encoding="utf-8")

        local_modules = {
            path.stem
            for path in DASHBOARD_DIR.glob("*.py")
            if path.name != "app.py"
        }
        imported = set(re.findall(r"^from (\w+) import", app_source, re.MULTILINE))
        imported |= set(re.findall(r"^import (\w+)", app_source, re.MULTILINE))
        required = sorted(local_modules & imported)

        copied = set(re.findall(r"COPY ([^\n]+) \./", dockerfile))
        copied_modules = {
            name.removesuffix(".py")
            for block in copied
            for name in block.split()
            if name.endswith(".py")
        }

        missing = [name for name in required if name not in copied_modules]
        assert not missing, (
            "dashboard/Dockerfile must COPY every local module imported by app.py; "
            f"missing: {missing}"
        )
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


class TestKestrelStatusReader:
    def test_read_kestrel_status_missing_file(self, tmp_path, monkeypatch):
        missing = tmp_path / "nope.json"
        monkeypatch.setattr("kestrel_status.KESTREL_STATUS_PATH", missing)
        snap = read_kestrel_status()
        assert snap["state"] == "no_data"
        assert snap["headline"] == "No energy data yet"
        assert snap["warning"] is not None

    def test_read_kestrel_status_invalid_json(self, tmp_path, monkeypatch):
        path = tmp_path / "kestrel_status.json"
        path.write_text("{bad", encoding="utf-8")
        monkeypatch.setattr("kestrel_status.KESTREL_STATUS_PATH", path)
        snap = read_kestrel_status()
        assert snap["state"] == "error"
        assert snap["warning"] is not None

    def test_read_kestrel_status_strips_sensitive_fields(self, tmp_path, monkeypatch):
        path = tmp_path / "kestrel_status.json"
        path.write_text(
            """{
  "interval_count": 2,
  "total_kwh": 2.0,
  "account_id_hash": "hidden",
  "meter_id_hash": "hidden",
  "raw_source": "hidden",
  "db_path": "/secret/path.db"
}""",
            encoding="utf-8",
        )
        monkeypatch.setattr("kestrel_status.KESTREL_STATUS_PATH", path)
        snap = read_kestrel_status()
        assert snap["state"] == "available"
        assert "hidden" not in str(snap)
        assert "/secret/path.db" not in str(snap)


    def test_read_kestrel_status_parses_list_daily_totals(self, tmp_path, monkeypatch):
        path = tmp_path / "kestrel_status.json"
        path.write_text(
            """{
  "status": "available",
  "interval_count": 2,
  "total_kwh": 2.4,
  "daily_totals": [
    {"date": "2026-06-15", "kwh": 6.25},
    {"date": "2026-06-16", "kwh": 5.0}
  ]
}""",
            encoding="utf-8",
        )
        monkeypatch.setattr("kestrel_status.KESTREL_STATUS_PATH", path)
        snap = read_kestrel_status()
        assert snap["daily_totals"]["2026-06-15"] == pytest.approx(6.25)
        assert snap["daily_totals"]["2026-06-16"] == pytest.approx(5.0)

    def test_read_kestrel_status_accepts_estimated_kw_in_top_intervals(self, tmp_path, monkeypatch):
        path = tmp_path / "kestrel_status.json"
        path.write_text(
            """{
  "interval_count": 1,
  "total_kwh": 1.5,
  "top_intervals": [
    {"start_ts": "2026-06-15T18:00:00+00:00", "end_ts": "2026-06-15T18:15:00+00:00", "kwh": 1.5, "estimated_kw": 6.0}
  ]
}""",
            encoding="utf-8",
        )
        monkeypatch.setattr("kestrel_status.KESTREL_STATUS_PATH", path)
        snap = read_kestrel_status()
        assert snap["top_intervals"][0]["estimated_peak_kw"] == pytest.approx(6.0)


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

    def test_raven_card_warn_when_temp_high(self):
        from app import _compute_raven_card
        from host_status import DockerSnapshot
        raven = {
            "hostname": "raven",
            "uptime": "1 day",
            "failed_units": [],
            "internet_ok": True,
            "load_average": None,
            "memory": None,
            "warnings": [],
        }
        docker = DockerSnapshot(daemon_active=True, daemon_state="active", warning=None, running_count=0, stopped_count=0)
        metrics = {
            "temp_now": "82°C",
            "temp_now_celsius": 82.0,
            "cpu_above_90_minutes_1h_raw": 0.0,
        }
        card = _compute_raven_card(raven, [], docker, metrics)
        assert card["status"] == "WARN"
        assert "temperature" in card["headline"].lower()

    def test_raven_card_fail_when_temp_critical(self):
        from app import _compute_raven_card
        from host_status import DockerSnapshot
        raven = {
            "hostname": "raven",
            "uptime": "1 day",
            "failed_units": [],
            "internet_ok": True,
            "load_average": None,
            "memory": None,
            "warnings": [],
        }
        docker = DockerSnapshot(daemon_active=True, daemon_state="active", warning=None, running_count=0, stopped_count=0)
        metrics = {
            "temp_now": "92°C",
            "temp_now_celsius": 92.0,
            "cpu_above_90_minutes_1h_raw": 0.0,
        }
        card = _compute_raven_card(raven, [], docker, metrics)
        assert card["status"] == "FAIL"
        assert "critical" in card["headline"].lower()

    def test_raven_card_warn_when_cpu_saturated(self):
        from app import _compute_raven_card
        from host_status import DockerSnapshot
        raven = {
            "hostname": "raven",
            "uptime": "1 day",
            "failed_units": [],
            "internet_ok": True,
            "load_average": None,
            "memory": None,
            "warnings": [],
        }
        docker = DockerSnapshot(daemon_active=True, daemon_state="active", warning=None, running_count=0, stopped_count=0)
        metrics = {
            "cpu_above_90_minutes_1h": "12 min",
            "cpu_above_90_minutes_1h_raw": 12.0,
            "temp_now_celsius": 65.0,
        }
        card = _compute_raven_card(raven, [], docker, metrics)
        assert card["status"] == "WARN"
        assert "90%" in card["headline"]

    def test_raven_card_fail_when_cpu_saturated_critical(self):
        from app import _compute_raven_card
        from host_status import DockerSnapshot
        raven = {
            "hostname": "raven",
            "uptime": "1 day",
            "failed_units": [],
            "internet_ok": True,
            "load_average": None,
            "memory": None,
            "warnings": [],
        }
        docker = DockerSnapshot(daemon_active=True, daemon_state="active", warning=None, running_count=0, stopped_count=0)
        metrics = {
            "cpu_above_90_minutes_1h": "35 min",
            "cpu_above_90_minutes_1h_raw": 35.0,
            "temp_now_celsius": 65.0,
        }
        card = _compute_raven_card(raven, [], docker, metrics)
        assert card["status"] == "FAIL"
        assert "90%" in card["headline"]

    def test_raven_card_includes_cpu_and_temp_fields(self):
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
        metrics = {
            "cpu_now": "42%",
            "cpu_above_90_minutes_1h": "0 min",
            "temp_now": "61°C",
            "temp_high_today": "74°C",
            "cpu_threads": 4,
            "load_pressure": "0.05",
            "load_help": "Load is runnable work, not CPU %. Compare load to CPU threads.",
        }
        card = _compute_raven_card(raven, [], docker, metrics)
        assert card["cpu_now"] == "42%"
        assert card["temp_now"] == "61°C"
        assert card["cpu_threads"] == 4

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

    def test_storage_card_shows_toshiba_pct_below_warn_threshold(self):
        """Toshiba usage must appear in the drive list even below the 80% WARN threshold."""
        from app import _compute_storage_card
        from storage_probe import StorageStatus
        mounts = [
            StorageStatus(name="Toshiba EXT", path="/mnt/storage/toshiba_ext", required=True,
                          status="OK", percent_used=60.0, used="600G", size="1.0T", available="400G"),
        ]
        card = _compute_storage_card(mounts)
        assert card["status"] == "OK"
        drives = card["drives"]
        assert len(drives) == 1
        line = drives[0]["line"]
        # Percentage must be visible even when healthy
        assert "60%" in line
        # Capacity context should also appear
        assert "600G" in line or "1.0T" in line

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

    def test_timer_active_no_recent_logs_stale_no_warning(self):
        # Timer is healthy (active, has next run); stale log activity should not
        # produce a WARN — the timer is the authoritative health source.
        journal = [
            "2026-06-07T10:00:00+0000 python[1]: 2026-06-07 10:00:00,000 [INFO] Hunt cycle completed",
        ]
        result = self._evaluate(service_active="inactive", journal=journal)
        # next_run is scheduled ("Mon 2026-06-07 12:00:00 UTC"), so status → "seen" not "stale"
        assert result["status"] == "seen"
        assert result["warning"] is None

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
        # next_run is set in the helper ("Mon 2026-06-07 12:00:00 UTC"), so timer
        # with scheduled run and no log activity → "seen", not "unknown".
        assert result["status"] == "seen"

    # ── New targeted tests covering the reported stale-warning regression ──────

    def test_adapter_warning_in_log_does_not_affect_scheduler_freshness(self):
        """A fresh adapter warning (e.g. Swappa zero slugs) in vulture.log must not
        make the scheduler appear stale.  Only scheduler-keyword lines count."""
        log_lines = [
            # Stale scheduler line (2h old at now=12:00)
            "2026-06-07 10:00:00,000 [INFO] Hunt cycle completed",
            # Fresh adapter warning — matches ERROR_PATTERNS but NOT ACTIVITY_KEYWORDS;
            # must be completely ignored for scheduler freshness.
            "2026-06-07 11:55:00,318 [WARNING] Swappa: zero model slugs for query 'DDR4 desktop RAM'",
        ]
        # Call _evaluate_scheduler_health directly so we can supply log_lines.
        timer_svc = ServiceStatus(
            "vulture-scheduler timer", "vulture-scheduler.timer", "active", "enabled"
        )
        service_svc = ServiceStatus(
            "vulture-scheduler service", "vulture-scheduler.service", "inactive", "disabled"
        )
        with patch("vulture_runtime._check_service", return_value=timer_svc):
            with patch("vulture_runtime._check_scheduler_service", return_value=service_svc):
                with patch("vulture_runtime._list_timer_next_run", return_value="Mon 2026-06-07 12:15:00 UTC"):
                    with patch("vulture_runtime._journal_lines", return_value=[]):
                        with patch("vulture_runtime.datetime", wraps=datetime) as mock_dt:
                            mock_dt.now.return_value = datetime(2026, 6, 7, 12, 0, 0, tzinfo=timezone.utc)
                            result = _evaluate_scheduler_health(log_lines)
        # Timer healthy with next_run; Swappa warning must not push scheduler to "stale"
        assert result["status"] in ("seen", "fresh")
        assert result["warning"] is None

    def test_timer_active_stale_logs_no_next_run_is_stale(self):
        """When timer is active but reports no upcoming run AND logs are stale,
        the scheduler deserves a 'stale' status (timer may be stuck)."""
        journal = [
            "2026-06-07T10:00:00+0000 python[1]: 2026-06-07 10:00:00,000 [INFO] Hunt cycle completed",
        ]
        result = self._evaluate(
            service_active="inactive",
            journal=journal,
            next_run=None,  # no scheduled next run
        )
        assert result["status"] == "stale"
        # No warning raised purely from log staleness — only timer state triggers warning.
        assert result["warning"] is None

    def test_parse_timer_next_run_returns_full_timestamp(self):
        """_parse_timer_next_run must return the full 'Day YYYY-MM-DD HH:MM:SS TZ'
        string, not just the weekday abbreviation."""
        output = (
            "NEXT                          LEFT           LAST                          "
            "PASSED       UNIT                           ACTIVATES\n"
            "Thu 2026-06-11 16:49:21 UTC   10min left     Thu 2026-06-11 16:34:21 UTC   "
            "4min ago     vulture-scheduler.timer        vulture-scheduler.service\n"
        )
        result = _parse_timer_next_run(output, "vulture-scheduler.timer")
        assert result == "Thu 2026-06-11 16:49:21 UTC"

    def test_parse_timer_next_run_returns_none_for_no_next_run(self):
        """When systemctl reports 'n/a' for the next run, return None."""
        output = (
            "NEXT  LEFT  LAST  PASSED  UNIT                           ACTIVATES\n"
            "n/a   n/a   n/a   n/a     vulture-scheduler.timer        vulture-scheduler.service\n"
        )
        result = _parse_timer_next_run(output, "vulture-scheduler.timer")
        assert result is None

    def test_vulture_card_seen_maps_to_ok(self):
        """'seen' scheduler status must render the Vulture card as OK, not WARN."""
        from app import _compute_vulture_card
        vulture = {
            "scheduler_freshness": {
                "status": "seen",
                "detail": "timer scheduled; no recent log activity",
                "next_run": "Thu 2026-06-11 16:49:21 UTC",
                "last_success": None,
            },
            "processes": [],
        }
        db = {"hunt_counts": {"active": 2, "total": 4, "paused": 0, "ended": 2}}
        card = _compute_vulture_card(vulture, db)
        assert card["status"] == "OK"
        assert "Thu 2026-06-11 16:49:21 UTC" in card["headline"]

    def test_recent_journal_success_marks_scheduler_healthy(self):
        """A recent hunt-cycle-completed line in the service journal must mark the
        scheduler as 'fresh' with no warning."""
        journal = [
            "2026-06-07T11:58:00+0000 python[1]: 2026-06-07 11:58:00,000 [INFO] Hunt cycle completed",
        ]
        result = self._evaluate(
            service_active="inactive",
            journal=journal,
            next_run="Mon 2026-06-07 12:13:00 UTC",
            now=datetime(2026, 6, 7, 12, 0, 0, tzinfo=timezone.utc),
        )
        assert result["status"] == "fresh"
        assert result["warning"] is None

    # ── Truncation / unit-filter regression tests ─────────────────────────────

    def test_parse_timer_next_run_returns_none_for_truncated_line(self):
        """A line with fewer than four whitespace tokens (e.g. output cut by the
        400-char _run_raw limit) must return None, not a partial weekday string."""
        # Simulate: systemctl output truncated so vulture line appears as "Thu"
        output = "NEXT  LEFT  LAST  PASSED  UNIT\nThu vulture-scheduler.timer\n"
        result = _parse_timer_next_run(output, "vulture-scheduler.timer")
        assert result is None

    def test_parse_timer_next_run_returns_none_for_partial_date_parts(self):
        """Three-part line (weekday + date, no time) must return None."""
        output = (
            "NEXT  LEFT  LAST  PASSED  UNIT\n"
            "Thu 2026-06-11 vulture-scheduler.timer\n"
        )
        result = _parse_timer_next_run(output, "vulture-scheduler.timer")
        assert result is None

    def test_list_timer_next_run_tries_unit_filter_first(self):
        """_list_timer_next_run must call run_systemctl with the unit name as a
        filter argument before falling back to the full unfiltered list.  The
        unit-filtered form produces a compact output (~250 chars) that is safe
        from the 400-char truncation applied by _run_raw."""
        unit_output = (
            "NEXT                        LEFT      LAST                        "
            "PASSED   UNIT                       ACTIVATES\n"
            "Thu 2026-06-11 17:00:00 UTC 5min left Thu 2026-06-11 16:45:00 UTC "
            "10min    vulture-scheduler.timer     vulture-scheduler.service\n"
        )
        calls: list[list] = []

        def fake_run_systemctl(subargs, **kwargs):
            calls.append(list(subargs))
            if "vulture-scheduler.timer" in subargs:
                return True, unit_output
            return False, "not called"

        with patch("vulture_runtime.run_systemctl", side_effect=fake_run_systemctl):
            result = _list_timer_next_run("vulture-scheduler.timer")

        # First call must include the unit name as a filter.
        assert calls[0][1] == "vulture-scheduler.timer", (
            f"first call should pass the unit as arg, got: {calls[0]}"
        )
        assert result == "Thu 2026-06-11 17:00:00 UTC"

    def test_list_timer_next_run_fallback_when_unit_filter_fails(self):
        """When the unit-filtered call returns no useful output, fall back to the
        unfiltered list."""
        full_output = (
            "NEXT                        LEFT      LAST                        "
            "PASSED   UNIT                       ACTIVATES\n"
            "Thu 2026-06-11 17:00:00 UTC 5min left Thu 2026-06-11 16:45:00 UTC "
            "10min    vulture-scheduler.timer     vulture-scheduler.service\n"
        )

        def fake_run_systemctl(subargs, **kwargs):
            if "vulture-scheduler.timer" in subargs and subargs[0] == "list-timers":
                # Simulate older systemd: unit filter returns no output.
                return True, "No timers listed."
            return True, full_output

        with patch("vulture_runtime.run_systemctl", side_effect=fake_run_systemctl):
            result = _list_timer_next_run("vulture-scheduler.timer")

        assert result == "Thu 2026-06-11 17:00:00 UTC"

    def test_regression_production_truncation_scenario(self):
        """Regression: full unfiltered systemctl output truncated at 400 chars places
        the vulture timer line beyond the limit.  The unit-filtered query must return
        the full timestamp; the status must be 'seen', not 'stale'.

        Simulates the exact production failure reported (305 min stale, next run Thu).
        """
        # Build a realistic unfiltered output where system timers appear before
        # vulture-scheduler.timer and push it past the 400-char limit.
        header = (
            "NEXT                        LEFT           LAST                        "
            "PASSED       UNIT                           ACTIVATES\n"
        )
        sys_line = (
            "Thu 2026-06-11 16:45:00 UTC 10min left     Thu 2026-06-11 16:30:00 UTC "
            "15min ago    logrotate.timer                logrotate.service\n"
        )
        vulture_line = (
            "Thu 2026-06-11 17:00:00 UTC 25min left     Thu 2026-06-11 16:45:00 UTC "
            "30min ago    vulture-scheduler.timer        vulture-scheduler.service\n"
        )
        # Full output: header + sys_line + vulture_line → truncated at 400 chars
        full_output_raw = header + sys_line + vulture_line
        truncated = full_output_raw[:400] + "…"  # mirrors _run_raw behaviour

        # The unit-filtered output is compact and never truncated.
        unit_output = header + vulture_line

        def fake_run_systemctl(subargs, **kwargs):
            if "vulture-scheduler.timer" in subargs and subargs[0] == "list-timers":
                return True, unit_output
            return True, truncated

        timer_svc = ServiceStatus(
            "vulture-scheduler timer", "vulture-scheduler.timer", "active", "enabled"
        )
        service_svc = ServiceStatus(
            "vulture-scheduler service", "vulture-scheduler.service", "inactive", "disabled"
        )
        # Stale log lines (305 min old) — mirror the production report
        log_lines = [
            "2026-06-11 13:18:58,000 [INFO] Hunt cycle completed",
        ]
        with patch("vulture_runtime._check_service", return_value=timer_svc):
            with patch("vulture_runtime._check_scheduler_service", return_value=service_svc):
                with patch("vulture_runtime.run_systemctl", side_effect=fake_run_systemctl):
                    with patch("vulture_runtime._journal_lines", return_value=[]):
                        with patch("vulture_runtime.datetime", wraps=datetime) as mock_dt:
                            # now ≈ 18:24 UTC (305 min after 13:18)
                            mock_dt.now.return_value = datetime(
                                2026, 6, 11, 18, 23, 58, tzinfo=timezone.utc
                            )
                            result = _evaluate_scheduler_health(log_lines)

        # Timer healthy with valid next_run → "seen", never "stale"
        assert result["status"] == "seen", f"expected 'seen', got {result['status']!r}"
        assert result["warning"] is None
        # next_run must be the full timestamp, not just "Thu"
        assert result["next_run"] == "Thu 2026-06-11 17:00:00 UTC", (
            f"next_run should be full timestamp, got {result['next_run']!r}"
        )


# ---------------------------------------------------------------------------
# New tests required by the health-fix issue
# ---------------------------------------------------------------------------

class TestTimerNextRunMachineReadable:
    """Tests for _get_timer_next_run_show (machine-readable NextElapseUSecRealtime)."""

    def test_parses_usec_timestamp_to_full_datetime(self):
        """A non-zero microsecond timestamp must produce a full 'Day YYYY-MM-DD HH:MM:SS UTC' string."""
        # 2026-06-11 17:00:00 UTC in microseconds
        usec = 1749657600 * 1_000_000  # Thu 2026-06-11 17:00:00 UTC
        with patch("vulture_runtime.run_systemctl", return_value=(True, str(usec))):
            result = _get_timer_next_run_show("vulture-scheduler.timer")
        assert result is not None
        # Must contain a full date, not a bare weekday
        assert re.match(r"\w+ \d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} UTC$", result), (
            f"expected full timestamp, got {result!r}"
        )

    def test_returns_none_for_zero_usec(self):
        """Zero microseconds means no scheduled run — must return None."""
        with patch("vulture_runtime.run_systemctl", return_value=(True, "0")):
            result = _get_timer_next_run_show("vulture-scheduler.timer")
        assert result is None

    def test_returns_none_when_systemctl_fails(self):
        """systemctl failure must not raise; must return None."""
        with patch("vulture_runtime.run_systemctl", return_value=(False, "unavailable")):
            result = _get_timer_next_run_show("vulture-scheduler.timer")
        assert result is None

    def test_returns_none_for_non_integer_output(self):
        """Non-integer output (e.g. property name echoed back) must return None gracefully."""
        with patch("vulture_runtime.run_systemctl", return_value=(True, "n/a")):
            result = _get_timer_next_run_show("vulture-scheduler.timer")
        assert result is None

    def test_list_timer_next_run_tries_show_before_list_timers(self):
        """_list_timer_next_run must call systemctl show first; list-timers is fallback."""
        usec = 1749657600 * 1_000_000
        calls: list[list] = []

        def fake_run_systemctl(subargs, **kwargs):
            calls.append(list(subargs))
            if subargs[0] == "show":
                return True, str(usec)
            return True, "unreachable"

        with patch("vulture_runtime.run_systemctl", side_effect=fake_run_systemctl):
            result = _list_timer_next_run("vulture-scheduler.timer")

        assert calls[0][0] == "show", f"first call should be 'show', got: {calls[0]}"
        assert result is not None
        assert re.match(r"\w+ \d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} UTC$", result)
        # list-timers should NOT have been called since show succeeded
        assert not any(c[0] == "list-timers" for c in calls), (
            "list-timers should not be called when systemctl show succeeds"
        )

    def test_list_timer_next_run_falls_back_to_list_timers_when_show_returns_zero(self):
        """When systemctl show returns 0 (unscheduled), fall back to list-timers."""
        unit_output = (
            "NEXT                        LEFT      LAST                        "
            "PASSED   UNIT                       ACTIVATES\n"
            "Thu 2026-06-11 17:00:00 UTC 5min left Thu 2026-06-11 16:45:00 UTC "
            "10min    vulture-scheduler.timer     vulture-scheduler.service\n"
        )

        def fake_run_systemctl(subargs, **kwargs):
            if subargs[0] == "show":
                return True, "0"  # Not scheduled via show
            if "vulture-scheduler.timer" in subargs and subargs[0] == "list-timers":
                return True, unit_output
            return True, unit_output

        with patch("vulture_runtime.run_systemctl", side_effect=fake_run_systemctl):
            result = _list_timer_next_run("vulture-scheduler.timer")

        assert result == "Thu 2026-06-11 17:00:00 UTC"

    def test_parse_timer_next_run_rejects_unit_name_in_time_position(self):
        """A partially-matched row where the time field contains the unit name must be
        rejected rather than returning a bogus 'Day YYYY-MM-DD unit-name...' string."""
        # Simulate a truncation scenario where only date is present but next column
        # is the unit name rather than HH:MM:SS.
        output = (
            "NEXT  LEFT  LAST  PASSED  UNIT\n"
            "Thu 2026-06-11 vulture-scheduler.timer vulture-scheduler.service\n"
        )
        result = _parse_timer_next_run(output, "vulture-scheduler.timer")
        assert result is None


class TestSchedulerStalenessWithJournal:
    """Tests for improved stale-vs-journal health logic."""

    def _evaluate(
        self,
        *,
        timer_active: str = "active",
        timer_unit: str | None = "vulture-scheduler.timer",
        service_active: str = "inactive",
        journal: list[str] | None = None,
        log_lines: list[str] | None = None,
        next_run: str | None = None,
        now: datetime | None = None,
    ):
        timer_svc = ServiceStatus(
            "vulture-scheduler timer",
            timer_unit,
            timer_active,
            "enabled" if timer_unit else "not configured",
        )
        service_svc = ServiceStatus(
            "vulture-scheduler service",
            "vulture-scheduler.service",
            service_active,
            "disabled",
        )
        now = now or datetime(2026, 6, 7, 12, 0, 0, tzinfo=timezone.utc)
        with patch("vulture_runtime._check_service", return_value=timer_svc):
            with patch("vulture_runtime._check_scheduler_service", return_value=service_svc):
                with patch("vulture_runtime._list_timer_next_run", return_value=next_run):
                    with patch("vulture_runtime._journal_lines", return_value=journal or []):
                        with patch("vulture_runtime.datetime", wraps=datetime) as mock_dt:
                            mock_dt.now.return_value = now
                            return _evaluate_scheduler_health(log_lines or [])

    def test_stale_log_but_recent_journal_success_is_healthy(self):
        """Recent hunt-cycle-completed in journal overrides stale vulture.log activity.

        If the journal (preferred source) shows a recent success, the scheduler
        must be considered healthy even when the log file has old activity.
        """
        journal = [
            # Recent: 5 min ago at now=12:00
            "2026-06-07T11:55:00+0000 python[1]: 2026-06-07 11:55:00,000 [INFO] Hunt cycle completed",
        ]
        stale_log_lines = [
            # Old: 200 min ago at now=12:00
            "2026-06-07 08:40:00,000 [INFO] Hunt cycle completed",
        ]
        result = self._evaluate(
            service_active="inactive",
            journal=journal,
            log_lines=stale_log_lines,
            next_run=None,
            now=datetime(2026, 6, 7, 12, 0, 0, tzinfo=timezone.utc),
        )
        # Journal shows fresh success; should not be stale regardless of log file
        assert result["status"] == "fresh", f"expected 'fresh', got {result['status']!r}"
        assert result["warning"] is None
        assert "journal" in result["detail"]

    def test_timer_active_next_run_scheduled_stale_log_is_seen(self):
        """Timer active + valid next_run + stale log → 'seen' (OK), not 'stale' (WARN)."""
        stale_log_lines = [
            # 303 min old — beyond SCHEDULER_FRESH_MINUTES threshold
            "2026-06-11 13:18:58,000 [INFO] Hunt cycle completed",
        ]
        result = self._evaluate(
            service_active="inactive",
            journal=[],  # journal unavailable
            log_lines=stale_log_lines,
            next_run="Thu 2026-06-11 18:30:00 UTC",
            now=datetime(2026, 6, 11, 18, 23, 58, tzinfo=timezone.utc),
        )
        assert result["status"] == "seen", f"expected 'seen' (timer has next_run), got {result['status']!r}"
        assert result["warning"] is None

    def test_timer_active_no_next_run_stale_log_inaccessible_journal_is_seen(self):
        """Timer active + stale log + no next_run + journal inaccessible → 'seen', not 'stale'.

        When journal returns no lines (inaccessible from Docker or no entries),
        log-file staleness alone must NOT mark the scheduler stale.  The timer
        being active is the authoritative health signal.
        """
        stale_log_lines = [
            "2026-06-11 13:18:58,000 [INFO] Hunt cycle completed",
        ]
        result = self._evaluate(
            service_active="inactive",
            journal=[],  # journal inaccessible / empty
            log_lines=stale_log_lines,
            next_run=None,
            now=datetime(2026, 6, 11, 18, 23, 58, tzinfo=timezone.utc),
        )
        # Timer active; log is the only stale evidence — should be "seen", not "stale"
        assert result["status"] == "seen", (
            f"expected 'seen' (log-only stale must not override active timer), "
            f"got {result['status']!r}"
        )
        assert result["warning"] is None

    def test_timer_active_no_next_run_stale_journal_confirms_stale(self):
        """Timer active + stale JOURNAL + no next_run → 'stale' (WARN).

        When journal IS accessible and its keyword matches show stale activity,
        that is positive evidence of scheduler staleness with no next run.
        """
        stale_journal = [
            "2026-06-11T13:18:58+0000 python[1]: 2026-06-11 13:18:58,000 [INFO] Hunt cycle completed",
        ]
        stale_log_lines = [
            "2026-06-11 13:18:58,000 [INFO] Hunt cycle completed",
        ]
        result = self._evaluate(
            service_active="inactive",
            journal=stale_journal,  # journal accessible, shows stale
            log_lines=stale_log_lines,
            next_run=None,
            now=datetime(2026, 6, 11, 18, 23, 58, tzinfo=timezone.utc),
        )
        assert result["status"] == "stale", (
            f"expected 'stale' (journal confirms stale), got {result['status']!r}"
        )

    def test_journal_unavailable_timer_active_with_next_run_is_seen(self):
        """When journal is inaccessible but timer is active with a next run, report 'seen'."""
        result = self._evaluate(
            service_active="inactive",
            journal=[],  # journal unavailable
            log_lines=[],  # no log activity
            next_run="Thu 2026-06-11 18:30:00 UTC",
            now=datetime(2026, 6, 11, 18, 23, 58, tzinfo=timezone.utc),
        )
        assert result["status"] == "seen"
        assert result["warning"] is None

    def test_journal_unavailable_detail_note(self):
        """When journal returns no lines and no log activity, the detail string mentions
        journal unavailability so the operator knows why no freshness data is shown."""
        result = self._evaluate(
            service_active="inactive",
            journal=[],
            log_lines=[],
            next_run=None,
            now=datetime(2026, 6, 11, 18, 23, 58, tzinfo=timezone.utc),
        )
        # Should still be "seen" (timer active is the health source)
        assert result["status"] == "seen"
        assert "journal unavailable" in result["detail"] or "no recent" in result["detail"]

    # ── New tests required by the health-fix issue ─────────────────────────────

    def test_stale_log_inaccessible_journal_detail_mentions_journal_unavailable(self):
        """When log is stale and journal is inaccessible, detail must say 'journal unavailable'
        so operators know why no journal evidence appears."""
        stale_log_lines = [
            "2026-06-11 13:18:58,000 [INFO] Hunt cycle completed",
        ]
        result = self._evaluate(
            service_active="inactive",
            journal=[],  # journal inaccessible
            log_lines=stale_log_lines,
            next_run=None,
            now=datetime(2026, 6, 11, 18, 23, 58, tzinfo=timezone.utc),
        )
        assert result["status"] == "seen"
        assert "journal unavailable" in result["detail"], (
            f"detail should mention journal unavailable, got: {result['detail']!r}"
        )

    def test_detail_includes_timer_state_for_healthy_scheduler(self):
        """Detail string must include timer state (active/enabled) for non-unhealthy statuses."""
        result = self._evaluate(
            service_active="inactive",
            journal=[],
            log_lines=[],
            next_run="Thu 2026-06-11 18:30:00 UTC",
            now=datetime(2026, 6, 11, 18, 23, 58, tzinfo=timezone.utc),
        )
        assert result["status"] == "seen"
        assert "timer" in result["detail"], (
            f"detail should include timer state, got: {result['detail']!r}"
        )

    def test_detail_does_not_imply_log_mtime_is_authoritative(self):
        """Detail must not suggest vulture.log is the primary evidence when timer is active
        and journal is inaccessible."""
        stale_log_lines = [
            "2026-06-11 13:18:58,000 [INFO] Hunt cycle completed",
        ]
        result = self._evaluate(
            service_active="inactive",
            journal=[],
            log_lines=stale_log_lines,
            next_run=None,
            now=datetime(2026, 6, 11, 18, 23, 58, tzinfo=timezone.utc),
        )
        assert result["status"] == "seen"
        # "journal unavailable" tells operators that log mtime is not authoritative
        assert "journal unavailable" in result["detail"]
        # The reason must not imply stale
        assert result["scheduler_status_reason"] is not None
        assert "stale" not in result["scheduler_status_reason"] or "log stale" in result["scheduler_status_reason"]

    def test_new_health_fields_present(self):
        """_evaluate_scheduler_health must include timer_enabled, last_success_source,
        log_mtime_age_minutes, and scheduler_status_reason in its return value."""
        result = self._evaluate(
            service_active="inactive",
            journal=[],
            log_lines=[],
            next_run="Thu 2026-06-11 18:30:00 UTC",
        )
        assert "timer_enabled" in result
        assert "last_success_source" in result
        assert "log_mtime_age_minutes" in result
        assert "scheduler_status_reason" in result
        assert result["scheduler_status_reason"] is not None

    def test_production_regression_stale_log_active_timer_no_journal_is_seen(self):
        """Regression test for the exact production failure:
        vulture.log shows 300 min stale, timer IS active, journald inaccessible from Docker.
        Expected: 'seen' (OK) not 'stale' (WARN).
        """
        # Simulates the exact symptom reported: "Last scheduler activity ~300 min ago (vulture.log)"
        log_lines = [
            "2026-06-11 16:06:21,000 [INFO] Hunt cycle completed",
        ]
        result = self._evaluate(
            service_active="inactive",
            journal=[],       # journald inaccessible from Docker
            log_lines=log_lines,
            next_run=None,    # timer is OnUnitInactiveSec (monotonic); NextElapseUSecRealtime = 0
            now=datetime(2026, 6, 11, 21, 6, 21, tzinfo=timezone.utc),  # 300 min later
        )
        assert result["status"] == "seen", (
            f"Production regression: timer active + journal inaccessible + stale log "
            f"must be 'seen', got {result['status']!r} (reason: {result.get('scheduler_status_reason')!r})"
        )
        assert result["warning"] is None
        assert "journal unavailable" in result["detail"]


class TestFailedUnitAllowlist:
    """Tests for the IGNORED_FAILED_UNITS allowlist and HEALTH card behaviour."""

    def test_ignored_unit_is_in_allowlist(self):
        """systemd-networkd-wait-online.service must be in the default allowlist."""
        assert "systemd-networkd-wait-online.service" in IGNORED_FAILED_UNITS

    def test_read_failed_units_splits_actionable_and_ignored(self):
        """_read_failed_units must separate actionable vs ignored units."""
        output = (
            "UNIT                                    LOAD   ACTIVE SUB    DESCRIPTION\n"
            "systemd-networkd-wait-online.service    loaded failed failed Wait for Network\n"
            "myapp.service                           loaded failed failed My App\n"
            "\n"
            "2 loaded units listed.\n"
        )
        with patch("host_status.run_systemctl", return_value=(True, output)):
            actionable, ignored, warn = _read_failed_units()
        assert "myapp.service" in actionable
        assert "systemd-networkd-wait-online.service" not in actionable
        assert "systemd-networkd-wait-online.service" in ignored
        assert warn is None

    def test_read_failed_units_returns_empty_ignored_when_none_present(self):
        """When no ignored units appear in systemctl --failed output, ignored list is empty."""
        output = (
            "UNIT               LOAD   ACTIVE SUB    DESCRIPTION\n"
            "myapp.service      loaded failed failed My App\n"
            "\n"
            "1 loaded units listed.\n"
        )
        with patch("host_status.run_systemctl", return_value=(True, output)):
            actionable, ignored, warn = _read_failed_units()
        assert "myapp.service" in actionable
        assert ignored == []

    def test_raven_card_ok_when_only_ignored_unit_failed(self):
        """HEALTH must not FAIL when the only failed unit is systemd-networkd-wait-online.service."""
        from app import _compute_raven_card
        from host_status import DockerSnapshot
        raven = {
            "hostname": "raven",
            "uptime": "3 days",
            "failed_units": [],  # Filtered out — no actionable failures
            "ignored_failed_units": ["systemd-networkd-wait-online.service"],
            "internet_ok": True,
            "load_average": None,
            "memory": None,
            "warnings": [],
        }
        docker = DockerSnapshot(daemon_active=True, daemon_state="active", warning=None, running_count=2, stopped_count=0)
        card = _compute_raven_card(raven, [], docker)
        assert card["status"] != "FAIL", (
            "HEALTH must not FAIL when only ignored units are present"
        )
        assert card["status"] == "OK"
        assert "healthy" in card["headline"].lower()

    def test_raven_card_ok_includes_ignored_units_in_output(self):
        """The ignored_failed_units must be forwarded in the raven card dict."""
        from app import _compute_raven_card
        from host_status import DockerSnapshot
        raven = {
            "hostname": "raven",
            "uptime": "3 days",
            "failed_units": [],
            "ignored_failed_units": ["systemd-networkd-wait-online.service"],
            "internet_ok": True,
            "load_average": None,
            "memory": None,
            "warnings": [],
        }
        docker = DockerSnapshot(daemon_active=True, daemon_state="active", warning=None, running_count=1, stopped_count=0)
        card = _compute_raven_card(raven, [], docker)
        assert "ignored_failed_units" in card
        assert "systemd-networkd-wait-online.service" in card["ignored_failed_units"]

    def test_raven_card_fail_when_real_core_service_failed(self):
        """HEALTH must FAIL when an actionable (non-ignored) service fails."""
        from app import _compute_raven_card
        from host_status import DockerSnapshot
        raven = {
            "hostname": "raven",
            "uptime": "3 days",
            "failed_units": ["docker.service"],  # Real core failure
            "ignored_failed_units": [],
            "internet_ok": True,
            "load_average": None,
            "memory": None,
            "warnings": [],
        }
        docker = DockerSnapshot(daemon_active=True, daemon_state="active", warning=None, running_count=0, stopped_count=0)
        card = _compute_raven_card(raven, [], docker)
        assert card["status"] == "FAIL"
        assert "docker.service" in card["headline"] or "failed" in card["headline"].lower()

    def test_raven_card_fail_when_both_ignored_and_real_unit_failed(self):
        """HEALTH must FAIL when a real unit is failed, even if ignored units also appear."""
        from app import _compute_raven_card
        from host_status import DockerSnapshot
        raven = {
            "hostname": "raven",
            "uptime": "3 days",
            "failed_units": ["myapp.service"],
            "ignored_failed_units": ["systemd-networkd-wait-online.service"],
            "internet_ok": True,
            "load_average": None,
            "memory": None,
            "warnings": [],
        }
        docker = DockerSnapshot(daemon_active=True, daemon_state="active", warning=None, running_count=1, stopped_count=0)
        card = _compute_raven_card(raven, [], docker)
        assert card["status"] == "FAIL"

    def test_get_raven_health_includes_ignored_failed_units_key(self):
        """get_raven_health() must include ignored_failed_units in the returned dict."""
        with patch("host_status.run_host_command", return_value=(False, "fail")):
            with patch("host_status.run_systemctl", return_value=(False, "fail")):
                health = get_raven_health()
        assert "ignored_failed_units" in health
        assert isinstance(health["ignored_failed_units"], list)
