"""
Unit tests for Canary parsing helpers, Raven storage checks, and resilience.
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
from canary.config import StorageVolumeSpec
from canary.parsers import (
    combine_status,
    derive_automount_unit,
    parse_df_output,
    parse_docker_ps_lines,
    parse_fstab_entries,
    parse_lan_ipv4_from_ip_br,
    parse_lsblk_uuid_map,
    parse_systemctl_failed,
    parse_tmux_sessions,
    storage_use_status,
    storage_volume_to_overall,
)
from canary.storage import evaluate_storage_volume
from canary.subprocess_util import set_command_runner


SAMPLE_DF = """\
Filesystem     1B-blocks      Used Available Use% Mounted on
/dev/sda2   100000000000 50000000000 40000000000  56% /
UUID=aaa-bbb-ccc   32000000000 16000000000  14400000000  50% /mnt/storage/toshiba_ext
"""

SAMPLE_LSBLK = (
    'UUID="aaa-bbb-ccc" FSTYPE="ext4" LABEL="TOSHIBA" SIZE="1T"\n'
)

SAMPLE_FSTAB = """
UUID=aaa-bbb-ccc /mnt/storage/toshiba_ext ext4 defaults,nofail,x-systemd.automount 0 2
"""

SAMPLE_SYSTEMCTL_FAILED = """\
  UNIT               LOAD   ACTIVE SUB    DESCRIPTION
  nginx.service      loaded failed failed A high performance web server

2 loaded units listed.
"""

SAMPLE_DOCKER_PS = """\
canary\tUp 2 hours\t
vulture-dashboard\tUp 1 hour\t8088/tcp
"""


class TestParsers:
    def test_combine_status_critical_wins(self):
        assert combine_status("ok", "warning", "critical") == "critical"

    def test_storage_volume_to_overall(self):
        assert storage_volume_to_overall("OK") == "ok"
        assert storage_volume_to_overall("MISSING_DEVICE") == "warning"
        assert storage_volume_to_overall("STALE_MOUNT") == "critical"
        assert storage_volume_to_overall("DF_TIMEOUT") == "critical"

    def test_storage_use_status_thresholds(self):
        assert storage_use_status(79.0, mounted=True, is_root=False) == "ok"
        assert storage_use_status(80.0, mounted=True, is_root=False) == "warning"
        assert storage_use_status(90.0, mounted=True, is_root=False) == "critical"

    def test_parse_df_output(self):
        parsed = parse_df_output(SAMPLE_DF)
        assert parsed["/"]["use_percent"] == 56.0
        assert parsed["/mnt/storage/toshiba_ext"]["use_percent"] == 50.0

    def test_parse_lsblk_uuid_map(self):
        parsed = parse_lsblk_uuid_map(SAMPLE_LSBLK)
        assert "aaa-bbb-ccc" in parsed
        assert parsed["aaa-bbb-ccc"]["fstype"] == "ext4"
        assert parsed["aaa-bbb-ccc"]["label"] == "TOSHIBA"
        assert "NAME" not in parsed["aaa-bbb-ccc"]

    def test_parse_fstab_entries(self):
        entries = parse_fstab_entries(SAMPLE_FSTAB)
        assert entries["/mnt/storage/toshiba_ext"]["uuid"] == "aaa-bbb-ccc"
        assert entries["/mnt/storage/toshiba_ext"]["automount_expected"] is True

    def test_derive_automount_unit(self):
        assert derive_automount_unit("/mnt/storage/toshiba_ext") == "mnt-storage-toshiba_ext.automount"
        assert derive_automount_unit("/mnt/storage/roost-spinning") == "mnt-storage-roost\\x2dspinning.automount"

    def test_parse_lan_ipv4_from_ip_br(self):
        text = "eth0  UP  192.168.1.50/24  fe80::1/64\n"
        assert parse_lan_ipv4_from_ip_br(text) == "192.168.1.50"

    def test_parse_systemctl_failed(self):
        count, names = parse_systemctl_failed(SAMPLE_SYSTEMCTL_FAILED)
        assert count == 2
        assert "nginx.service" in names

    # --- regression: bullet-prefixed lines from newer systemctl --failed output ---

    def test_parse_systemctl_failed_bullet_prefix(self):
        text = (
            "  UNIT                      LOAD   ACTIVE SUB    DESCRIPTION\n"
            "● pelican-monitor.service   loaded failed failed Pelican backup monitor\n"
            "\n"
            "1 loaded units listed.\n"
        )
        count, names = parse_systemctl_failed(text)
        assert "pelican-monitor.service" in names
        assert count >= 1

    def test_parse_systemctl_failed_no_bullet(self):
        text = (
            "  UNIT                      LOAD   ACTIVE SUB    DESCRIPTION\n"
            "  pelican-monitor.service   loaded failed failed Pelican backup monitor\n"
            "\n"
            "1 loaded units listed.\n"
        )
        count, names = parse_systemctl_failed(text)
        assert "pelican-monitor.service" in names
        assert count >= 1

    def test_parse_systemctl_failed_multiple_units_mixed(self):
        text = (
            "  UNIT                  LOAD   ACTIVE SUB    DESCRIPTION\n"
            "● nginx.service         loaded failed failed A high performance web server\n"
            "  sshd.service          loaded failed failed OpenSSH server daemon\n"
            "\n"
            "2 loaded units listed.\n"
        )
        count, names = parse_systemctl_failed(text)
        assert "nginx.service" in names
        assert "sshd.service" in names
        assert count == 2

    def test_parse_systemctl_failed_headers_summaries_ignored(self):
        text = (
            "  UNIT  LOAD  ACTIVE  SUB  DESCRIPTION\n"
            "\n"
            "0 loaded units listed.\n"
        )
        count, names = parse_systemctl_failed(text)
        assert count == 0
        assert names == []

    def test_parse_systemctl_failed_empty(self):
        assert parse_systemctl_failed("") == (0, [])
        assert parse_systemctl_failed("   \n\n  ") == (0, [])

    def test_parse_systemctl_failed_malformed_line_ignored(self):
        text = (
            "  UNIT  LOAD  ACTIVE  SUB  DESCRIPTION\n"
            "  not-a-unit   loaded failed failed something\n"
            "● also-not-a-unit  loaded failed failed other\n"
            "\n"
            "0 loaded units listed.\n"
        )
        count, names = parse_systemctl_failed(text)
        assert names == []

    def test_parse_systemctl_failed_return_structure(self):
        text = (
            "  UNIT                    LOAD   ACTIVE SUB    DESCRIPTION\n"
            "● pelican-monitor.service loaded failed failed Pelican backup monitor\n"
            "\n"
            "1 loaded units listed.\n"
        )
        result = parse_systemctl_failed(text)
        assert isinstance(result, tuple)
        assert len(result) == 2
        count, names = result
        assert isinstance(count, int)
        assert isinstance(names, list)

    def test_parse_docker_ps_lines(self):
        containers = parse_docker_ps_lines(SAMPLE_DOCKER_PS)
        assert containers[1]["name"] == "vulture-dashboard"

    def test_parse_tmux_sessions(self):
        text = "vulture: 1 windows\nbot: 1 windows\n"
        assert parse_tmux_sessions(text) == ["vulture", "bot"]


class TestStorageScenarios:
    UUID = "aaa-bbb-ccc"
    MOUNT = "/mnt/storage/toshiba_ext"
    AUTOMOUNT = "mnt-storage-toshiba_ext.automount"

    def setup_method(self) -> None:
        set_command_runner(None)

    def teardown_method(self) -> None:
        set_command_runner(None)

    def _spec(self) -> StorageVolumeSpec:
        return StorageVolumeSpec(
            label="toshiba_ext",
            mount_path=self.MOUNT,
            uuid=self.UUID,
            fstype="ext4",
            automount_expected=True,
            automount_unit=self.AUTOMOUNT,
        )

    def test_ok_mounted_volume(self):
        def runner(args, timeout):  # noqa: ARG001
            cmd = args[0]
            if cmd == "blkid":
                return True, self.UUID
            if cmd == "findmnt":
                return True, f"{self.MOUNT} UUID={self.UUID} ext4\n"
            if cmd == "df":
                return True, SAMPLE_DF
            return False, f"unexpected {args}"

        set_command_runner(runner)
        with (
            patch("canary.storage.host_path", side_effect=lambda p: p),
            patch("canary.storage.path_access_check", return_value=(True, None)),
        ):
            result = evaluate_storage_volume(
                self._spec(),
                lsblk_by_uuid=parse_lsblk_uuid_map(SAMPLE_LSBLK),
            )

        assert result["status"] == "OK"
        assert result["mounted"] is True
        assert result["uuid"] == self.UUID
        assert result["fstype"] == "ext4"
        assert result["use_percent"] == 50.0
        assert "sd" not in json.dumps(result).lower()

    def test_missing_device(self):
        def runner(args, timeout):  # noqa: ARG001
            if args[0] == "blkid":
                return False, "not found"
            return False, "unexpected"

        set_command_runner(runner)
        with patch("canary.storage.host_path", side_effect=lambda p: p):
            result = evaluate_storage_volume(self._spec(), lsblk_by_uuid={})

        assert result["status"] == "MISSING_DEVICE"
        assert result["mounted"] is False

    def test_stale_mount(self):
        def runner(args, timeout):  # noqa: ARG001
            if args[0] == "blkid":
                return True, self.UUID
            if args[0] == "findmnt":
                return True, f"{self.MOUNT} UUID={self.UUID} ext4\n"
            return False, "unexpected"

        set_command_runner(runner)
        with (
            patch("canary.storage.host_path", side_effect=lambda p: p),
            patch(
                "canary.storage.path_access_check",
                return_value=(False, "path access timed out"),
            ),
        ):
            result = evaluate_storage_volume(
                self._spec(),
                lsblk_by_uuid=parse_lsblk_uuid_map(SAMPLE_LSBLK),
            )

        assert result["status"] == "STALE_MOUNT"
        assert result["mounted"] is True

    def test_df_timeout(self):
        def runner(args, timeout):  # noqa: ARG001
            if args[0] == "blkid":
                return True, self.UUID
            if args[0] == "findmnt":
                return True, f"{self.MOUNT} UUID={self.UUID} ext4\n"
            if args[0] == "df":
                return False, "timed out"
            return False, "unexpected"

        set_command_runner(runner)
        with (
            patch("canary.storage.host_path", side_effect=lambda p: p),
            patch("canary.storage.path_access_check", return_value=(True, None)),
        ):
            result = evaluate_storage_volume(
                self._spec(),
                lsblk_by_uuid=parse_lsblk_uuid_map(SAMPLE_LSBLK),
            )

        assert result["status"] == "DF_TIMEOUT"

    def test_automount_inactive(self):
        def runner(args, timeout):  # noqa: ARG001
            if args[0] == "blkid":
                return True, self.UUID
            if args[0] == "findmnt":
                return False, "not mounted"
            if args[0] == "systemctl" and self.AUTOMOUNT in args:
                return True, "inactive"
            return False, f"unexpected {args}"

        set_command_runner(runner)
        with patch("canary.storage.host_path", side_effect=lambda p: p):
            result = evaluate_storage_volume(
                self._spec(),
                lsblk_by_uuid=parse_lsblk_uuid_map(SAMPLE_LSBLK),
            )

        assert result["status"] == "AUTOMOUNT_INACTIVE"
        assert result["mounted"] is False


class TestRunAllChecks:
    def setup_method(self) -> None:
        set_command_runner(None)

    def teardown_method(self) -> None:
        set_command_runner(None)

    def test_writes_status_when_commands_fail(self, tmp_path: Path):
        def failing_runner(args, timeout):  # noqa: ARG001
            return False, f"unavailable: {args[0]}"

        set_command_runner(failing_runner)

        with (
            patch("canary.checks.config.LOGS_DIR", tmp_path / "logs"),
            patch("canary.checks.config.HOST_ROOT", Path("/")),
            patch("canary.storage.config.FSTAB_PATH", tmp_path / "fstab"),
            patch("canary.storage._read_fstab", return_value={}),
        ):
            payload = run_all_checks()
            canary_app.write_status(payload)

        assert "overall_status" in payload
        assert payload["overall_status"] in ("ok", "warning", "critical")
        assert "alerts" in payload
        assert "storage" in payload["checks"]

    def test_run_all_checks_includes_scheduler_timer(self, tmp_path: Path):
        def mock_runner(args, timeout):  # noqa: ARG001
            cmd = args[0]
            if cmd == "ping":
                return True, "ok"
            if cmd == "hostname":
                return True, "raven"
            if cmd == "systemctl":
                if "--failed" in args:
                    return True, "0 loaded units listed.\n"
                return True, "active"
            if cmd == "docker":
                return True, SAMPLE_DOCKER_PS
            if cmd == "lsblk":
                return True, SAMPLE_LSBLK
            if cmd == "blkid":
                return False, "not found"
            if cmd in ("findmnt", "df", "ip", "tailscale", "pgrep", "tmux"):
                return False, "unavailable"
            return False, f"unexpected {args}"

        set_command_runner(mock_runner)

        with (
            patch("canary.checks.config.LOGS_DIR", tmp_path / "logs"),
            patch("canary.checks.config.HOST_ROOT", Path("/")),
            patch("canary.storage.config.FSTAB_PATH", tmp_path / "fstab"),
            patch("canary.storage._read_fstab", return_value=parse_fstab_entries(SAMPLE_FSTAB)),
        ):
            payload = run_all_checks()

        service_labels = [s["label"] for s in payload["checks"]["services"]["services"]]
        assert "vulture_scheduler_timer" in service_labels
        assert "dashboard" in service_labels
        assert payload["host"] == "raven"
