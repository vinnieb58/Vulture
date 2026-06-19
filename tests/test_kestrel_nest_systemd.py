"""Tests for Kestrel Nest systemd poll timer and failure-safe snapshot writes."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PROBE_FILE = ROOT / "experiments" / "kestrel" / "nest_probe.py"
_spec = importlib.util.spec_from_file_location("nest_probe", PROBE_FILE)
assert _spec and _spec.loader
probe_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(probe_module)

from kestrel.nest import NestApiError, NestConfig, NestConfigError  # noqa: E402

DEPLOY_DIR = ROOT / "deploy" / "systemd"
SERVICE_FILE = DEPLOY_DIR / "kestrel-nest-poll.service"
TIMER_FILE = DEPLOY_DIR / "kestrel-nest-poll.timer"
DOCS_FILE = ROOT / "docs" / "current" / "NEST_THERMOSTAT_INTEGRATION.md"
INSTALL_SCRIPT = ROOT / "scripts" / "install_kestrel_nest_timer.sh"


def _nest_config(tmp_path: Path) -> NestConfig:
    return NestConfig(
        project_id="616e2a03-0969-424c-b5ac-1a8ba461e0be",
        client_id="client-id",
        client_secret="client-secret",
        refresh_token="refresh-token",
        output_path=str(tmp_path / "kestrel_nest_status.json"),
    )


class TestNestProbeFailurePreservesSnapshot:
    def test_successful_poll_appends_history(self, tmp_path: Path) -> None:
        config = _nest_config(tmp_path)
        output_path = Path(config.output_path)
        history_path = tmp_path / "kestrel_nest_history.jsonl"
        snapshot = {
            "updated_at": "2026-06-19T12:00:00+00:00",
            "thermostats": {
                "downstairs": {
                    "name": "Downstairs",
                    "temperature": 72,
                    "action": "COOLING",
                    "online": True,
                }
            },
        }

        with (
            patch.object(sys, "argv", ["nest_probe", "--once"]),
            patch.object(probe_module, "load_nest_config", return_value=config),
            patch.object(probe_module, "setup_logging"),
            patch.object(probe_module, "poll_nest_thermostats", return_value=snapshot),
            patch.object(probe_module, "append_history_from_snapshot") as append_mock,
        ):
            code = probe_module.main()

        assert code == 0
        append_mock.assert_called_once_with(snapshot)
        assert output_path.is_file()

    def test_api_failure_does_not_overwrite_existing_snapshot(self, tmp_path: Path) -> None:
        config = _nest_config(tmp_path)
        output_path = Path(config.output_path)
        existing = {
            "updated_at": "2026-06-19T10:00:00+00:00",
            "thermostats": {"downstairs": {"name": "Downstairs", "temperature": 73}},
        }
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(existing, indent=2) + "\n", encoding="utf-8")

        with (
            patch.object(sys, "argv", ["nest_probe", "--once"]),
            patch.object(probe_module, "load_nest_config", return_value=config),
            patch.object(probe_module, "setup_logging"),
            patch.object(
                probe_module,
                "poll_nest_thermostats",
                side_effect=NestApiError("OAuth token request failed: HTTP 401"),
            ),
        ):
            code = probe_module.main()

        assert code == 1
        assert json.loads(output_path.read_text(encoding="utf-8")) == existing


class TestNestSystemdUnits:
    def test_service_unit_has_required_fields(self) -> None:
        text = SERVICE_FILE.read_text(encoding="utf-8")
        assert "Type=oneshot" in text
        assert "WorkingDirectory=/home/vinnieb58/projects/vulture" in text
        assert "EnvironmentFile=/home/vinnieb58/projects/vulture/.env" in text
        assert "nest_probe.py --once" in text
        assert "User=vinnieb58" in text
        assert "Group=vinnieb58" in text
        assert "NoNewPrivileges=true" in text
        assert "TimeoutStartSec=120" in text
        assert "Restart=no" in text

    def test_timer_unit_polls_every_five_minutes(self) -> None:
        text = TIMER_FILE.read_text(encoding="utf-8")
        assert "OnUnitActiveSec=5min" in text
        assert "Persistent=true" in text
        assert "Unit=kestrel-nest-poll.service" in text
        assert "WantedBy=timers.target" in text

    def test_units_do_not_reference_secrets(self) -> None:
        combined = SERVICE_FILE.read_text(encoding="utf-8") + TIMER_FILE.read_text(encoding="utf-8")
        for forbidden in (
            "NEST_GOOGLE_CLIENT_SECRET",
            "NEST_GOOGLE_REFRESH_TOKEN",
            "ya29.",
            "client_secret=",
            "refresh_token=",
        ):
            assert forbidden.lower() not in combined.lower()


class TestNestOperatorDocs:
    def test_integration_doc_has_install_status_journal_and_manual_run(self) -> None:
        text = DOCS_FILE.read_text(encoding="utf-8")
        assert "kestrel-nest-poll.service" in text
        assert "kestrel-nest-poll.timer" in text
        assert "install_kestrel_nest_timer.sh" in text
        assert "systemctl enable --now kestrel-nest-poll.timer" in text
        assert "systemctl status kestrel-nest-poll.timer" in text
        assert "journalctl -u kestrel-nest-poll.service" in text
        assert "nest_probe.py --once" in text
        assert "does not overwrite" in text.lower()
        for forbidden in ("NEST_GOOGLE_CLIENT_SECRET=", "ya29.a0", "refresh_token=1//"):
            assert forbidden not in text

    def test_install_script_references_unit_files(self) -> None:
        text = INSTALL_SCRIPT.read_text(encoding="utf-8")
        assert "cat .env" not in text
        assert "NEST_GOOGLE_CLIENT_SECRET" not in text
        assert "kestrel-nest-poll.service" in text
        assert "kestrel-nest-poll.timer" in text

    def test_install_script_is_executable(self) -> None:
        assert INSTALL_SCRIPT.exists()
        assert INSTALL_SCRIPT.stat().st_mode & 0o111
