"""
tests/test_crow_system.py

Unit tests for Crow v0.2 Raven system health helpers (no Discord or live systemd).
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from crow.system import _status
from crow.system.docker import DockerStatus, _parse_container_names, docker_level
from crow.system.health import parse_healthcheck_summary
from crow.system.ports import _parse_listening_ports
from crow.system.services import ServiceCheck, format_service_line, service_level
from crow.system.storage import StorageMount, format_storage_line, storage_level


class TestStatusEvaluation:
    def test_combine_levels_fail_wins(self):
        assert _status.combine_levels("ok", "warn", "fail") == "fail"

    def test_combine_levels_warn_over_ok(self):
        assert _status.combine_levels("ok", "warn") == "warn"

    def test_overall_from_items(self):
        items = [
            _status.StatusItem("a", "ok"),
            _status.StatusItem("b", "warn"),
        ]
        assert _status.overall_from_items(items) == "warn"

    def test_format_item_line_with_detail(self):
        item = _status.StatusItem("Internet", "ok", "Reachable")
        assert "✅" in _status.format_item_line(item)
        assert "Reachable" in _status.format_item_line(item)


class TestHealthcheckParsing:
    SAMPLE_SUMMARY = """
  Overall: WARN — production may be OK; review warnings.
"""

    def test_parse_warn(self):
        assert parse_healthcheck_summary(self.SAMPLE_SUMMARY) == "warn"

    def test_parse_fail(self):
        text = "  Overall: FAIL — review failed items above."
        assert parse_healthcheck_summary(text) == "fail"

    def test_parse_ok(self):
        text = "  Overall: OK"
        assert parse_healthcheck_summary(text) == "ok"

    def test_parse_missing_defaults_warn(self):
        assert parse_healthcheck_summary("no summary here") == "warn"


class TestServiceLevel:
    def test_active_service_is_ok(self):
        check = ServiceCheck("SSH", "ssh.service", True, "active")
        assert service_level(check) == "ok"

    def test_inactive_vulture_is_fail(self):
        check = ServiceCheck("Vulture Bot", "vulture-bot", False, "inactive")
        assert service_level(check) == "fail"

    def test_inactive_ssh_is_warn(self):
        check = ServiceCheck("SSH", "ssh.service", False, "inactive")
        assert service_level(check) == "warn"

    def test_format_active_line(self):
        check = ServiceCheck("SSH", "ssh.service", True, "active")
        assert format_service_line(check) == "SSH                ACTIVE"

    def test_format_inactive_line(self):
        check = ServiceCheck("Vulture Scheduler", "vulture-scheduler", False, "inactive")
        assert "❌" in format_service_line(check)
        assert "Vulture Scheduler" in format_service_line(check)


class TestStorageLevel:
    def test_root_missing_is_fail(self):
        mount = StorageMount("Root SSD", "/", False, None)
        assert storage_level(mount) == "fail"

    def test_optional_missing_is_warn(self):
        mount = StorageMount("portable_beast", "/mnt/portable_beast", False, None)
        assert storage_level(mount) == "warn"

    def test_mounted_high_usage_warn(self):
        mount = StorageMount("Root SSD", "/", True, 92.0)
        assert storage_level(mount) == "warn"

    def test_format_missing_mount(self):
        mount = StorageMount("toshiba_ext", "/mnt/toshiba_ext", False, None)
        line = format_storage_line(mount)
        assert "MISSING" in line
        assert "toshiba_ext" in line

    def test_format_used_mount(self):
        mount = StorageMount("Root SSD", "/", True, 35.0)
        assert "35% used" in format_storage_line(mount)


class TestDockerParsing:
    def test_parse_container_names(self):
        text = "portainer\nhello-raven\nvulture-dashboard\n"
        assert _parse_container_names(text) == [
            "hello-raven",
            "portainer",
            "vulture-dashboard",
        ]

    def test_docker_inactive_is_fail(self):
        status = DockerStatus(False, "inactive", [], [])
        assert docker_level(status) == "fail"


class TestPortParsing:
    SAMPLE_SS = """
Netid State  Recv-Q Send-Q Local Address:Port Peer Address:PortProcess
tcp   LISTEN 0      128          0.0.0.0:22        0.0.0.0:*
tcp   LISTEN 0      128                *:9443            *:*
tcp   LISTEN 0      128             127.0.0.1:8088       0.0.0.0:*
"""

    def test_parse_listening_ports(self):
        ports = _parse_listening_ports(self.SAMPLE_SS)
        assert 22 in ports
        assert 9443 in ports
        assert 8088 in ports


class TestRavenHealthSummary:
    def test_get_raven_health_summary_aggregates(self):
        from crow.system.health import get_raven_health_summary

        with patch("crow.system.health.get_hostname", return_value="raven"):
            with patch("crow.system.health.get_uptime", return_value="up 3 days, 4 hours"):
                with patch("crow.system.health.check_internet_reachable", return_value=True):
                    with patch(
                        "crow.system.health.get_tailscale_status",
                        return_value=__import__(
                            "crow.system.network", fromlist=["TailscaleStatus"]
                        ).TailscaleStatus(True, "100.82.1.18", "raven"),
                    ):
                        with patch(
                            "crow.system.health.get_storage_summary",
                            return_value=[
                                StorageMount("Root SSD", "/", True, 35.0),
                                StorageMount("portable_beast", "/mnt/portable_beast", False, None),
                            ],
                        ):
                            with patch(
                                "crow.system.health.get_critical_service_statuses",
                                return_value=[
                                    ServiceCheck("SSH", "ssh.service", True, "active"),
                                    ServiceCheck("Tailscale", "tailscaled", True, "active"),
                                    ServiceCheck("Samba", "smbd", True, "active"),
                                    ServiceCheck("Docker", "docker", True, "active"),
                                    ServiceCheck("Vulture Bot", "vulture-bot", True, "active"),
                                    ServiceCheck(
                                        "Vulture Scheduler",
                                        "vulture-scheduler",
                                        True,
                                        "active",
                                    ),
                                ],
                            ):
                                with patch(
                                    "crow.system.health.get_docker_status",
                                    return_value=DockerStatus(
                                        True,
                                        "active",
                                        ["portainer", "hello-raven"],
                                        [],
                                    ),
                                ):
                                    summary = get_raven_health_summary(post_reboot=True)

        assert summary.hostname == "raven"
        assert summary.overall in ("ok", "warn", "fail")
        assert summary.docker_detail.running == ["portainer", "hello-raven"]
        assert any("portable_beast" in i.label for i in summary.storage)


class TestPostRebootValidation:
    def test_post_reboot_warn_when_storage_missing(self):
        from crow.system.health import get_post_reboot_validation

        with patch(
            "crow.system.health.get_critical_service_statuses",
            return_value=[
                ServiceCheck("SSH", "ssh.service", True, "active"),
                ServiceCheck("Tailscale", "tailscaled", True, "active"),
                ServiceCheck("Docker", "docker", True, "active"),
                ServiceCheck("Samba", "smbd", True, "active"),
                ServiceCheck("Vulture Bot", "vulture-bot", True, "active"),
                ServiceCheck("Vulture Scheduler", "vulture-scheduler", True, "active"),
            ],
        ):
            with patch("crow.system.health.check_internet_reachable", return_value=True):
                with patch(
                    "crow.system.health.get_storage_summary",
                    return_value=[
                        StorageMount("Root SSD", "/", True, 10.0),
                        StorageMount("portable_beast", "/mnt/portable_beast", False, None),
                        StorageMount("toshiba_ext", "/mnt/toshiba_ext", False, None),
                    ],
                ):
                    result = get_post_reboot_validation()

        assert result.overall == "warn"
        labels = [c.label for c in result.checks]
        assert "portable_beast" in labels
        assert "toshiba_ext" in labels


class TestCrowStorageConfig:
    """Verify Crow config defaults use /mnt/storage/* layout (Aviary standard)."""

    def test_default_mounts_use_storage_parent(self):
        from crow.config import EXPECTED_STORAGE_MOUNTS

        paths = [path for _, path in EXPECTED_STORAGE_MOUNTS]
        for path in paths:
            if path == "/":
                continue  # root SSD is always /
            assert path.startswith("/mnt/storage/"), (
                f"Crow default mount '{path}' should use /mnt/storage/* layout"
            )

    def test_default_mounts_include_microsd(self):
        from crow.config import EXPECTED_STORAGE_MOUNTS

        paths = [path for _, path in EXPECTED_STORAGE_MOUNTS]
        assert "/mnt/storage/microsd" in paths

    def test_default_mounts_include_toshiba(self):
        from crow.config import EXPECTED_STORAGE_MOUNTS

        paths = [path for _, path in EXPECTED_STORAGE_MOUNTS]
        assert "/mnt/storage/toshiba_ext" in paths

    def test_legacy_paths_not_in_defaults(self):
        """Old paths /mnt/microsd and /mnt/toshiba_ext must not appear in defaults."""
        from crow.config import EXPECTED_STORAGE_MOUNTS

        paths = [path for _, path in EXPECTED_STORAGE_MOUNTS]
        assert "/mnt/microsd" not in paths, "legacy /mnt/microsd still in Crow defaults"
        assert "/mnt/toshiba_ext" not in paths, "legacy /mnt/toshiba_ext still in Crow defaults"
        assert "/mnt/portable_beast" not in paths, "legacy /mnt/portable_beast still in Crow defaults"
