"""Static validation for Vulture concert watch systemd units."""

from __future__ import annotations

from configparser import ConfigParser
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SYSTEMD_DIR = REPO_ROOT / "deploy" / "systemd"
SERVICE = SYSTEMD_DIR / "vulture-concert-watches.service"
TIMER = SYSTEMD_DIR / "vulture-concert-watches.timer"
INSTALL_SCRIPT = REPO_ROOT / "scripts" / "install_concert_watches_timer.sh"
RAVEN_APP_DIR = "/home/vinnieb58/projects/vulture"
EXEC_START = (
    "/home/vinnieb58/projects/vulture/.venv/bin/python "
    "/home/vinnieb58/projects/vulture/scripts/run_concert_watches.py"
)


def _read_unit(path: Path) -> ConfigParser:
    parser = ConfigParser(interpolation=None)
    parser.optionxform = str
    parser.read(path)
    return parser


def test_concert_watch_service_exists() -> None:
    assert SERVICE.is_file()


def test_concert_watch_timer_exists() -> None:
    assert TIMER.is_file()


def test_concert_watch_service_fields() -> None:
    unit = _read_unit(SERVICE)
    assert unit.get("Service", "Type") == "oneshot"
    assert unit.get("Service", "User") == "vinnieb58"
    assert unit.get("Service", "WorkingDirectory") == RAVEN_APP_DIR
    assert unit.get("Service", "EnvironmentFile") == f"{RAVEN_APP_DIR}/.env"
    assert unit.get("Service", "ExecStart") == EXEC_START


def test_concert_watch_timer_triggers_service() -> None:
    unit = _read_unit(TIMER)
    assert unit.get("Timer", "Unit") == "vulture-concert-watches.service"
    assert unit.get("Install", "WantedBy") == "timers.target"


def test_install_script_exists() -> None:
    assert INSTALL_SCRIPT.is_file()
    text = INSTALL_SCRIPT.read_text(encoding="utf-8")
    assert "vulture-concert-watches.timer" in text
    assert "--enable" in text
