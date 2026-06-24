"""Static validation for Pelican twice-daily database snapshot systemd units."""

from __future__ import annotations

from configparser import ConfigParser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEPLOY_DIR = ROOT / "deploy" / "systemd"
SERVICE_FILE = DEPLOY_DIR / "pelican-db-snapshot.service"
TIMER_FILE = DEPLOY_DIR / "pelican-db-snapshot.timer"
DOCS_FILE = ROOT / "docs" / "current" / "PELiCAN_BACKUP.md"
INSTALL_SCRIPT = ROOT / "scripts" / "install_pelican_db_snapshot_timer.sh"
RAVEN_APP_DIR = "/home/vinnieb58/projects/vulture"
EXEC_START = f"{RAVEN_APP_DIR}/scripts/pelican_db_snapshot.sh"


def _read_unit(path: Path) -> ConfigParser:
    parser = ConfigParser(interpolation=None)
    parser.optionxform = str
    parser.read(path)
    return parser


class TestPelicanDbSnapshotServiceUnit:
    def test_service_unit_exists(self) -> None:
        assert SERVICE_FILE.is_file()

    def test_service_user_and_working_directory(self) -> None:
        unit = _read_unit(SERVICE_FILE)
        assert unit.get("Service", "User") == "vinnieb58"
        assert unit.get("Service", "Group") == "vinnieb58"
        assert unit.get("Service", "WorkingDirectory") == RAVEN_APP_DIR

    def test_service_execstart_uses_existing_snapshot_script(self) -> None:
        unit = _read_unit(SERVICE_FILE)
        assert unit.get("Service", "ExecStart") == EXEC_START

    def test_service_is_oneshot_without_restart(self) -> None:
        unit = _read_unit(SERVICE_FILE)
        assert unit.get("Service", "Type") == "oneshot"
        assert unit.get("Service", "Restart") == "no"

    def test_service_logs_to_journald(self) -> None:
        text = SERVICE_FILE.read_text(encoding="utf-8")
        assert "StandardOutput=journal" in text
        assert "StandardError=journal" in text
        assert "SyslogIdentifier=pelican-db-snapshot" in text

    def test_service_not_enabled_for_boot_directly(self) -> None:
        unit = _read_unit(SERVICE_FILE)
        assert not unit.has_section("Install")


class TestPelicanDbSnapshotTimerUnit:
    def test_timer_unit_exists(self) -> None:
        assert TIMER_FILE.is_file()

    def test_timer_twice_daily_schedule_with_randomized_delay(self) -> None:
        text = TIMER_FILE.read_text(encoding="utf-8")
        assert "OnCalendar=*-*-* 03:00:00" in text
        assert "OnCalendar=*-*-* 15:00:00" in text
        assert "Persistent=true" in text
        assert "RandomizedDelaySec=15m" in text
        assert "Unit=pelican-db-snapshot.service" in text

    def test_timer_enabled_via_timers_target(self) -> None:
        text = TIMER_FILE.read_text(encoding="utf-8")
        assert "WantedBy=timers.target" in text


class TestPelicanDbSnapshotOperatorDocs:
    def test_docs_cover_db_snapshot_systemd_operations(self) -> None:
        text = DOCS_FILE.read_text(encoding="utf-8")
        assert "pelican-db-snapshot.service" in text
        assert "pelican-db-snapshot.timer" in text
        assert "raven-db-snapshots" in text
        assert "systemctl enable --now pelican-db-snapshot.timer" in text
        assert "journalctl -u pelican-db-snapshot.service" in text

    def test_install_script_references_units_without_running_backup(self) -> None:
        text = INSTALL_SCRIPT.read_text(encoding="utf-8")
        assert "pelican-db-snapshot.service" in text
        assert "pelican-db-snapshot.timer" in text
        assert "systemctl enable --now" in text
        assert "cat .env" not in text

    def test_install_script_is_executable(self) -> None:
        assert INSTALL_SCRIPT.exists()
        assert INSTALL_SCRIPT.stat().st_mode & 0o111
