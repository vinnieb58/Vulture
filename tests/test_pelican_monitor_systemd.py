"""Static validation for Pelican monitor systemd units."""

from __future__ import annotations

from configparser import ConfigParser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEPLOY_DIR = ROOT / "deploy" / "systemd"
SERVICE_FILE = DEPLOY_DIR / "pelican-monitor.service"
TIMER_FILE = DEPLOY_DIR / "pelican-monitor.timer"
DOCS_FILE = ROOT / "docs" / "current" / "PELiCAN_BACKUP.md"
INSTALL_SCRIPT = ROOT / "scripts" / "install_pelican_monitor_timer.sh"
RAVEN_APP_DIR = "/home/vinnieb58/projects/vulture"
EXEC_START = f"{RAVEN_APP_DIR}/scripts/pelican_monitor.sh"


def _read_unit(path: Path) -> ConfigParser:
    parser = ConfigParser(interpolation=None)
    parser.optionxform = str
    parser.read(path)
    return parser


class TestPelicanMonitorServiceUnit:
    def test_service_unit_exists(self) -> None:
        assert SERVICE_FILE.is_file()

    def test_service_user_and_working_directory(self) -> None:
        unit = _read_unit(SERVICE_FILE)
        assert unit.get("Service", "User") == "vinnieb58"
        assert unit.get("Service", "Group") == "vinnieb58"
        assert unit.get("Service", "WorkingDirectory") == RAVEN_APP_DIR

    def test_service_execstart_uses_monitor_script(self) -> None:
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
        assert "SyslogIdentifier=pelican-monitor" in text

    def test_service_not_enabled_for_boot_directly(self) -> None:
        unit = _read_unit(SERVICE_FILE)
        assert not unit.has_section("Install")

    def test_service_loads_optional_env_file(self) -> None:
        text = SERVICE_FILE.read_text(encoding="utf-8")
        assert "EnvironmentFile=-/home/vinnieb58/projects/vulture/.env" in text

    def test_service_does_not_embed_webhook_secrets(self) -> None:
        text = SERVICE_FILE.read_text(encoding="utf-8")
        assert "DISCORD_WEBHOOK" not in text
        assert "webhooks/" not in text


class TestPelicanMonitorTimerUnit:
    def test_timer_unit_exists(self) -> None:
        assert TIMER_FILE.is_file()

    def test_timer_runs_every_six_hours_with_persistent(self) -> None:
        text = TIMER_FILE.read_text(encoding="utf-8")
        assert "OnCalendar=*-*-* 00,06,12,18:00:00" in text
        assert "Persistent=true" in text
        assert "RandomizedDelaySec=15m" in text
        assert "Unit=pelican-monitor.service" in text

    def test_timer_enabled_via_timers_target(self) -> None:
        unit = _read_unit(TIMER_FILE)
        assert unit.get("Install", "WantedBy") == "timers.target"


class TestPelicanMonitorInstallScript:
    def test_install_script_references_units_without_running_check(self) -> None:
        text = INSTALL_SCRIPT.read_text(encoding="utf-8")
        assert "pelican-monitor.service" in text
        assert "pelican-monitor.timer" in text
        assert "systemctl enable --now" in text
        assert "systemctl start pelican-monitor.service" not in text.split("Manual one-shot")[0]

    def test_install_script_is_executable(self) -> None:
        assert INSTALL_SCRIPT.exists()
        assert INSTALL_SCRIPT.stat().st_mode & 0o111

    def test_docs_cover_monitor_systemd(self) -> None:
        text = DOCS_FILE.read_text(encoding="utf-8")
        assert "pelican-monitor.service" in text
        assert "pelican-monitor.timer" in text
        assert "backup_monitor_status.json" in text
