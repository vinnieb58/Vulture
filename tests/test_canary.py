"""
Unit tests for Canary v0.1 parsing helpers and resilient check aggregation.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from canary import app as canary_app
from canary.checks import run_all_checks
from canary.parsers import (
    combine_status,
    parse_df_output,
    parse_docker_ps_lines,
    parse_lan_ipv4_from_ip_br,
    parse_systemctl_failed,
    parse_tmux_sessions,
    storage_use_status,
)
from canary.subprocess_util import set_command_runner


SAMPLE_DF = """\
Filesystem     1B-blocks      Used Available Use% Mounted on
/dev/sda2   100000000000 50000000000 40000000000  56% /
/dev/sdb1    32000000000 28000000000  2000000000  93% /mnt/storage/microsd
"""

SAMPLE_SYSTEMCTL_FAILED = """\
  UNIT               LOAD   ACTIVE SUB    DESCRIPTION
  nginx.service      loaded failed failed A high performance web server

2 loaded units listed.
"""

SAMPLE_DOCKER_PS = """\
canary\tUp 2 hours\t
vulture-dashboard\tExited (0) 1 day ago\t
"""


class TestParsers:
    def test_combine_status_critical_wins(self):
        assert combine_status("ok", "warning", "critical") == "critical"

    def test_combine_status_warning_over_ok(self):
        assert combine_status("ok", "warning") == "warning"

    def test_storage_use_status_thresholds(self):
        assert storage_use_status(79.0, mounted=True, is_root=False) == "ok"
        assert storage_use_status(80.0, mounted=True, is_root=False) == "warning"
        assert storage_use_status(90.0, mounted=True, is_root=False) == "critical"
        assert storage_use_status(None, mounted=False, is_root=False) == "warning"
        assert storage_use_status(None, mounted=False, is_root=True) == "critical"

    def test_parse_df_output(self):
        parsed = parse_df_output(SAMPLE_DF)
        assert "/" in parsed
        assert parsed["/"]["use_percent"] == 56.0
        assert parsed["/mnt/storage/microsd"]["use_percent"] == 93.0

    def test_parse_lan_ipv4_from_ip_br(self):
        text = "eth0  UP  192.168.1.50/24  fe80::1/64\n"
        assert parse_lan_ipv4_from_ip_br(text) == "192.168.1.50"

    def test_parse_systemctl_failed(self):
        count, names = parse_systemctl_failed(SAMPLE_SYSTEMCTL_FAILED)
        assert count == 2
        assert "nginx.service" in names

    def test_parse_systemctl_failed_zero(self):
        text = "0 loaded units listed.\n"
        count, names = parse_systemctl_failed(text)
        assert count == 0
        assert names == []

    def test_parse_docker_ps_lines(self):
        containers = parse_docker_ps_lines(SAMPLE_DOCKER_PS)
        assert len(containers) == 2
        assert containers[0]["name"] == "canary"
        assert containers[1]["status"].startswith("Exited")

    def test_parse_tmux_sessions(self):
        text = "vulture: 1 windows\nbot: 1 windows\n"
        assert parse_tmux_sessions(text) == ["vulture", "bot"]


class TestRunAllChecks:
    def setup_method(self) -> None:
        set_command_runner(None)

    def teardown_method(self) -> None:
        set_command_runner(None)

    def test_writes_status_when_commands_fail(self, tmp_path: Path):
        def failing_runner(args, timeout):  # noqa: ARG001
            return False, f"unavailable: {args[0]}"

        set_command_runner(failing_runner)

        status_path = tmp_path / "canary_status.json"
        with (
            patch("canary.checks.config.STATUS_PATH", status_path),
            patch("canary.app.STATUS_PATH", status_path),
            patch("canary.checks.config.LOGS_DIR", tmp_path / "logs"),
            patch("canary.checks.config.HOST_ROOT", Path("/")),
        ):
            payload = run_all_checks()
            canary_app.write_status(payload)

        assert status_path.is_file()
        data = json.loads(status_path.read_text(encoding="utf-8"))
        assert data["overall_status"] in ("ok", "warning", "critical")
        assert "checks" in data
        assert "internet" in data["checks"]
        assert data["checks"]["internet"]["status"] == "warning"

    def test_run_all_checks_structure_with_mocks(self, tmp_path: Path):
        responses = {
            ("ping",): (True, "reachable"),
            ("ip",): (True, "eth0  UP  10.0.0.5/24\n"),
            ("tailscale",): (True, "100.64.0.1\n"),
            ("systemctl",): (True, "active"),
            ("df",): (True, SAMPLE_DF),
            ("docker",): (True, SAMPLE_DOCKER_PS),
            ("pgrep",): (False, "command not found: pgrep"),
            ("tmux",): (False, "command not found: tmux"),
            ("hostname",): (True, "test-host"),
        }

        def mock_runner(args, timeout):  # noqa: ARG001
            key = (args[0],)
            if key in responses:
                return responses[key]
            if args[0] == "ping" and len(args) > 3 and args[-1] == "google.com":
                return True, "reachable"
            if args[0] == "systemctl" and "--failed" in args:
                return True, "0 loaded units listed.\n"
            if args[0] == "systemctl" and "is-enabled" in args:
                return True, "enabled"
            return False, f"unexpected: {args}"

        set_command_runner(mock_runner)

        with (
            patch("canary.checks.config.LOGS_DIR", tmp_path / "logs"),
            patch("canary.checks.config.HOST_ROOT", Path("/")),
            patch("canary.checks.Path.is_dir", return_value=False),
        ):
            payload = run_all_checks()

        assert payload["host"] == "test-host"
        assert payload["overall_status"] in ("ok", "warning", "critical")
        assert "generated_at" in payload
        assert payload["checks"]["docker"]["running_count"] == 1
