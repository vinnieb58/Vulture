"""Tests for the House dashboard card (Nest thermostat snapshot)."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
DASHBOARD_DIR = ROOT / "dashboard"
sys.path.insert(0, str(DASHBOARD_DIR))

import app as dashboard_app  # noqa: E402
from house_formatting import format_house_card_display, format_summary  # noqa: E402
from house_status import read_house_status  # noqa: E402
from nest_error_status import read_nest_poll_error  # noqa: E402

FIXED_NOW = datetime(2026, 6, 19, 12, 2, tzinfo=timezone.utc)


def _recent_updated_at(minutes_ago: int = 2) -> str:
    return (FIXED_NOW - timedelta(minutes=minutes_ago)).replace(microsecond=0).isoformat()


def _write_nest_snapshot(path: Path, **overrides: object) -> None:
    payload = {
        "updated_at": _recent_updated_at(2),
        "thermostats": {
            "downstairs": {
                "name": "Downstairs",
                "temperature": 72,
                "humidity": 65,
                "mode": "COOL",
                "action": "COOLING",
                "online": True,
            },
            "upstairs": {
                "name": "Upstairs",
                "temperature": 77,
                "humidity": 65,
                "mode": "MANUAL_ECO",
                "action": "OFF",
                "online": True,
            },
        },
    }
    payload.update(overrides)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _write_nest_error(path: Path, **overrides: object) -> None:
    payload = {
        "timestamp": _recent_updated_at(1),
        "error_type": "oauth",
        "message": 'OAuth token request failed: {"error":"invalid_grant"}',
        "last_success": _recent_updated_at(20),
    }
    payload.update(overrides)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


class TestHouseFormatting:
    def test_summary_prefers_active_hvac_action(self) -> None:
        assert format_summary("COOL", "COOLING") == "Cooling"

    def test_summary_shows_eco_for_manual_eco(self) -> None:
        assert format_summary("MANUAL_ECO", "OFF") == "Eco"


class TestReadHouseStatus:
    def test_valid_snapshot_is_available(self, tmp_path: Path, monkeypatch) -> None:
        path = tmp_path / "kestrel_nest_status.json"
        _write_nest_snapshot(path)
        monkeypatch.setattr("house_status.NEST_STATUS_PATH", path)

        house = read_house_status(now=FIXED_NOW)

        assert house["state"] == "available"
        assert len(house["thermostats"]) == 2
        assert house["thermostats"][0]["name"] == "Downstairs"
        assert house["thermostats"][1]["name"] == "Upstairs"
        assert house["age_minutes"] == 2

    def test_missing_snapshot_is_no_data(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr("house_status.NEST_STATUS_PATH", tmp_path / "missing.json")

        house = read_house_status(now=FIXED_NOW)

        assert house["state"] == "no_data"
        assert house["headline"] == "Nest data unavailable"
        assert house["thermostats"] == []

    def test_stale_snapshot_warns(self, tmp_path: Path, monkeypatch) -> None:
        path = tmp_path / "kestrel_nest_status.json"
        _write_nest_snapshot(path, updated_at=_recent_updated_at(20))
        monkeypatch.setattr("house_status.NEST_STATUS_PATH", path)

        house = read_house_status(now=FIXED_NOW)

        assert house["state"] == "stale"
        assert house["age_minutes"] == 20
        assert "stale" in house["headline"].lower()

    def test_stale_snapshot_with_auth_error_shows_auth_failure(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        status_path = tmp_path / "kestrel_nest_status.json"
        error_path = tmp_path / "kestrel_nest_error.json"
        _write_nest_snapshot(status_path, updated_at=_recent_updated_at(20))
        _write_nest_error(error_path)
        monkeypatch.setattr("house_status.NEST_STATUS_PATH", status_path)
        monkeypatch.setattr("nest_error_status.NEST_ERROR_PATH", error_path)

        house = read_house_status(now=FIXED_NOW)
        card = format_house_card_display(house, now=FIXED_NOW)

        assert house["state"] == "auth_failure"
        assert house["warning"] == "Nest auth failure"
        assert card["status"] == "Auth failure"
        assert card["style"] == "fail"

    def test_stale_snapshot_with_api_error_shows_nest_stale(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        status_path = tmp_path / "kestrel_nest_status.json"
        error_path = tmp_path / "kestrel_nest_error.json"
        _write_nest_snapshot(status_path, updated_at=_recent_updated_at(20))
        _write_nest_error(
            error_path,
            error_type="api",
            message="Nest SDM devices request failed: HTTP 503",
        )
        monkeypatch.setattr("house_status.NEST_STATUS_PATH", status_path)
        monkeypatch.setattr("nest_error_status.NEST_ERROR_PATH", error_path)

        house = read_house_status(now=FIXED_NOW)

        assert house["state"] == "stale"
        assert house["warning"] == "Nest stale"

    def test_empty_thermostat_list_is_no_data(self, tmp_path: Path, monkeypatch) -> None:
        path = tmp_path / "kestrel_nest_status.json"
        _write_nest_snapshot(path, thermostats={})
        monkeypatch.setattr("house_status.NEST_STATUS_PATH", path)

        house = read_house_status(now=FIXED_NOW)

        assert house["state"] == "no_data"
        assert house["thermostats"] == []
        assert house["warning"] == "No Nest thermostats in snapshot"

    def test_card_display_for_valid_snapshot(self, tmp_path: Path, monkeypatch) -> None:
        path = tmp_path / "kestrel_nest_status.json"
        _write_nest_snapshot(path)
        monkeypatch.setattr("house_status.NEST_STATUS_PATH", path)

        house = read_house_status(now=FIXED_NOW)
        card = format_house_card_display(house, now=FIXED_NOW)

        assert card["style"] == "ok"
        assert card["updated_display"] == "Updated 2 minutes ago"
        downstairs = card["thermostats"][0]
        assert downstairs["name"] == "Downstairs"
        assert downstairs["metrics_line"] == "72°F · 65%"
        assert downstairs["summary"] == "Cooling"
        upstairs = card["thermostats"][1]
        assert upstairs["summary"] == "Eco"


class TestHouseDashboardHTTP:
    @pytest.fixture
    def client(self, tmp_path, monkeypatch):
        db_path = tmp_path / "missing.db"
        log_path = tmp_path / "missing.log"
        monkeypatch.setattr(dashboard_app, "DB_PATH", db_path)
        monkeypatch.setattr(dashboard_app, "LOG_PATH", log_path)
        monkeypatch.setattr("db_readers.DB_PATH", db_path)
        monkeypatch.setattr("log_readers.LOG_PATH", log_path)
        monkeypatch.setattr("vulture_runtime.LOG_PATH", log_path)
        return TestClient(dashboard_app.app)

    def _stub_host(self, client):
        with patch("host_status.run_host_command", return_value=(False, "unavailable")):
            with patch("host_status.run_systemctl", return_value=(False, "unavailable")):
                return client

    def test_home_renders_house_card_with_valid_snapshot(
        self, client, tmp_path, monkeypatch
    ) -> None:
        nest_path = tmp_path / "kestrel_nest_status.json"
        _write_nest_snapshot(nest_path)
        monkeypatch.setattr("house_status.NEST_STATUS_PATH", nest_path)
        monkeypatch.setattr("kestrel_status.KESTREL_STATUS_PATH", tmp_path / "missing.json")
        monkeypatch.setattr("kestrel_metrics.KESTREL_DB_PATH", tmp_path / "missing.db")
        monkeypatch.setattr(
            dashboard_app,
            "read_house_status",
            lambda: read_house_status(now=FIXED_NOW),
        )

        response = self._stub_host(client).get("/")

        assert response.status_code == 200
        text = response.text
        assert "House" in text
        assert "Downstairs" in text
        assert "Upstairs" in text
        assert "72°F" in text
        assert "77°F" in text
        assert "Cooling" in text
        assert "Eco" in text
        assert "Updated 2 minutes ago" in text

    def test_home_missing_snapshot_shows_unavailable(self, client, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr("house_status.NEST_STATUS_PATH", tmp_path / "missing.json")
        monkeypatch.setattr("kestrel_status.KESTREL_STATUS_PATH", tmp_path / "missing.json")
        monkeypatch.setattr("kestrel_metrics.KESTREL_DB_PATH", tmp_path / "missing.db")

        response = self._stub_host(client).get("/")

        assert response.status_code == 200
        assert "Nest data unavailable" in response.text

    def test_home_stale_snapshot_shows_warning_state(self, client, tmp_path, monkeypatch) -> None:
        nest_path = tmp_path / "kestrel_nest_status.json"
        _write_nest_snapshot(nest_path, updated_at=_recent_updated_at(20))
        monkeypatch.setattr("house_status.NEST_STATUS_PATH", nest_path)
        monkeypatch.setattr("kestrel_status.KESTREL_STATUS_PATH", tmp_path / "missing.json")
        monkeypatch.setattr("kestrel_metrics.KESTREL_DB_PATH", tmp_path / "missing.db")
        monkeypatch.setattr(
            dashboard_app,
            "read_house_status",
            lambda: read_house_status(now=FIXED_NOW),
        )

        response = self._stub_host(client).get("/")

        assert response.status_code == 200
        text = response.text
        assert "Stale" in text
        assert "Updated 20 minutes ago" in text

    def test_home_auth_failure_renders_warning(self, client, tmp_path, monkeypatch) -> None:
        nest_path = tmp_path / "kestrel_nest_status.json"
        error_path = tmp_path / "kestrel_nest_error.json"
        _write_nest_snapshot(nest_path, updated_at=_recent_updated_at(20))
        _write_nest_error(error_path)
        monkeypatch.setattr("house_status.NEST_STATUS_PATH", nest_path)
        monkeypatch.setattr("nest_error_status.NEST_ERROR_PATH", error_path)
        monkeypatch.setattr("house_status.read_nest_poll_error", read_nest_poll_error)
        monkeypatch.setattr("kestrel_status.KESTREL_STATUS_PATH", tmp_path / "missing.json")
        monkeypatch.setattr("kestrel_metrics.KESTREL_DB_PATH", tmp_path / "missing.db")
        monkeypatch.setattr(
            dashboard_app,
            "read_house_status",
            lambda: read_house_status(now=FIXED_NOW),
        )

        response = self._stub_host(client).get("/")

        assert response.status_code == 200
        text = response.text
        assert "Auth failure" in text
        assert "Nest auth failure" in text

    def test_home_empty_thermostat_list_does_not_break_page(self, client, tmp_path, monkeypatch) -> None:
        nest_path = tmp_path / "kestrel_nest_status.json"
        _write_nest_snapshot(nest_path, thermostats={})
        monkeypatch.setattr("house_status.NEST_STATUS_PATH", nest_path)
        monkeypatch.setattr("kestrel_status.KESTREL_STATUS_PATH", tmp_path / "missing.json")
        monkeypatch.setattr("kestrel_metrics.KESTREL_DB_PATH", tmp_path / "missing.db")

        response = self._stub_host(client).get("/")

        assert response.status_code == 200
        assert "Nest data unavailable" in response.text

    def test_rendered_card_example(self, tmp_path: Path, monkeypatch) -> None:
        """Document the compact House card layout."""
        path = tmp_path / "kestrel_nest_status.json"
        _write_nest_snapshot(path)
        monkeypatch.setattr("house_status.NEST_STATUS_PATH", path)

        card = format_house_card_display(read_house_status(now=FIXED_NOW), now=FIXED_NOW)
        lines = ["HOUSE"]
        for zone in card["thermostats"]:
            temp, humidity = zone["metrics_line"].split(" · ")
            lines.extend([zone["name"], temp, humidity, zone["summary"]])
        if card["updated_display"]:
            lines.append(card["updated_display"])

        rendered = "\n".join(lines)
        assert rendered.splitlines() == [
            "HOUSE",
            "Downstairs",
            "72°F",
            "65%",
            "Cooling",
            "Upstairs",
            "77°F",
            "65%",
            "Eco",
            "Updated 2 minutes ago",
        ]
