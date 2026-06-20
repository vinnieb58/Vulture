"""Static validation for Pelican daily backup systemd units."""

from __future__ import annotations

from configparser import ConfigParser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEPLOY_DIR = ROOT / "deploy" / "systemd"
SERVICE_FILE = DEPLOY_DIR / "pelican-backup.service"
TIMER_FILE = DEPLOY_DIR / "pelican-backup.timer"
DOCS_FILE = ROOT / "docs" / "current" / "PELiCAN_BACKUP.md"
INSTALL_SCRIPT = ROOT / "scripts" / "install_pelican_timer.sh"
RAVEN_APP_DIR = "/home/vinnieb58/projects/vulture"
EXEC_START = f"{RAVEN_APP_DIR}/scripts/pelican_backup.sh"


def _read_unit(path: Path) -> ConfigParser:
    parser = ConfigParser(interpolation=None)
    parser.optionxform = str
    parser.read(path)
    return parser


class TestPelicanBackupServiceUnit:
    def test_service_unit_exists(self) -> None:
        assert SERVICE_FILE.is_file()

    def test_service_user_and_working_directory(self) -> None:
        unit = _read_unit(SERVICE_FILE)
        assert unit.get("Service", "User") == "vinnieb58"
        assert unit.get("Service", "Group") == "vinnieb58"
        assert unit.get("Service", "WorkingDirectory") == RAVEN_APP_DIR

    def test_service_execstart_uses_existing_backup_script(self) -> None:
        unit = _read_unit(SERVICE_FILE)
        assert unit.get("Service", "ExecStart") == EXEC_START

    def test_service_is_oneshot_without_restart(self) -> None:
        unit = _read_unit(SERVICE_FILE)
        assert unit.get("Service", "Type") == "oneshot"
        assert unit.get("Service", "Restart") == "no"

    def test_service_has_restrictive_umask(self) -> None:
        unit = _read_unit(SERVICE_FILE)
        assert unit.get("Service", "UMask") == "0077"

    def test_service_logs_to_journald(self) -> None:
        text = SERVICE_FILE.read_text(encoding="utf-8")
        assert "StandardOutput=journal" in text
        assert "StandardError=journal" in text
        assert "SyslogIdentifier=pelican-backup" in text

    def test_service_not_enabled_for_boot_directly(self) -> None:
        unit = _read_unit(SERVICE_FILE)
        assert not unit.has_section("Install")

    def test_service_has_no_network_online_dependency(self) -> None:
        text = SERVICE_FILE.read_text(encoding="utf-8").lower()
        assert "network-online.target" not in text

    def test_service_does_not_load_env_file(self) -> None:
        text = SERVICE_FILE.read_text(encoding="utf-8")
        assert "EnvironmentFile" not in text


class TestPelicanBackupTimerUnit:
    def test_timer_unit_exists(self) -> None:
        assert TIMER_FILE.is_file()

    def test_timer_daily_schedule_with_randomized_delay(self) -> None:
        text = TIMER_FILE.read_text(encoding="utf-8")
        assert "OnCalendar=*-*-* 03:00:00" in text
        assert "Persistent=true" in text
        assert "RandomizedDelaySec=15m" in text
        assert "Unit=pelican-backup.service" in text

    def test_timer_enabled_via_timers_target(self) -> None:
        unit = _read_unit(TIMER_FILE)
        assert unit.get("Install", "WantedBy") == "timers.target"


class TestPelicanSystemdSecurity:
    def test_units_do_not_reference_secret_values(self) -> None:
        combined = SERVICE_FILE.read_text(encoding="utf-8") + TIMER_FILE.read_text(encoding="utf-8")
        for forbidden in (".env", "DISCORD", "PASSWORD=", "TOKEN="):
            assert forbidden not in combined


class TestPelicanOperatorDocs:
    def test_docs_cover_systemd_operations(self) -> None:
        text = DOCS_FILE.read_text(encoding="utf-8")
        assert "pelican-backup.service" in text
        assert "pelican-backup.timer" in text
        assert "systemctl enable --now pelican-backup.timer" in text
        assert "systemctl list-timers" in text
        assert "journalctl -u pelican-backup.service" in text
        assert "systemctl disable --now pelican-backup.timer" in text
        assert "systemctl start pelican-backup.service" in text

    def test_install_script_references_units_without_running_backup(self) -> None:
        text = INSTALL_SCRIPT.read_text(encoding="utf-8")
        assert "pelican-backup.service" in text
        assert "pelican-backup.timer" in text
        assert "systemctl enable --now" in text
        assert "pelican_backup.sh" not in text or "ExecStart" not in text
        assert "cat .env" not in text

    def test_install_script_is_executable(self) -> None:
        assert INSTALL_SCRIPT.exists()
        assert INSTALL_SCRIPT.stat().st_mode & 0o111
