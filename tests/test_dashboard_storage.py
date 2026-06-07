"""
tests/test_dashboard_storage.py

Storage / Roost mount detection tests for Vulture Dashboard.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

DASHBOARD_DIR = Path(__file__).resolve().parent.parent / "dashboard"
sys.path.insert(0, str(DASHBOARD_DIR))

from storage_config import ExpectedDrive  # noqa: E402
from storage_probe import (  # noqa: E402
    StorageStatus,
    get_storage_status,
    probe_expected_drive,
    status_display_class,
)

MOUNTINFO_ROOT = """\
24 1 8:2 / / rw,relatime shared:1 - ext4 /dev/sda2 rw
"""

MOUNTINFO_WITH_MICROSD = MOUNTINFO_ROOT + """\
36 24 179:1 / /mnt/storage/microsd rw,relatime shared:2 - ext4 /dev/mmcblk0p1 rw
"""

MOUNTINFO_WITH_TOSHIBA = MOUNTINFO_WITH_MICROSD.replace(
    "/dev/mmcblk0p1 rw\n",
    "/dev/mmcblk0p1 rw\n"
    + "37 24 8:17 / /mnt/storage/toshiba_ext rw,relatime shared:3 - ntfs3 /dev/sdb1 rw\n",
)

MOUNTINFO_AUTOFS_PELICAN = MOUNTINFO_WITH_TOSHIBA + """\
38 24 0:61 / /mnt/storage/pelican_backup rw,relatime shared:4 - autofs systemd-1 rw
"""


def _mountinfo_patch(text: str):
    mountinfo = DASHBOARD_DIR.parent / "dashboard" / "storage_probe.py"
    return patch(
        "storage_probe._read_mountinfo",
        return_value=_parse_mountinfo_for_test(text),
    )


def _parse_mountinfo_for_test(text: str) -> dict[str, tuple[str, str]]:
    from parsers import parse_mountinfo

    return parse_mountinfo(text)


def _df_map() -> dict[str, str]:
    return {
        "/host/root": (
            "Filesystem      Size  Used Avail Use% Mounted on\n"
            "/dev/sda2        29G   12G   15G  45% /host/root\n"
        ),
        "/mnt/storage/microsd": (
            "Filesystem      Size  Used Avail Use% Mounted on\n"
            "/dev/mmcblk0p1  234G   80G  142G  36% /mnt/storage/microsd\n"
        ),
        "/mnt/storage/toshiba_ext": (
            "Filesystem      Size  Used Avail Use% Mounted on\n"
            "/dev/sdb1       1.4T  400G  1.0T  29% /mnt/storage/toshiba_ext\n"
        ),
        "/mnt/storage/portable_beast": (
            "Filesystem      Size  Used Avail Use% Mounted on\n"
            "/dev/sda2        29G   12G   15G  45% /mnt/storage/portable_beast\n"
        ),
        "/mnt/storage/pelican_backup": (
            "Filesystem      Size  Used Avail Use% Mounted on\n"
            "/dev/sda2        29G   12G   15G  45% /mnt/storage/pelican_backup\n"
        ),
    }


def _command_path(args) -> str | None:
    if not args:
        return None
    return args[-1]


def _mock_df(args, **_kwargs):
    path = _command_path(args)
    outputs = _df_map()
    output = outputs.get(path or "")
    if output is None:
        return False, "no such file or directory"
    return True, output


def _mock_findmnt(args, **_kwargs):
    if len(args) < 4 or args[0] != "findmnt":
        return False, "unexpected command"
    host_path = args[2]
    mapping = {
        "/": ("/dev/sda2", "ext4", None),
        "/mnt/storage/microsd": ("/dev/mmcblk0p1", "ext4", "ff481ad2-e9bd-4868-8c8c-6729a461e4b4"),
        "/mnt/storage/toshiba_ext": ("/dev/sdb1", "ntfs3", "0846863B46862A10"),
        "/mnt/storage/pelican_backup": ("systemd-1", "autofs", None),
    }
    if host_path not in mapping:
        return False, ""
    source, fstype, uuid = mapping[host_path]
    if uuid:
        return True, f"{source} {fstype} {uuid}"
    return True, f"{source} {fstype}"


def _mock_blkid(args, **_kwargs):
    device = args[-1]
    if device == "/dev/mmcblk0p1":
        if "-s" in args and "UUID" in args:
            return True, "ff481ad2-e9bd-4868-8c8c-6729a461e4b4"
        if "-s" in args and "LABEL" in args:
            return True, "SK256"
    if device == "/dev/sdb1":
        if "-s" in args and "UUID" in args:
            return True, "0846863B46862A10"
        if "-s" in args and "LABEL" in args:
            return True, "TOSHIBA EXT"
    return False, ""


def _mock_systemctl(unit, **_kwargs):
    states = {
        "mnt-storage-microsd.automount": "active",
        "mnt-storage-microsd.mount": "active",
        "mnt-storage-toshiba_ext.automount": "active",
        "mnt-storage-toshiba_ext.mount": "active",
        "mnt-storage-pelican_backup.automount": "active",
        "mnt-storage-pelican_backup.mount": "inactive",
        "mnt-storage-raven_nvme.automount": "active",
        "mnt-storage-raven_nvme.mount": "inactive",
        "mnt-storage-roost_spinning_0.automount": "active",
        "mnt-storage-roost_spinning_0.mount": "inactive",
    }
    return True, states.get(unit, "unknown")


class TestStorageProbeScenarios:
    def test_mounted_microsd_ok(self):
        drive = ExpectedDrive(
            name="MicroSD",
            path="/mnt/storage/microsd",
            expected_uuid="ff481ad2-e9bd-4868-8c8c-6729a461e4b4",
            expected_fstype="ext4",
            expected_label="SK256",
        )
        with _mountinfo_patch(MOUNTINFO_WITH_MICROSD):
            with patch("storage_probe.run_command", side_effect=_mock_df):
                with patch("storage_probe.run_host_command", side_effect=_mock_findmnt):
                    with patch("storage_probe.systemctl_is_active", side_effect=_mock_systemctl):
                        with patch("storage_probe._lookup_uuid", return_value=("ff481ad2-e9bd-4868-8c8c-6729a461e4b4", None)):
                            with patch("storage_probe._lookup_label", return_value="SK256"):
                                with patch.object(Path, "exists", return_value=True):
                                    result = probe_expected_drive(drive, root_source="/dev/sda2")
        assert result.status in ("OK", "OK_AUTOMOUNTED")
        assert result.actual_source == "/dev/mmcblk0p1"
        assert result.mounted is True
        assert result.size == "234G"

    def test_mounted_toshiba_ok(self):
        drive = ExpectedDrive(
            name="Toshiba EXT",
            path="/mnt/storage/toshiba_ext",
            expected_uuid="0846863B46862A10",
            expected_fstype="ntfs3",
            expected_label="TOSHIBA EXT",
        )
        with _mountinfo_patch(MOUNTINFO_WITH_TOSHIBA):
            with patch("storage_probe.run_command", side_effect=_mock_df):
                with patch("storage_probe.run_host_command", side_effect=_mock_findmnt):
                    with patch("storage_probe.systemctl_is_active", side_effect=_mock_systemctl):
                        with patch("storage_probe._lookup_uuid", return_value=("0846863B46862A10", None)):
                            with patch("storage_probe._lookup_label", return_value="TOSHIBA EXT"):
                                with patch.object(Path, "exists", return_value=True):
                                    result = probe_expected_drive(drive, root_source="/dev/sda2")
        assert result.status in ("OK", "OK_AUTOMOUNTED")
        assert result.actual_source == "/dev/sdb1"
        assert result.mounted is True

    def test_portable_beast_root_fallback_not_mounted(self):
        drive = ExpectedDrive(
            name="portable_beast",
            path="/mnt/storage/portable_beast",
            legacy=True,
            required=False,
        )
        with _mountinfo_patch(MOUNTINFO_ROOT):
            with patch("storage_probe.run_command", side_effect=_mock_df):
                with patch("storage_probe.run_host_command", return_value=(False, "")):
                    with patch("storage_probe.systemctl_is_active", return_value=(True, "unknown")):
                        with patch.object(Path, "exists", return_value=True):
                            result = probe_expected_drive(drive, root_source="/dev/sda2")
        assert result.status == "LEGACY_PATH"
        assert result.mounted is False
        assert result.actual_source == "/dev/sda2"
        assert "not mounted" in result.message.lower()
        assert "pelican_backup" in result.message

    def test_pelican_backup_autofs_waiting(self):
        drive = ExpectedDrive(
            name="Pelican Backup",
            path="/mnt/storage/pelican_backup",
            expected_uuid="b6c0bc2c-5564-4615-bab6-2ff0ded11bbc",
            expected_fstype="ext4",
            required=False,
        )
        with _mountinfo_patch(MOUNTINFO_AUTOFS_PELICAN):
            with patch("storage_probe.run_command", side_effect=_mock_df):
                with patch("storage_probe.run_host_command", side_effect=_mock_findmnt):
                    with patch("storage_probe.systemctl_is_active", side_effect=_mock_systemctl):
                        with patch.object(Path, "exists", return_value=True):
                            result = probe_expected_drive(drive, root_source="/dev/sda2")
        assert result.status == "AUTOMOUNT_WAITING"
        assert result.mounted is False
        assert result.automount_unit_state == "active"
        assert result.mount_unit_state == "inactive"
        assert result.size is None

    def test_missing_path(self):
        drive = ExpectedDrive(
            name="Raven NVME",
            path="/mnt/storage/raven_nvme",
            required=False,
        )
        with _mountinfo_patch(MOUNTINFO_ROOT):
            with patch("storage_probe.run_command", return_value=(False, "no such file")):
                with patch("storage_probe.run_host_command", return_value=(False, "")):
                    with patch("storage_probe.systemctl_is_active", side_effect=_mock_systemctl):
                        with patch.object(Path, "exists", return_value=False):
                            result = probe_expected_drive(drive, root_source="/dev/sda2")
        assert result.status == "PATH_MISSING"
        assert result.path_exists is False

    def test_uuid_mismatch(self):
        drive = ExpectedDrive(
            name="MicroSD",
            path="/mnt/storage/microsd",
            expected_uuid="deadbeef-dead-beef-dead-beefdeadbeef",
            expected_fstype="ext4",
        )
        with _mountinfo_patch(MOUNTINFO_WITH_MICROSD):
            with patch("storage_probe.run_command", side_effect=_mock_df):
                with patch("storage_probe.run_host_command", side_effect=_mock_findmnt):
                    with patch("storage_probe.systemctl_is_active", side_effect=_mock_systemctl):
                        with patch("storage_probe._lookup_uuid", return_value=("ff481ad2-e9bd-4868-8c8c-6729a461e4b4", None)):
                            with patch("storage_probe._lookup_label", return_value="SK256"):
                                with patch.object(Path, "exists", return_value=True):
                                    result = probe_expected_drive(drive, root_source="/dev/sda2")
        assert result.status == "UUID_MISMATCH"
        assert result.mounted is False

    def test_df_timeout_error(self):
        drive = ExpectedDrive(
            name="MicroSD",
            path="/mnt/storage/microsd",
            expected_uuid="ff481ad2-e9bd-4868-8c8c-6729a461e4b4",
            expected_fstype="ext4",
        )

        def timeout_df(args, **_kwargs):
            if _command_path(args) == drive.path:
                return False, "timed out"
            return _mock_df(args, **_kwargs)

        with _mountinfo_patch(MOUNTINFO_WITH_MICROSD):
            with patch("storage_probe.run_command", side_effect=timeout_df):
                with patch("storage_probe.run_host_command", side_effect=_mock_findmnt):
                    with patch("storage_probe.systemctl_is_active", side_effect=_mock_systemctl):
                        with patch("storage_probe._lookup_uuid", return_value=("ff481ad2-e9bd-4868-8c8c-6729a461e4b4", None)):
                            with patch("storage_probe._lookup_label", return_value="SK256"):
                                with patch.object(Path, "exists", return_value=True):
                                    result = probe_expected_drive(drive, root_source="/dev/sda2")
        assert result.status == "ERROR"
        assert "timed out" in result.message.lower()

    def test_stale_mount(self):
        drive = ExpectedDrive(
            name="Toshiba EXT",
            path="/mnt/storage/toshiba_ext",
            expected_uuid="0846863B46862A10",
            expected_fstype="ntfs3",
        )

        def stale_df(args, **_kwargs):
            if _command_path(args) == drive.path:
                return False, "transport endpoint is not connected"
            return _mock_df(args, **_kwargs)

        with _mountinfo_patch(MOUNTINFO_WITH_TOSHIBA):
            with patch("storage_probe.run_command", side_effect=stale_df):
                with patch("storage_probe.run_host_command", side_effect=_mock_findmnt):
                    with patch("storage_probe.systemctl_is_active", side_effect=_mock_systemctl):
                        with patch.object(Path, "exists", return_value=True):
                            result = probe_expected_drive(drive, root_source="/dev/sda2")
        assert result.status == "STALE_MOUNT"

    def test_not_mounted_parent_root_for_active_path(self):
        drive = ExpectedDrive(
            name="Pelican Backup",
            path="/mnt/storage/pelican_backup",
            expected_uuid="b6c0bc2c-5564-4615-bab6-2ff0ded11bbc",
            required=False,
        )
        with _mountinfo_patch(MOUNTINFO_ROOT):
            with patch("storage_probe.run_command", side_effect=_mock_df):
                with patch("storage_probe.run_host_command", return_value=(False, "")):
                    with patch("storage_probe.systemctl_is_active", side_effect=_mock_systemctl):
                        with patch.object(Path, "exists", return_value=True):
                            result = probe_expected_drive(drive, root_source="/dev/sda2")
        assert result.status == "AUTOMOUNT_WAITING"

    def test_get_storage_status_never_raises(self):
        with patch("storage_probe._root_source", side_effect=RuntimeError("boom")):
            results = get_storage_status((ExpectedDrive(name="x", path="/mnt/storage/x"),))
        assert len(results) == 1

        with patch("storage_probe.probe_expected_drive", side_effect=RuntimeError("boom")):
            drives = (ExpectedDrive(name="x", path="/mnt/storage/x"),)
            results = get_storage_status(drives)
        assert len(results) == 1
        assert results[0].status == "ERROR"


class TestStorageDisplayClass:
    def test_green_for_ok(self):
        assert status_display_class("OK", required=True) == "ok"

    def test_yellow_for_optional_waiting(self):
        assert status_display_class("AUTOMOUNT_WAITING", required=False) == "warn"

    def test_red_for_parent_root(self):
        assert status_display_class("NOT_MOUNTED_PARENT_ROOT", required=False) == "bad"
