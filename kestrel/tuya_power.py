"""Tuya dual-channel energy meter read-only polling (V-WIFI-DL02-ES / PJ1103A class)."""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from kestrel.config import load_dotenv_if_available
from kestrel.redact import redact_text

log = logging.getLogger(__name__)

DEVICE_MODEL = "V-WIFI-DL02-ES"
DEFAULT_PROTOCOL_VERSION = 3.4

METER_1_KEY = "meter_1"
METER_2_KEY = "meter_2"
CHANNEL_1_KEY = "channel_1"
CHANNEL_2_KEY = "channel_2"

# CT mapping: meter slot -> channel -> appliance key / label
CHANNEL_MAPPING: dict[str, dict[str, tuple[str, str]]] = {
    METER_1_KEY: {
        CHANNEL_1_KEY: ("ac_compressor", "AC compressor"),
        CHANNEL_2_KEY: ("furnace_air_handler", "Furnace / air handler"),
    },
    METER_2_KEY: {
        CHANNEL_1_KEY: ("dryer", "Dryer"),
        CHANNEL_2_KEY: ("dishwasher", "Dishwasher"),
    },
}

# WiFiDualMeterDevice DPS ids (TinyTuya Contrib / Tuya v3.4 dual meter)
DPS_POWER_A = "101"
DPS_POWER_B = "105"
DPS_ENERGY_FORWARD_A = "106"
DPS_ENERGY_FORWARD_B = "108"
DPS_VOLTAGE = "112"
DPS_CURRENT_A = "113"
DPS_CURRENT_B = "114"
DPS_TOTAL_POWER = "115"

_POWER_SCALE = 10
_ENERGY_SCALE = 100
_VOLTAGE_SCALE = 10
_CURRENT_SCALE = 1000

_LOCAL_KEY = re.compile(r"(?i)\b(local_key|localkey|secret)\s*[:=]\s*\S+")
_DEVICE_ID = re.compile(r"(?i)\b(device_id|dev_id|deviceid)\s*[:=]\s*\S+")
_TUYA_TOKEN = re.compile(r"(?i)\b(access_token|sign|signature)\s*[:=]\s*\S+")


class TuyaPowerConfigError(Exception):
    """Raised when Tuya power monitoring configuration is invalid."""


class TuyaPowerApiError(Exception):
    """Raised when Tuya local or cloud reads fail."""


@dataclass(frozen=True)
class TuyaMeterConfig:
    meter_key: str
    device_id: str
    address: str
    local_key: str
    version: float


@dataclass(frozen=True)
class TuyaPowerConfig:
    meters: tuple[TuyaMeterConfig, ...]
    output_path: str
    cloud_api_key: str | None
    cloud_api_secret: str | None
    cloud_region: str

    @property
    def has_cloud_fallback(self) -> bool:
        return bool(self.cloud_api_key and self.cloud_api_secret)


def redact_tuya_message(text: str | None) -> str | None:
    """Redact Tuya local keys, device ids, and cloud tokens from log/error text."""
    if not text:
        return text
    result = redact_text(text)
    result = _LOCAL_KEY.sub(r"\1=[REDACTED]", result)
    result = _DEVICE_ID.sub(r"\1=[REDACTED]", result)
    result = _TUYA_TOKEN.sub(r"\1=[REDACTED]", result)
    return result


def _parse_meter_config(
    *,
    meter_key: str,
    device_id_env: str,
    address_env: str,
    local_key_env: str,
    shared_local_key: str,
    version: float,
) -> TuyaMeterConfig | None:
    device_id = (os.getenv(device_id_env) or "").strip()
    address = (os.getenv(address_env) or "").strip()
    local_key = (os.getenv(local_key_env) or shared_local_key).strip()
    if not device_id and not address:
        return None
    missing: list[str] = []
    if not device_id:
        missing.append(device_id_env)
    if not address:
        missing.append(address_env)
    if not local_key:
        missing.append(local_key_env if os.getenv(local_key_env) else "TUYA_LOCAL_KEY")
    if missing:
        raise TuyaPowerConfigError(
            f"Meter {meter_key} is partially configured; missing: {', '.join(missing)}"
        )
    return TuyaMeterConfig(
        meter_key=meter_key,
        device_id=device_id,
        address=address,
        local_key=local_key,
        version=version,
    )


def load_tuya_power_config() -> TuyaPowerConfig:
    """Load Tuya dual-meter credentials from environment variables."""
    load_dotenv_if_available()

    try:
        version = float(os.getenv("TUYA_DEVICE_VERSION", str(DEFAULT_PROTOCOL_VERSION)))
    except ValueError as exc:
        raise TuyaPowerConfigError("TUYA_DEVICE_VERSION must be a number") from exc

    shared_local_key = (os.getenv("TUYA_LOCAL_KEY") or "").strip()
    output_path = (os.getenv("TUYA_STATUS_PATH") or "data/kestrel_tuya_power_status.json").strip()

    meters: list[TuyaMeterConfig] = []
    for meter_key, device_env, address_env, key_env in (
        (METER_1_KEY, "TUYA_METER1_DEVICE_ID", "TUYA_METER1_IP", "TUYA_METER1_LOCAL_KEY"),
        (METER_2_KEY, "TUYA_METER2_DEVICE_ID", "TUYA_METER2_IP", "TUYA_METER2_LOCAL_KEY"),
    ):
        meter = _parse_meter_config(
            meter_key=meter_key,
            device_id_env=device_env,
            address_env=address_env,
            local_key_env=key_env,
            shared_local_key=shared_local_key,
            version=version,
        )
        if meter is not None:
            meters.append(meter)

    if not meters:
        raise TuyaPowerConfigError(
            "No Tuya meters configured. Set TUYA_METER1_DEVICE_ID/TUYA_METER1_IP and "
            "TUYA_METER2_DEVICE_ID/TUYA_METER2_IP (plus local keys) in .env."
        )

    cloud_api_key = (os.getenv("TUYA_CLOUD_API_KEY") or "").strip() or None
    cloud_api_secret = (os.getenv("TUYA_CLOUD_API_SECRET") or "").strip() or None
    cloud_region = (os.getenv("TUYA_CLOUD_REGION") or "us").strip().lower()

    return TuyaPowerConfig(
        meters=tuple(meters),
        output_path=output_path,
        cloud_api_key=cloud_api_key,
        cloud_api_secret=cloud_api_secret,
        cloud_region=cloud_region,
    )


def _scaled_value(raw: Any, scale: int) -> float | None:
    if raw is None:
        return None
    try:
        return float(raw) / scale
    except (TypeError, ValueError):
        return None


def _extract_dps(status_payload: dict[str, Any]) -> dict[str, Any]:
    dps = status_payload.get("dps")
    if isinstance(dps, dict):
        return {str(key): value for key, value in dps.items()}
    return {}


def parse_dual_meter_dps(
    raw_dps: dict[str, Any],
    *,
    meter_key: str,
    source: str,
    online: bool = True,
) -> dict[str, Any]:
    """Normalize WiFiDualMeter DPS into meter + appliance channel entries."""
    power_a = _scaled_value(raw_dps.get(DPS_POWER_A), _POWER_SCALE)
    power_b = _scaled_value(raw_dps.get(DPS_POWER_B), _POWER_SCALE)
    current_a = _scaled_value(raw_dps.get(DPS_CURRENT_A), _CURRENT_SCALE)
    current_b = _scaled_value(raw_dps.get(DPS_CURRENT_B), _CURRENT_SCALE)
    energy_a = _scaled_value(raw_dps.get(DPS_ENERGY_FORWARD_A), _ENERGY_SCALE)
    energy_b = _scaled_value(raw_dps.get(DPS_ENERGY_FORWARD_B), _ENERGY_SCALE)
    voltage = _scaled_value(raw_dps.get(DPS_VOLTAGE), _VOLTAGE_SCALE)
    total_power = _scaled_value(raw_dps.get(DPS_TOTAL_POWER), _POWER_SCALE)

    channel_values = {
        CHANNEL_1_KEY: {
            "power_w": power_a,
            "current_a": current_a,
            "energy_forward_kwh": energy_a,
        },
        CHANNEL_2_KEY: {
            "power_w": power_b,
            "current_a": current_b,
            "energy_forward_kwh": energy_b,
        },
    }

    channels: dict[str, Any] = {}
    for channel_key, values in channel_values.items():
        appliance_key, label = CHANNEL_MAPPING[meter_key][channel_key]
        channels[channel_key] = {
            "label": label,
            "key": appliance_key,
            "online": online,
            "source": source,
            **values,
        }

    return {
        "meter_key": meter_key,
        "online": online,
        "source": source,
        "voltage_v": voltage,
        "total_power_w": total_power,
        "raw_dps_keys": sorted(raw_dps.keys()),
        "channels": channels,
    }


def build_appliance_index(meters: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Flatten meter channel entries into appliance-keyed index."""
    appliances: dict[str, dict[str, Any]] = {}
    for meter_entry in meters.values():
        if not isinstance(meter_entry, dict):
            continue
        meter_key = str(meter_entry.get("meter_key") or "")
        channels = meter_entry.get("channels")
        if not isinstance(channels, dict):
            continue
        for channel_entry in channels.values():
            if not isinstance(channel_entry, dict):
                continue
            appliance_key = channel_entry.get("key")
            if not isinstance(appliance_key, str) or not appliance_key:
                continue
            appliances[appliance_key] = {
                "label": channel_entry.get("label"),
                "meter": meter_key,
                "online": channel_entry.get("online"),
                "source": channel_entry.get("source"),
                "power_w": channel_entry.get("power_w"),
                "current_a": channel_entry.get("current_a"),
                "energy_forward_kwh": channel_entry.get("energy_forward_kwh"),
            }
    return appliances


def build_tuya_power_snapshot(
    meters: dict[str, dict[str, Any]],
    *,
    updated_at: str | None = None,
    source: str = "local",
    limited: bool = False,
    stale: bool = False,
) -> dict[str, Any]:
    """Build the normalized Tuya power status snapshot."""
    timestamp = updated_at or datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    return {
        "updated_at": timestamp,
        "device_model": DEVICE_MODEL,
        "source": source,
        "limited": limited,
        "stale": stale,
        "meters": meters,
        "appliances": build_appliance_index(meters),
    }


def _require_tinytuya():
    try:
        import tinytuya  # noqa: F401
    except ImportError as exc:
        raise TuyaPowerApiError(
            "TinyTuya is not installed. Install with: pip install tinytuya"
        ) from exc


def scan_local_devices(*, maxretry: int | None = 15) -> dict[str, Any]:
    """Run a TinyTuya UDP scan (discovery only, no writes).

    TinyTuya 1.18.x expects ``maxretry`` (not ``max_retries``).
    """
    _require_tinytuya()
    import tinytuya

    try:
        devices = tinytuya.deviceScan(maxretry=maxretry)
    except Exception as exc:
        raise TuyaPowerApiError(
            redact_tuya_message(str(exc)) or "TinyTuya device scan failed"
        ) from exc
    if not isinstance(devices, dict):
        return {}
    return devices


def read_meter_local(meter: TuyaMeterConfig) -> dict[str, Any]:
    """Read one dual-channel meter via TinyTuya local API."""
    _require_tinytuya()
    from tinytuya.Contrib import WiFiDualMeterDevice

    try:
        client = WiFiDualMeterDevice.WiFiDualMeterDevice(
            dev_id=meter.device_id,
            address=meter.address,
            local_key=meter.local_key,
            version=meter.version,
        )
        payload = client.status()
    except Exception as exc:
        raise TuyaPowerApiError(
            redact_tuya_message(str(exc)) or f"Local read failed for {meter.meter_key}"
        ) from exc

    if not isinstance(payload, dict):
        raise TuyaPowerApiError(f"Local read for {meter.meter_key} returned non-object payload")

    dps = _extract_dps(payload)
    if not dps:
        raise TuyaPowerApiError(f"Local read for {meter.meter_key} returned empty DPS")

    return {
        "transport": "local",
        "raw_status": payload,
        "raw_dps": dps,
    }


def read_meter_cloud(config: TuyaPowerConfig, meter: TuyaMeterConfig) -> dict[str, Any]:
    """Read one meter via Tuya Cloud (fallback only)."""
    _require_tinytuya()
    import tinytuya

    if not config.has_cloud_fallback:
        raise TuyaPowerApiError("Tuya Cloud fallback is not configured")

    try:
        cloud = tinytuya.Cloud(
            config.cloud_api_key,
            config.cloud_api_secret,
            config.cloud_region,
        )
        payload = cloud.getstatus(meter.device_id)
    except Exception as exc:
        raise TuyaPowerApiError(
            redact_tuya_message(str(exc)) or f"Cloud read failed for {meter.meter_key}"
        ) from exc

    if not isinstance(payload, dict):
        raise TuyaPowerApiError(f"Cloud read for {meter.meter_key} returned non-object payload")

    result = payload.get("result")
    if isinstance(result, list):
        dps = {str(item.get("code")): item.get("value") for item in result if isinstance(item, dict)}
    elif isinstance(result, dict):
        dps = {str(key): value for key, value in result.items()}
    else:
        dps = _extract_dps(payload)

    if not dps:
        raise TuyaPowerApiError(f"Cloud read for {meter.meter_key} returned empty DPS")

    return {
        "transport": "cloud",
        "raw_status": payload,
        "raw_dps": dps,
    }


def read_meter_with_fallback(
    config: TuyaPowerConfig,
    meter: TuyaMeterConfig,
) -> tuple[dict[str, Any], str]:
    """Prefer local TinyTuya reads; fall back to cloud when configured."""
    try:
        payload = read_meter_local(meter)
        return payload, "local"
    except TuyaPowerApiError as local_exc:
        if not config.has_cloud_fallback:
            raise local_exc
        log.warning(
            "Local read failed for %s; trying Tuya Cloud fallback",
            meter.meter_key,
        )
        payload = read_meter_cloud(config, meter)
        return payload, "cloud"


def poll_tuya_power_meters(config: TuyaPowerConfig) -> dict[str, Any]:
    """Poll configured meters and return a normalized power snapshot."""
    meter_entries: dict[str, dict[str, Any]] = {}
    sources: set[str] = set()
    limited = False

    for meter in config.meters:
        payload, source = read_meter_with_fallback(config, meter)
        sources.add(source)
        parsed = parse_dual_meter_dps(
            payload["raw_dps"],
            meter_key=meter.meter_key,
            source=source,
            online=True,
        )
        parsed["device_id_suffix"] = meter.device_id[-4:] if len(meter.device_id) >= 4 else "????"
        meter_entries[meter.meter_key] = parsed

    expected = {METER_1_KEY, METER_2_KEY}
    found = set(meter_entries)
    if found != expected:
        limited = True

    combined_source = "local"
    if sources == {"cloud"}:
        combined_source = "cloud"
    elif len(sources) > 1:
        combined_source = "mixed"

    snapshot = build_tuya_power_snapshot(
        meter_entries,
        source=combined_source,
        limited=limited,
        stale=False,
    )
    log.info(
        "Parsed %s Tuya meter(s) via %s (limited=%s)",
        len(meter_entries),
        combined_source,
        limited,
    )
    return snapshot


def format_raw_dps_lines(
    *,
    meter_key: str,
    raw_dps: dict[str, Any],
    source: str,
) -> list[str]:
    """Return sanitized raw DPS key/value lines for operator discovery."""
    lines: list[str] = []
    for dps_key in sorted(raw_dps.keys(), key=lambda item: (len(item), item)):
        value = raw_dps[dps_key]
        if isinstance(value, (dict, list)):
            rendered = json.dumps(redact_tuya_message(json.dumps(value)), sort_keys=True)
        else:
            rendered = str(value)
        lines.append(f"meter={meter_key} source={source} dps={dps_key} value={rendered}")
    return lines


def format_debug_dps_summary(snapshot: dict[str, Any]) -> list[str]:
    """Return sanitized per-appliance power lines for operator debugging."""
    appliances = snapshot.get("appliances")
    if not isinstance(appliances, dict):
        return []

    lines: list[str] = []
    for appliance_key in sorted(appliances):
        entry = appliances[appliance_key]
        if not isinstance(entry, dict):
            continue
        power = entry.get("power_w")
        current = entry.get("current_a")
        energy = entry.get("energy_forward_kwh")
        lines.append(
            " | ".join(
                [
                    f"appliance={appliance_key}",
                    f"label={entry.get('label') or '—'}",
                    f"meter={entry.get('meter') or '—'}",
                    f"source={entry.get('source') or '—'}",
                    f"power_w={power if power is not None else '—'}",
                    f"current_a={current if current is not None else '—'}",
                    f"energy_kwh={energy if energy is not None else '—'}",
                    f"online={entry.get('online')}",
                ]
            )
        )
    return lines
