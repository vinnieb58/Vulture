"""Tests for Kestrel Tuya power systemd poll timer and failure-safe snapshot writes."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PROBE_FILE = ROOT / "experiments" / "kestrel" / "tuya_power_probe.py"
_spec = importlib.util.spec_from_file_location("tuya_power_probe", PROBE_FILE)
assert _spec and _spec.loader
probe_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(probe_module)

from kestrel.tuya_power import TuyaPowerApiError, TuyaPowerConfig  # noqa: E402

DEPLOY_DIR = ROOT / "deploy" / "systemd"
SERVICE_FILE = DEPLOY_DIR / "kestrel-tuya-power-poll.service"
TIMER_FILE = DEPLOY_DIR / "kestrel-tuya-power-poll.timer"
DOCS_FILE = ROOT / "docs" / "current" / "Kestrel_Tuya_Power_Monitoring.md"
INSTALL_SCRIPT = ROOT / "scripts" / "install_kestrel_tuya_power_timer.sh"


def _tuya_config(tmp_path: Path) -> TuyaPowerConfig:
    return TuyaPowerConfig(
        meters=(),
        output_path=str(tmp_path / "kestrel_tuya_power_status.json"),
        cloud_api_key=None,
        cloud_api_secret=None,
        cloud_region="us",
    )


class TestTuyaPowerProbeFailurePreservesSnapshot:
    def test_successful_poll_appends_history(self, tmp_path: Path) -> None:
        config = _tuya_config(tmp_path)
        output_path = Path(config.output_path)
        snapshot = {
            "updated_at": "2026-06-27T12:00:00+00:00",
            "meters": {"meter_1": {"online": True}},
            "appliances": {"ac_compressor": {"label": "AC compressor", "power_w": 100.0}},
        }
        log = MagicMock()

        with patch.object(probe_module, "poll_tuya_power_meters", return_value=snapshot):
            with patch.object(probe_module, "append_history_from_snapshot") as append_mock:
                code, result = probe_module.execute_poll_once(config, log=log)

        assert code == 0
        assert result == snapshot
        append_mock.assert_called_once_with(snapshot)
        assert output_path.is_file()

    def test_api_failure_does_not_overwrite_existing_snapshot(self, tmp_path: Path) -> None:
        config = _tuya_config(tmp_path)
        output_path = Path(config.output_path)
        existing = {
            "updated_at": "2026-06-27T10:00:00+00:00",
            "appliances": {"ac_compressor": {"power_w": 2500.0}},
        }
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(existing, indent=2) + "\n", encoding="utf-8")
        log = MagicMock()

        with patch.object(
            probe_module,
            "poll_tuya_power_meters",
            side_effect=TuyaPowerApiError("Local read failed for meter_2: connection timeout"),
        ):
            code, result = probe_module.execute_poll_once(config, log=log)

        assert code == 1
        assert result is None
        assert json.loads(output_path.read_text(encoding="utf-8")) == existing

    def test_api_failure_writes_redacted_error_json(self, tmp_path: Path) -> None:
        config = _tuya_config(tmp_path)
        output_path = Path(config.output_path)
        error_path = tmp_path / "kestrel_tuya_power_error.json"
        existing = {
            "updated_at": "2026-06-27T10:00:00+00:00",
            "appliances": {"ac_compressor": {"power_w": 2500.0}},
        }
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(existing, indent=2) + "\n", encoding="utf-8")
        secret = "wizardkey12345678"
        api_error = f"Local read failed local_key={secret}"
        log = MagicMock()

        with patch.object(
            probe_module,
            "poll_tuya_power_meters",
            side_effect=TuyaPowerApiError(api_error),
        ):
            code, _ = probe_module.execute_poll_once(config, log=log)

        assert code == 1
        assert error_path.is_file()
        error = json.loads(error_path.read_text(encoding="utf-8"))
        assert error["error_type"] == "local"
        assert error["last_success"] == "2026-06-27T10:00:00+00:00"
        assert secret not in json.dumps(error)
        assert json.loads(output_path.read_text(encoding="utf-8")) == existing

    def test_successful_poll_clears_error_json(self, tmp_path: Path) -> None:
        config = _tuya_config(tmp_path)
        output_path = Path(config.output_path)
        error_path = tmp_path / "kestrel_tuya_power_error.json"
        error_path.write_text(
            json.dumps(
                {
                    "timestamp": "2026-06-27T11:00:00+00:00",
                    "error_type": "local",
                    "message": "Local read failed",
                }
            )
            + "\n",
            encoding="utf-8",
        )
        snapshot = {
            "updated_at": "2026-06-27T12:00:00+00:00",
            "appliances": {"ac_compressor": {"label": "AC compressor", "power_w": 100.0}},
        }
        log = MagicMock()

        with patch.object(probe_module, "poll_tuya_power_meters", return_value=snapshot):
            with patch.object(probe_module, "append_history_from_snapshot", return_value=True):
                code, _ = probe_module.execute_poll_once(config, log=log)

        assert code == 0
        assert not error_path.exists()
        assert output_path.is_file()


class TestTuyaPowerSystemdUnits:
    def test_service_unit_has_required_fields(self) -> None:
        text = SERVICE_FILE.read_text(encoding="utf-8")
        assert "Type=oneshot" in text
        assert "WorkingDirectory=/home/vinnieb58/projects/vulture" in text
        assert "EnvironmentFile=/home/vinnieb58/projects/vulture/.env" in text
        assert "tuya_power_probe.py --once" in text
        assert ".venv/bin/python experiments/kestrel/tuya_power_probe.py --once" in text
        assert "User=vinnieb58" in text
        assert "Group=vinnieb58" in text
        assert "NoNewPrivileges=true" in text
        assert "TimeoutStartSec=120" in text
        assert "Restart=no" in text

    def test_timer_unit_polls_every_sixty_seconds(self) -> None:
        text = TIMER_FILE.read_text(encoding="utf-8")
        assert "OnUnitActiveSec=60" in text
        assert "Persistent=true" in text
        assert "Unit=kestrel-tuya-power-poll.service" in text
        assert "WantedBy=timers.target" in text

    def test_units_do_not_reference_secrets(self) -> None:
        combined = SERVICE_FILE.read_text(encoding="utf-8") + TIMER_FILE.read_text(encoding="utf-8")
        for forbidden in (
            "TUYA_METER1_LOCAL_KEY",
            "TUYA_CLOUD_API_SECRET",
            "local_key=",
            "wizardkey",
        ):
            assert forbidden.lower() not in combined.lower()


class TestTuyaPowerOperatorDocs:
    def test_monitoring_doc_has_install_status_journal_and_manual_run(self) -> None:
        text = DOCS_FILE.read_text(encoding="utf-8")
        assert "kestrel-tuya-power-poll.service" in text
        assert "kestrel-tuya-power-poll.timer" in text
        assert "install_kestrel_tuya_power_timer.sh" in text
        assert "systemctl enable --now kestrel-tuya-power-poll.timer" in text
        assert "systemctl status kestrel-tuya-power-poll.timer" in text
        assert "journalctl -u kestrel-tuya-power-poll.service" in text
        assert "tuya_power_probe.py --once" in text
        assert "does not overwrite" in text.lower() or "preserved" in text.lower()
        for forbidden in ("TUYA_METER1_LOCAL_KEY=", "wizardkey12345678", "local_key=abcd"):
            assert forbidden not in text

    def test_install_script_references_unit_files(self) -> None:
        text = INSTALL_SCRIPT.read_text(encoding="utf-8")
        assert "cat .env" not in text
        assert "TUYA_METER1_LOCAL_KEY" not in text
        assert "kestrel-tuya-power-poll.service" in text
        assert "kestrel-tuya-power-poll.timer" in text

    def test_install_script_is_executable(self) -> None:
        assert INSTALL_SCRIPT.exists()
        assert INSTALL_SCRIPT.stat().st_mode & 0o111
