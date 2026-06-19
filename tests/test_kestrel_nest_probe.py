"""Tests for Kestrel Nest SDM thermostat probe."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kestrel.nest import (
    build_nest_snapshot,
    celsius_to_fahrenheit,
    celsius_to_fahrenheit_rounded,
    extract_display_name,
    format_debug_trait_summary,
    normalize_room_key,
    parse_devices_payload,
    parse_thermostat_device,
    redact_nest_message,
)

FIXTURE_PATH = Path(__file__).resolve().parent / "fixtures" / "nest_sdm_two_thermostats.json"


def _load_fixture() -> dict:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


class TestCelsiusConversion:
    def test_celsius_to_fahrenheit_exact_freezing(self) -> None:
        assert celsius_to_fahrenheit(0) == pytest.approx(32.0)

    def test_celsius_to_fahrenheit_rounded(self) -> None:
        assert celsius_to_fahrenheit_rounded(22.78) == 73
        assert celsius_to_fahrenheit_rounded(21.67) == 71
        assert celsius_to_fahrenheit_rounded(24.44) == 76
        assert celsius_to_fahrenheit_rounded(18.33) == 65


class TestRoomNameExtraction:
    def test_prefers_parent_relations_display_name(self) -> None:
        device = _load_fixture()["devices"][0]
        assert extract_display_name(device) == "Downstairs"

    def test_falls_back_to_custom_name_without_parent_relations(self) -> None:
        device = {
            "name": "enterprises/demo/devices/THERMO-42",
            "traits": {
                "sdm.devices.traits.Info": {"customName": "Guest Room"},
            },
        }
        assert extract_display_name(device) == "Guest Room"
        assert normalize_room_key(extract_display_name(device)) == "guest_room"

    def test_falls_back_to_device_id_segment(self) -> None:
        device = {
            "name": "enterprises/demo/devices/THERMO-99",
            "traits": {},
        }
        assert extract_display_name(device) == "THERMO-99"
        assert normalize_room_key(extract_display_name(device)) == "thermo_99"


class TestParseDevicesPayload:
    def test_parses_two_thermostat_fixture(self) -> None:
        thermostats = parse_devices_payload(_load_fixture())

        assert set(thermostats) == {"downstairs", "upstairs"}

        downstairs = thermostats["downstairs"]
        assert downstairs["name"] == "Downstairs"
        assert downstairs["temperature"] == 73
        assert downstairs["humidity"] == 65
        assert downstairs["mode"] == "COOL"
        assert downstairs["raw_thermostat_mode"] == "COOL"
        assert downstairs["raw_mode"] == "COOL"
        assert downstairs["raw_hvac_status"] == "COOLING"
        assert downstairs["eco_mode"] == "OFF"
        assert downstairs["action"] == "COOLING"
        assert downstairs["action"] == downstairs["raw_hvac_status"]
        assert downstairs["cool_setpoint"] == 71
        assert downstairs["heat_setpoint"] is None
        assert downstairs["setpoint"] == 71
        assert downstairs["online"] is True

        upstairs = thermostats["upstairs"]
        assert upstairs["name"] == "Upstairs"
        assert upstairs["temperature"] == 76
        assert upstairs["humidity"] == 67
        assert upstairs["mode"] == "MANUAL_ECO"
        assert upstairs["raw_thermostat_mode"] == "COOL"
        assert upstairs["raw_mode"] == "COOL"
        assert upstairs["raw_hvac_status"] == "OFF"
        assert upstairs["eco_mode"] == "MANUAL_ECO"
        assert upstairs["action"] == "OFF"
        assert upstairs["action"] == upstairs["raw_hvac_status"]
        assert upstairs["cool_setpoint"] == 76
        assert upstairs["heat_setpoint"] == 65
        assert upstairs["setpoint"] == 76
        assert upstairs["online"] is True

    def test_normal_cool_thermostat_with_standard_setpoint(self) -> None:
        device = {
            "name": "enterprises/demo/devices/cool-1",
            "type": "sdm.devices.types.THERMOSTAT",
            "parentRelations": [{"displayName": "Living Room"}],
            "traits": {
                "sdm.devices.traits.Temperature": {"ambientTemperatureCelsius": 23.0},
                "sdm.devices.traits.Humidity": {"ambientHumidityPercent": 50},
                "sdm.devices.traits.ThermostatMode": {"mode": "COOL"},
                "sdm.devices.traits.ThermostatHvac": {"status": "COOLING"},
                "sdm.devices.traits.ThermostatTemperatureSetpoint": {"coolCelsius": 22.0},
                "sdm.devices.traits.ThermostatEco": {"mode": "OFF"},
                "sdm.devices.traits.Connectivity": {"status": "ONLINE"},
            },
        }
        room_key, entry = parse_thermostat_device(device)
        assert room_key == "living_room"
        assert entry["mode"] == "COOL"
        assert entry["cool_setpoint"] == 72
        assert entry["setpoint"] == 72

    def test_manual_eco_with_empty_setpoint_trait(self) -> None:
        device = {
            "name": "enterprises/demo/devices/eco-1",
            "type": "sdm.devices.types.THERMOSTAT",
            "parentRelations": [{"displayName": "Office"}],
            "traits": {
                "sdm.devices.traits.Temperature": {"ambientTemperatureCelsius": 21.0},
                "sdm.devices.traits.Humidity": {"ambientHumidityPercent": 40},
                "sdm.devices.traits.ThermostatMode": {"mode": "HEAT"},
                "sdm.devices.traits.ThermostatHvac": {"status": "OFF"},
                "sdm.devices.traits.ThermostatTemperatureSetpoint": {},
                "sdm.devices.traits.ThermostatEco": {
                    "mode": "MANUAL_ECO",
                    "heatCelsius": 19.0,
                    "coolCelsius": 23.0,
                },
                "sdm.devices.traits.Connectivity": {"status": "ONLINE"},
            },
        }
        room_key, entry = parse_thermostat_device(device)
        assert room_key == "office"
        assert entry["mode"] == "MANUAL_ECO"
        assert entry["raw_mode"] == "HEAT"
        assert entry["heat_setpoint"] == 66
        assert entry["cool_setpoint"] == 73
        assert entry["setpoint"] == 66

    def test_build_snapshot_shape(self) -> None:
        thermostats = parse_devices_payload(_load_fixture())
        snapshot = build_nest_snapshot(thermostats, updated_at="2026-06-19T12:00:00+00:00")

        assert snapshot["updated_at"] == "2026-06-19T12:00:00+00:00"
        assert isinstance(snapshot["thermostats"], dict)
        assert "downstairs" in snapshot["thermostats"]
        assert "upstairs" in snapshot["thermostats"]


class TestHvacActionFromRawTraits:
    def _device(
        self,
        *,
        thermostat_mode: str,
        hvac_status: str,
        eco_mode: str = "OFF",
    ) -> dict:
        return {
            "name": "enterprises/demo/devices/debug-1",
            "type": "sdm.devices.types.THERMOSTAT",
            "parentRelations": [{"displayName": "Debug Room"}],
            "traits": {
                "sdm.devices.traits.Temperature": {"ambientTemperatureCelsius": 22.0},
                "sdm.devices.traits.Humidity": {"ambientHumidityPercent": 50},
                "sdm.devices.traits.ThermostatMode": {"mode": thermostat_mode},
                "sdm.devices.traits.ThermostatHvac": {"status": hvac_status},
                "sdm.devices.traits.ThermostatTemperatureSetpoint": {"coolCelsius": 21.0},
                "sdm.devices.traits.ThermostatEco": {"mode": eco_mode},
                "sdm.devices.traits.Connectivity": {"status": "ONLINE"},
            },
        }

    def test_cool_mode_off_hvac_status_yields_action_off(self) -> None:
        _, entry = parse_thermostat_device(self._device(thermostat_mode="COOL", hvac_status="OFF"))
        assert entry["raw_thermostat_mode"] == "COOL"
        assert entry["raw_hvac_status"] == "OFF"
        assert entry["action"] == "OFF"
        assert entry["mode"] == "COOL"

    def test_cool_mode_cooling_hvac_status_yields_action_cooling(self) -> None:
        _, entry = parse_thermostat_device(
            self._device(thermostat_mode="COOL", hvac_status="COOLING")
        )
        assert entry["raw_hvac_status"] == "COOLING"
        assert entry["action"] == "COOLING"

    def test_manual_eco_off_hvac_status_yields_action_off(self) -> None:
        device = self._device(thermostat_mode="COOL", hvac_status="OFF", eco_mode="MANUAL_ECO")
        device["traits"]["sdm.devices.traits.ThermostatEco"] = {
            "mode": "MANUAL_ECO",
            "coolCelsius": 23.0,
            "heatCelsius": 19.0,
        }
        _, entry = parse_thermostat_device(device)
        assert entry["eco_mode"] == "MANUAL_ECO"
        assert entry["mode"] == "MANUAL_ECO"
        assert entry["raw_hvac_status"] == "OFF"
        assert entry["action"] == "OFF"


class TestDebugTraitSummary:
    def test_format_debug_trait_summary_is_sanitized(self) -> None:
        thermostats = parse_devices_payload(_load_fixture())
        snapshot = build_nest_snapshot(thermostats)
        lines = format_debug_trait_summary(snapshot)

        assert len(lines) == 2
        combined = "\n".join(lines)
        assert "ya29." not in combined
        assert "enterprises/" not in combined
        assert "raw_hvac_status=COOLING" in combined
        assert "raw_hvac_status=OFF" in combined
        assert "raw_thermostat_mode=COOL" in combined
        assert "eco_mode=MANUAL_ECO" in combined
        assert "room=Downstairs" in combined
        assert "room=Upstairs" in combined


class TestRedactNestMessage:
    def test_redacts_google_access_token(self) -> None:
        raw = "OAuth failed: Bearer ya29.a0AfH6SMBx-example-token-value"
        redacted = redact_nest_message(raw)
        assert "ya29." not in redacted
        assert "[REDACTED" in redacted

    def test_redacts_client_secret_assignment(self) -> None:
        raw = "config error client_secret=GOCSPX-not-a-real-secret"
        redacted = redact_nest_message(raw)
        assert "GOCSPX-not-a-real-secret" not in redacted
        assert "client_secret=[REDACTED]" in redacted

    def test_redacts_refresh_token_assignment(self) -> None:
        raw = "refresh_token=1//0g-not-a-real-refresh-token"
        redacted = redact_nest_message(raw)
        assert "1//0g-not-a-real-refresh-token" not in redacted
