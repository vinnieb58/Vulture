"""Static validation for repo-tracked systemd unit files."""

from __future__ import annotations

from configparser import ConfigParser
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SYSTEMD_DIR = REPO_ROOT / "deploy" / "systemd"

FINCH_API_UNIT = SYSTEMD_DIR / "finch-api.service"
FINCH_WHATSAPP_UNIT = SYSTEMD_DIR / "finch-whatsapp.service"
FINCH_TELEGRAM_UNIT = SYSTEMD_DIR / "finch-telegram.service"
FINCH_EXEC_START = (
    "/home/vinnieb58/projects/vulture/.venv/bin/python "
    "-m uvicorn finch.api:app --host 127.0.0.1 --port 8091"
)
FINCH_WHATSAPP_EXEC_START = (
    "/home/vinnieb58/projects/vulture/.venv/bin/python "
    "-m uvicorn finch_whatsapp.app:app --host 127.0.0.1 --port 8092"
)
FINCH_TELEGRAM_EXEC_START = (
    "/home/vinnieb58/projects/vulture/.venv/bin/python -m finch_telegram"
)
RAVEN_APP_DIR = "/home/vinnieb58/projects/vulture"


def _read_unit(path: Path) -> ConfigParser:
    parser = ConfigParser(interpolation=None)
    parser.optionxform = str  # preserve key casing
    parser.read(path)
    return parser


def test_finch_api_service_exists() -> None:
    assert FINCH_API_UNIT.is_file()


def test_finch_api_service_required_fields() -> None:
    unit = _read_unit(FINCH_API_UNIT)

    assert unit.get("Service", "User") == "vinnieb58"
    assert unit.get("Service", "WorkingDirectory") == RAVEN_APP_DIR
    assert unit.get("Service", "EnvironmentFile") == f"{RAVEN_APP_DIR}/.env"
    assert unit.get("Service", "ExecStart") == FINCH_EXEC_START
    assert unit.get("Service", "Restart") == "on-failure"


def test_finch_api_service_binds_localhost_only() -> None:
    unit = _read_unit(FINCH_API_UNIT)
    exec_start = unit.get("Service", "ExecStart")

    assert "127.0.0.1" in exec_start
    assert "0.0.0.0" not in exec_start
    assert "--port 8091" in exec_start


def test_finch_api_service_enabled_on_boot() -> None:
    unit = _read_unit(FINCH_API_UNIT)
    assert unit.get("Install", "WantedBy") == "multi-user.target"


def test_finch_whatsapp_service_exists() -> None:
    assert FINCH_WHATSAPP_UNIT.is_file()


def test_finch_whatsapp_service_required_fields() -> None:
    unit = _read_unit(FINCH_WHATSAPP_UNIT)

    assert unit.get("Service", "User") == "vinnieb58"
    assert unit.get("Service", "WorkingDirectory") == RAVEN_APP_DIR
    assert unit.get("Service", "EnvironmentFile") == f"{RAVEN_APP_DIR}/.env"
    assert unit.get("Service", "ExecStart") == FINCH_WHATSAPP_EXEC_START
    assert unit.get("Service", "Restart") == "on-failure"
    assert unit.get("Unit", "Requires") == "finch-api.service"


def test_finch_whatsapp_service_binds_localhost_only() -> None:
    unit = _read_unit(FINCH_WHATSAPP_UNIT)
    exec_start = unit.get("Service", "ExecStart")

    assert "127.0.0.1" in exec_start
    assert "0.0.0.0" not in exec_start
    assert "--port 8092" in exec_start


def test_finch_whatsapp_service_enabled_on_boot() -> None:
    unit = _read_unit(FINCH_WHATSAPP_UNIT)
    assert unit.get("Install", "WantedBy") == "multi-user.target"


def test_finch_telegram_service_exists() -> None:
    assert FINCH_TELEGRAM_UNIT.is_file()


def test_finch_telegram_service_required_fields() -> None:
    unit = _read_unit(FINCH_TELEGRAM_UNIT)

    assert unit.get("Service", "User") == "vinnieb58"
    assert unit.get("Service", "WorkingDirectory") == RAVEN_APP_DIR
    assert unit.get("Service", "EnvironmentFile") == f"{RAVEN_APP_DIR}/.env"
    assert unit.get("Service", "ExecStart") == FINCH_TELEGRAM_EXEC_START
    assert unit.get("Service", "Restart") == "on-failure"
    assert unit.get("Unit", "Requires") == "finch-api.service"


def test_finch_telegram_service_enabled_on_boot() -> None:
    unit = _read_unit(FINCH_TELEGRAM_UNIT)
    assert unit.get("Install", "WantedBy") == "multi-user.target"
