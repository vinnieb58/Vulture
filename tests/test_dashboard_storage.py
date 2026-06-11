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

MOUNTINFO_STALE_ROOST = MOUNTINFO_WITH_TOSHIBA + """\
39 24 8:65 / /mnt/storage/roost_spinning_0 rw,relatime shared:5 - ext4 /dev/sde1 rw
40 24 0:62 / /mnt/storage/roost_spinning_0 rw,relatime shared:6 - autofs systemd-1 rw
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
        "/mnt/storage/roost_spinning_0": ("systemd-1", "autofs", None),
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


def _path_access_ok(*_args, **_kwargs):
    return True, None


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
                        with patch("storage_probe._path_access_check", side_effect=_path_access_ok):
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
                        with patch("storage_probe._path_access_check", side_effect=_path_access_ok):
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
                        with patch("storage_probe._path_access_check", side_effect=_path_access_ok):
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

    def test_roost_spinning_stale_automount_enodev(self):
        """Broken automount: proc may still list /dev/sde1 but findmnt shows autofs only."""
        drive = ExpectedDrive(
            name="Roost Spinning 0",
            path="/mnt/storage/roost_spinning_0",
            expected_uuid="13dc60fa-ba57-4c18-ac03-22e0ab8d6828",
            expected_fstype="ext4",
            required=False,
        )

        def roost_df(args, **_kwargs):
            if _command_path(args) == drive.path:
                return False, "df: /mnt/storage/roost_spinning_0: No such device"
            return _mock_df(args, **_kwargs)

        def roost_access(_path):
            return False, "no such device"

        with _mountinfo_patch(MOUNTINFO_STALE_ROOST):
            with patch("storage_probe.run_command", side_effect=roost_df):
                with patch("storage_probe.run_host_command", side_effect=_mock_findmnt):
                    with patch("storage_probe.systemctl_is_active", side_effect=_mock_systemctl):
                        with patch("storage_probe._path_access_check", side_effect=roost_access):
                            with patch.object(Path, "exists", return_value=True):
                                result = probe_expected_drive(drive, root_source="/dev/sda2")
        assert result.status == "STALE_AUTOMOUNT"
        assert result.mounted is False
        assert result.actual_source is None
        assert "backing device is unavailable" in result.message.lower()
        assert "/dev/sde1" not in (result.message or "")

    def test_roost_autofs_only_waiting(self):
        drive = ExpectedDrive(
            name="Roost Spinning 0",
            path="/mnt/storage/roost_spinning_0",
            required=False,
        )
        mountinfo = MOUNTINFO_WITH_TOSHIBA + """\
41 24 0:63 / /mnt/storage/roost_spinning_0 rw,relatime shared:7 - autofs systemd-1 rw
"""

        def roost_df(args, **_kwargs):
            if _command_path(args) == drive.path:
                return False, "no such file or directory"
            return _mock_df(args, **_kwargs)

        with _mountinfo_patch(mountinfo):
            with patch("storage_probe.run_command", side_effect=roost_df):
                with patch("storage_probe.run_host_command", side_effect=_mock_findmnt):
                    with patch("storage_probe.systemctl_is_active", side_effect=_mock_systemctl):
                        with patch("storage_probe._path_access_check", side_effect=_path_access_ok):
                            with patch.object(Path, "exists", return_value=True):
                                result = probe_expected_drive(drive, root_source="/dev/sda2")
        assert result.status == "AUTOMOUNT_WAITING"
        assert result.mounted is False
        assert result.actual_source is None

    def test_optional_missing_path_warning(self):
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
        assert status_display_class(result.status, required=False) == "bad"

    def test_ordinary_directory_not_mounted(self):
        drive = ExpectedDrive(
            name="Ordinary dir",
            path="/mnt/storage/not_a_mount",
            required=False,
        )

        def root_only_df(args, **_kwargs):
            path = _command_path(args)
            if path == drive.path:
                return (
                    True,
                    "Filesystem      Size  Used Avail Use% Mounted on\n"
                    "/dev/sda2        29G   12G   15G  45% /mnt/storage/not_a_mount\n",
                )
            return _mock_df(args, **_kwargs)

        with _mountinfo_patch(MOUNTINFO_ROOT):
            with patch("storage_probe.run_command", side_effect=root_only_df):
                with patch("storage_probe.run_host_command", return_value=(False, "")):
                    with patch("storage_probe.systemctl_is_active", return_value=(True, "unknown")):
                        with patch("storage_probe._path_access_check", side_effect=_path_access_ok):
                            with patch.object(Path, "exists", return_value=True):
                                result = probe_expected_drive(drive, root_source="/dev/sda2")
        assert result.status == "NOT_MOUNTED_PARENT_ROOT"
        assert result.mounted is False

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

    def test_yellow_for_stale_automount_optional(self):
        assert status_display_class("STALE_AUTOMOUNT", required=False) == "warn"

    def test_red_for_parent_root(self):
        assert status_display_class("NOT_MOUNTED_PARENT_ROOT", required=False) == "bad"


MOUNTINFO_DOCKER_OVERLAY_ROOT = """\
24 0 0:38 / / rw,relatime shared:1 - overlay overlay rw,lowerdir=/var/lib/docker/overlay2/l/ABC:/var/lib/docker/overlay2/l/DEF,upperdir=/var/lib/docker/overlay2/HASH/diff,workdir=/var/lib/docker/overlay2/HASH/work
"""


class TestDashboardWarningFixes:
    """Regression tests for the four warning fixes in the dashboard."""

    def test_docker_overlay_root_no_fstype_mismatch_warning(self):
        # When the dashboard container reads the root fs as overlay, it must not
        # produce an FSTYPE_MISMATCH warning against the expected ext4.
        drive = ExpectedDrive(
            name="Root filesystem",
            path="/host/root",
            expected_source="/dev/sda2",
            expected_fstype="ext4",
            role="root",
            required=True,
        )
        with _mountinfo_patch(MOUNTINFO_DOCKER_OVERLAY_ROOT):
            with patch("storage_probe.run_command", return_value=(False, "")):
                with patch(
                    "storage_probe.run_host_command",
                    return_value=(True, "overlay overlay"),
                ):
                    with patch("storage_probe.systemctl_is_active", return_value=(True, "unknown")):
                        with patch.object(Path, "exists", return_value=True):
                            with patch("storage_probe._lookup_uuid", return_value=(None, None)):
                                with patch("storage_probe._lookup_label", return_value=None):
                                    result = probe_expected_drive(drive, root_source="/dev/sda2")
        assert result.status != "FSTYPE_MISMATCH", (
            f"Overlay root should not produce FSTYPE_MISMATCH; got status={result.status}"
        )
        assert result.warning is None or "overlay" not in (result.warning or "").lower() or "ext4" not in (result.warning or "").lower()

    def test_portable_beast_legacy_inactive_no_warning(self):
        # portable_beast has legacy=True; LEGACY_PATH status must not propagate as a warning.
        drive = ExpectedDrive(
            name="portable_beast",
            path="/mnt/storage/portable_beast",
            role="storage",
            required=False,
            legacy=True,
        )
        with _mountinfo_patch(MOUNTINFO_ROOT):
            with patch("storage_probe.run_command", side_effect=_mock_df):
                with patch("storage_probe.run_host_command", return_value=(False, "")):
                    with patch("storage_probe.systemctl_is_active", return_value=(True, "unknown")):
                        with patch.object(Path, "exists", return_value=True):
                            result = probe_expected_drive(drive, root_source="/dev/sda2")
        assert result.status == "LEGACY_PATH"
        assert result.warning is None, (
            f"Legacy path must not produce a warning; got: {result.warning!r}"
        )

    def test_toshiba_ext_disk_usage_90pct_warning(self):
        # Toshiba EXT at 90% usage must still generate a warning (threshold preserved).
        drive = ExpectedDrive(
            name="Toshiba EXT",
            path="/mnt/storage/toshiba_ext",
            expected_uuid="0846863B46862A10",
            expected_fstype="ntfs3",
            expected_label="TOSHIBA EXT",
            required=True,
        )

        def df_90pct(args, **_kwargs):
            path = args[-1] if args else ""
            if path == drive.path:
                return (
                    True,
                    "Filesystem      Size  Used Avail Use% Mounted on\n"
                    "/dev/sdb1       1.4T  1.3T   140G  90% /mnt/storage/toshiba_ext\n",
                )
            return _mock_df(args, **_kwargs)

        with _mountinfo_patch(MOUNTINFO_WITH_TOSHIBA):
            with patch("storage_probe.run_command", side_effect=df_90pct):
                with patch("storage_probe.run_host_command", side_effect=_mock_findmnt):
                    with patch("storage_probe.systemctl_is_active", side_effect=_mock_systemctl):
                        with patch("storage_probe._path_access_check", side_effect=_path_access_ok):
                            with patch("storage_probe._lookup_uuid", return_value=("0846863B46862A10", None)):
                                with patch("storage_probe._lookup_label", return_value="TOSHIBA EXT"):
                                    with patch.object(Path, "exists", return_value=True):
                                        result = probe_expected_drive(drive, root_source="/dev/sda2")
        assert result.status in ("OK", "OK_AUTOMOUNTED")
        assert result.percent_used == 90.0
        assert result.warning is not None
        assert "90" in result.warning
        assert result.path == "/mnt/storage/toshiba_ext"
