"""Google Smart Device Management (Nest) read-only thermostat polling."""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import requests

from kestrel.config import load_dotenv_if_available
from kestrel.redact import redact_text

log = logging.getLogger(__name__)

GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
SDM_API_BASE = "https://smartdevicemanagement.googleapis.com/v1"

TRAIT_INFO = "sdm.devices.traits.Info"
TRAIT_TEMPERATURE = "sdm.devices.traits.Temperature"
TRAIT_HUMIDITY = "sdm.devices.traits.Humidity"
TRAIT_THERMOSTAT_MODE = "sdm.devices.traits.ThermostatMode"
TRAIT_THERMOSTAT_HVAC = "sdm.devices.traits.ThermostatHvac"
TRAIT_THERMOSTAT_SETPOINT = "sdm.devices.traits.ThermostatTemperatureSetpoint"
TRAIT_THERMOSTAT_ECO = "sdm.devices.traits.ThermostatEco"
TRAIT_CONNECTIVITY = "sdm.devices.traits.Connectivity"

THERMOSTAT_TYPE = "sdm.devices.types.THERMOSTAT"

_GOOGLE_ACCESS_TOKEN = re.compile(r"\bya29\.[A-Za-z0-9._-]+\b")
_CLIENT_SECRET = re.compile(r"(?i)\b(client_secret|refresh_token|access_token)\s*[:=]\s*\S+")
_JSON_SECRET = re.compile(
    r'(?i)"(client_secret|refresh_token|access_token)"\s*:\s*"[^"]*"'
)


class NestConfigError(Exception):
    """Raised when Nest SDM configuration is invalid."""


class NestApiError(Exception):
    """Raised when Nest SDM API access fails."""


@dataclass(frozen=True)
class NestConfig:
    project_id: str
    client_id: str
    client_secret: str
    refresh_token: str
    output_path: str

    @property
    def devices_url(self) -> str:
        return f"{SDM_API_BASE}/enterprises/{self.project_id}/devices"


def redact_nest_message(text: str | None) -> str | None:
    """Redact Google OAuth tokens and Nest secrets from log/error text."""
    if not text:
        return text
    result = redact_text(text)
    result = _GOOGLE_ACCESS_TOKEN.sub("[REDACTED_TOKEN]", result)
    result = _JSON_SECRET.sub(r'"\1": "[REDACTED]"', result)
    result = _CLIENT_SECRET.sub(r"\1=[REDACTED]", result)
    return result


def load_nest_config() -> NestConfig:
    """Load Nest SDM credentials from environment variables."""
    load_dotenv_if_available()

    project_id = (os.getenv("NEST_SDM_PROJECT_ID") or "").strip()
    client_id = (os.getenv("NEST_GOOGLE_CLIENT_ID") or "").strip()
    client_secret = (os.getenv("NEST_GOOGLE_CLIENT_SECRET") or "").strip()
    refresh_token = (os.getenv("NEST_GOOGLE_REFRESH_TOKEN") or "").strip()
    output_path = (os.getenv("NEST_STATUS_PATH") or "data/kestrel_nest_status.json").strip()

    missing = [
        name
        for name, value in (
            ("NEST_SDM_PROJECT_ID", project_id),
            ("NEST_GOOGLE_CLIENT_ID", client_id),
            ("NEST_GOOGLE_CLIENT_SECRET", client_secret),
            ("NEST_GOOGLE_REFRESH_TOKEN", refresh_token),
        )
        if not value
    ]
    if missing:
        raise NestConfigError(
            "Missing required Nest environment variables: " + ", ".join(missing)
        )

    return NestConfig(
        project_id=project_id,
        client_id=client_id,
        client_secret=client_secret,
        refresh_token=refresh_token,
        output_path=output_path,
    )


def celsius_to_fahrenheit(celsius: float) -> float:
    """Convert Celsius to Fahrenheit without rounding."""
    return (celsius * 9 / 5) + 32


def celsius_to_fahrenheit_rounded(celsius: float) -> int:
    """Convert Celsius to dashboard-facing Fahrenheit (nearest whole number)."""
    return round(celsius_to_fahrenheit(celsius))


def normalize_room_key(display_name: str) -> str:
    """Normalize a room/display name to lowercase snake_case."""
    cleaned = re.sub(r"[^A-Za-z0-9]+", "_", display_name.strip())
    cleaned = re.sub(r"_+", "_", cleaned).strip("_")
    return cleaned.lower() or "thermostat"


def _device_id_segment(device_name: str) -> str:
    return device_name.rsplit("/", 1)[-1] if device_name else "thermostat"


def extract_display_name(device: dict[str, Any]) -> str:
    """Prefer parentRelations displayName, then Info.customName, then device id."""
    parent_relations = device.get("parentRelations") or []
    if parent_relations:
        first = parent_relations[0]
        if isinstance(first, dict):
            display = first.get("displayName")
            if isinstance(display, str) and display.strip():
                return display.strip()

    traits = device.get("traits") or {}
    info = traits.get(TRAIT_INFO) or {}
    custom_name = info.get("customName")
    if isinstance(custom_name, str) and custom_name.strip():
        return custom_name.strip()

    return _device_id_segment(str(device.get("name") or "thermostat"))


def _trait(device: dict[str, Any], trait_key: str) -> dict[str, Any]:
    traits = device.get("traits") or {}
    value = traits.get(trait_key)
    return value if isinstance(value, dict) else {}


def _optional_celsius(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _active_setpoint_f(
    *,
    effective_mode: str,
    raw_mode: str | None,
    cool_setpoint_f: int | None,
    heat_setpoint_f: int | None,
) -> int | None:
    if effective_mode == "MANUAL_ECO":
        if raw_mode == "HEAT":
            return heat_setpoint_f
        if raw_mode in {"COOL", "HEATCOOL"}:
            return cool_setpoint_f
        return cool_setpoint_f if cool_setpoint_f is not None else heat_setpoint_f

    if effective_mode == "HEAT":
        return heat_setpoint_f
    if effective_mode in {"COOL", "HEATCOOL"}:
        return cool_setpoint_f
    if raw_mode == "HEAT":
        return heat_setpoint_f
    if raw_mode in {"COOL", "HEATCOOL"}:
        return cool_setpoint_f
    return cool_setpoint_f if cool_setpoint_f is not None else heat_setpoint_f


def parse_thermostat_device(device: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Parse one SDM thermostat device into a snapshot entry."""
    display_name = extract_display_name(device)
    room_key = normalize_room_key(display_name)

    temperature_trait = _trait(device, TRAIT_TEMPERATURE)
    humidity_trait = _trait(device, TRAIT_HUMIDITY)
    mode_trait = _trait(device, TRAIT_THERMOSTAT_MODE)
    hvac_trait = _trait(device, TRAIT_THERMOSTAT_HVAC)
    setpoint_trait = _trait(device, TRAIT_THERMOSTAT_SETPOINT)
    eco_trait = _trait(device, TRAIT_THERMOSTAT_ECO)
    connectivity_trait = _trait(device, TRAIT_CONNECTIVITY)

    ambient_c = _optional_celsius(temperature_trait.get("ambientTemperatureCelsius"))
    humidity = humidity_trait.get("ambientHumidityPercent")
    raw_mode = mode_trait.get("mode")
    raw_thermostat_mode = str(raw_mode) if raw_mode is not None else None
    raw_hvac_status_value = hvac_trait.get("status")
    raw_hvac_status = str(raw_hvac_status_value) if raw_hvac_status_value is not None else None
    action = raw_hvac_status

    eco_mode = eco_trait.get("mode")
    eco_mode_str = str(eco_mode) if eco_mode is not None else "OFF"

    if eco_mode_str == "MANUAL_ECO":
        effective_mode = "MANUAL_ECO"
        cool_c = _optional_celsius(eco_trait.get("coolCelsius"))
        heat_c = _optional_celsius(eco_trait.get("heatCelsius"))
    else:
        effective_mode = raw_thermostat_mode or "OFF"
        cool_c = _optional_celsius(setpoint_trait.get("coolCelsius"))
        heat_c = _optional_celsius(setpoint_trait.get("heatCelsius"))

    cool_setpoint_f = celsius_to_fahrenheit_rounded(cool_c) if cool_c is not None else None
    heat_setpoint_f = celsius_to_fahrenheit_rounded(heat_c) if heat_c is not None else None

    if effective_mode == "HEAT":
        cool_setpoint_f = None
    elif effective_mode == "COOL":
        heat_setpoint_f = None

    connectivity = connectivity_trait.get("status")
    online = str(connectivity).upper() == "ONLINE" if connectivity is not None else False

    temperature_f = celsius_to_fahrenheit_rounded(ambient_c) if ambient_c is not None else None
    humidity_percent = int(humidity) if isinstance(humidity, (int, float)) else None

    entry: dict[str, Any] = {
        "name": display_name,
        "device_name": str(device.get("name") or ""),
        "temperature": temperature_f,
        "humidity": humidity_percent,
        "mode": effective_mode,
        "action": action,
        "setpoint": _active_setpoint_f(
            effective_mode=effective_mode,
            raw_mode=raw_thermostat_mode,
            cool_setpoint_f=cool_setpoint_f,
            heat_setpoint_f=heat_setpoint_f,
        ),
        "online": online,
        "raw_hvac_status": raw_hvac_status,
        "raw_thermostat_mode": raw_thermostat_mode,
        "raw_mode": raw_thermostat_mode,
        "eco_mode": eco_mode_str,
        "cool_setpoint": cool_setpoint_f,
        "heat_setpoint": heat_setpoint_f,
    }
    return room_key, entry


def parse_devices_payload(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Parse an SDM /devices response into thermostat snapshot entries."""
    devices = payload.get("devices")
    if not isinstance(devices, list):
        return {}

    thermostats: dict[str, dict[str, Any]] = {}
    for device in devices:
        if not isinstance(device, dict):
            continue
        if device.get("type") != THERMOSTAT_TYPE:
            continue
        room_key, entry = parse_thermostat_device(device)
        thermostats[room_key] = entry
    return thermostats


def build_nest_snapshot(
    thermostats: dict[str, dict[str, Any]],
    *,
    updated_at: str | None = None,
) -> dict[str, Any]:
    """Build the normalized Nest status snapshot."""
    timestamp = updated_at or datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    return {
        "updated_at": timestamp,
        "thermostats": thermostats,
    }


def fetch_access_token(config: NestConfig, *, session: requests.Session | None = None) -> str:
    """Exchange a refresh token for a short-lived access token."""
    http = session or requests.Session()
    try:
        response = http.post(
            GOOGLE_TOKEN_URL,
            data={
                "client_id": config.client_id,
                "client_secret": config.client_secret,
                "refresh_token": config.refresh_token,
                "grant_type": "refresh_token",
            },
            timeout=30,
        )
    except requests.RequestException as exc:
        raise NestApiError(redact_nest_message(str(exc)) or "OAuth token request failed") from exc

    if response.status_code >= 400:
        detail = redact_nest_message(response.text[:500]) or f"HTTP {response.status_code}"
        raise NestApiError(f"OAuth token request failed: {detail}")

    try:
        payload = response.json()
    except ValueError as exc:
        raise NestApiError("OAuth token response was not valid JSON") from exc

    token = payload.get("access_token")
    if not isinstance(token, str) or not token:
        raise NestApiError("OAuth token response missing access_token")

    return token


def fetch_devices(
    config: NestConfig,
    *,
    session: requests.Session | None = None,
    access_token: str | None = None,
) -> dict[str, Any]:
    """Fetch the SDM /devices payload for the configured project."""
    http = session or requests.Session()
    token = access_token or fetch_access_token(config, session=http)

    try:
        response = http.get(
            config.devices_url,
            headers={"Authorization": f"Bearer {token}"},
            timeout=30,
        )
    except requests.RequestException as exc:
        raise NestApiError(redact_nest_message(str(exc)) or "SDM devices request failed") from exc

    if response.status_code >= 400:
        detail = redact_nest_message(response.text[:500]) or f"HTTP {response.status_code}"
        raise NestApiError(f"SDM devices request failed: {detail}")

    try:
        payload = response.json()
    except ValueError as exc:
        raise NestApiError("SDM devices response was not valid JSON") from exc

    if not isinstance(payload, dict):
        raise NestApiError("SDM devices response must be a JSON object")
    return payload


def poll_nest_thermostats(config: NestConfig) -> dict[str, Any]:
    """Poll SDM and return a normalized thermostat snapshot."""
    payload = fetch_devices(config)
    thermostats = parse_devices_payload(payload)
    snapshot = build_nest_snapshot(thermostats)
    log.info("Parsed %s Nest thermostat(s)", len(thermostats))
    return snapshot


def _format_setpoints(entry: dict[str, Any]) -> str:
    parts: list[str] = []
    cool = entry.get("cool_setpoint")
    heat = entry.get("heat_setpoint")
    if cool is not None:
        parts.append(f"cool={cool}F")
    if heat is not None:
        parts.append(f"heat={heat}F")
    if not parts:
        setpoint = entry.get("setpoint")
        if setpoint is not None:
            return f"setpoint={setpoint}F"
        return "—"
    return ", ".join(parts)


def format_debug_trait_summary(snapshot: dict[str, Any]) -> list[str]:
    """Return sanitized per-thermostat raw trait lines for operator debugging."""
    thermostats = snapshot.get("thermostats")
    if not isinstance(thermostats, dict):
        return []

    lines: list[str] = []
    for room_key in sorted(thermostats):
        entry = thermostats[room_key]
        if not isinstance(entry, dict):
            continue
        room = str(entry.get("name") or room_key)
        temperature = entry.get("temperature")
        temp_display = f"{temperature}F" if temperature is not None else "—"
        lines.append(
            " | ".join(
                [
                    f"room={room}",
                    f"raw_hvac_status={entry.get('raw_hvac_status') or '—'}",
                    f"raw_thermostat_mode={entry.get('raw_thermostat_mode') or '—'}",
                    f"eco_mode={entry.get('eco_mode') or '—'}",
                    f"action={entry.get('action') or '—'}",
                    f"temperature={temp_display}",
                    f"setpoints={_format_setpoints(entry)}",
                ]
            )
        )
    return lines

