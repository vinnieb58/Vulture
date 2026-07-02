"""Static validation for Vulture concert watch systemd units."""

from __future__ import annotations

import os
import subprocess
import sys
from configparser import ConfigParser
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SYSTEMD_DIR = REPO_ROOT / "deploy" / "systemd"
SERVICE = SYSTEMD_DIR / "vulture-concert-watches.service"
TIMER = SYSTEMD_DIR / "vulture-concert-watches.timer"
INSTALL_SCRIPT = REPO_ROOT / "scripts" / "install_concert_watches_timer.sh"
RAVEN_APP_DIR = "/home/vinnieb58/projects/vulture"
VENV_PYTHON = f"{RAVEN_APP_DIR}/.venv/bin/python"
EXEC_START = f"{VENV_PYTHON} scripts/run_concert_watches.py"
PYTHONPATH_ENV = f"PYTHONPATH={RAVEN_APP_DIR}"


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
    assert unit.get("Service", "Environment") == PYTHONPATH_ENV
    assert unit.get("Service", "ExecStart") == EXEC_START
    # Script path is relative to WorkingDirectory (same pattern as vulture-bot.service).
    assert unit.get("Service", "ExecStart").endswith(" scripts/run_concert_watches.py")
    assert VENV_PYTHON in unit.get("Service", "ExecStart")


def test_concert_watch_timer_triggers_service() -> None:
    unit = _read_unit(TIMER)
    assert unit.get("Timer", "Unit") == "vulture-concert-watches.service"
    assert unit.get("Install", "WantedBy") == "timers.target"


def test_install_script_exists() -> None:
    assert INSTALL_SCRIPT.is_file()
    text = INSTALL_SCRIPT.read_text(encoding="utf-8")
    assert "vulture-concert-watches.timer" in text
    assert "--enable" in text


def test_run_concert_watches_subprocess_from_repo_root() -> None:
    """Run entrypoint the way systemd does: cwd=repo root, no PYTHONPATH."""
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}

    result = subprocess.run(
        [sys.executable, "scripts/run_concert_watches.py"],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )

    combined = result.stdout + result.stderr
    assert "ModuleNotFoundError" not in combined
    assert "No module named 'engine'" not in combined
    assert result.returncode in (0, 1), combined
