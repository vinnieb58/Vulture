"""Defensive Tuya appliance power status reader for the Kestrel dashboard."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from tuya_power_error_status import (
    read_tuya_poll_error,
    tuya_poll_warning_for_missing_data,
    tuya_poll_warning_for_stale_data,
)

TUYA_STATUS_PATH = Path(
    os.environ.get("TUYA_STATUS_PATH", "/app/data/kestrel_tuya_power_status.json")
)

# 60-second poll timer; treat snapshots older than 2× interval as stale.
STALE_AFTER_SECONDS = 120

_APPLIANCE_ORDER = (
    "ac_compressor",
    "furnace_air_handler",
    "dryer",
    "dishwasher",
)

_APPLIANCE_LABELS = {
    "ac_compressor": "AC compressor",
    "furnace_air_handler": "Furnace / air handler",
    "dryer": "Dryer",
    "dishwasher": "Dishwasher",
}

_RAW_FIELDS = frozenset({"raw_dps", "raw_unknown"})


def _parse_iso(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _sanitize_appliance_entry(entry: dict[str, Any]) -> dict[str, Any]:
    clean: dict[str, Any] = {}
    for key, value in entry.items():
        if key in _RAW_FIELDS:
            continue
        clean[key] = value
    return clean


def _parse_appliances(raw: dict[str, Any]) -> dict[str, dict[str, Any]]:
    appliances_raw = raw.get("appliances")
    if not isinstance(appliances_raw, dict):
        return {}

    appliances: dict[str, dict[str, Any]] = {}
    for appliance_key, entry in appliances_raw.items():
        if not isinstance(entry, dict):
            continue
        appliances[str(appliance_key)] = _sanitize_appliance_entry(entry)
    return appliances


def _compute_section_state(
    *,
    has_snapshot: bool,
    age_seconds: int | None,
    snapshot_stale: bool,
    limited: bool,
    appliances: dict[str, dict[str, Any]],
) -> str:
    if not has_snapshot:
        return "no_data"
    if not appliances:
        return "error"
    if snapshot_stale or (
        age_seconds is not None and age_seconds > STALE_AFTER_SECONDS
    ):
        return "stale"
    if limited:
        return "limited"
    return "online"


def _headline_for_state(state: str, *, age_seconds: int | None = None) -> str:
    if state == "online":
        return "Appliance power available"
    if state == "limited":
        return "Appliance power limited"
    if state == "stale":
        if age_seconds is not None:
            age_minutes = max(1, age_seconds // 60)
            return f"Tuya power data stale ({age_minutes} min old)"
        return "Tuya power data stale"
    if state == "error":
        return "Tuya power status unavailable"
    return "No Tuya power data yet"


def read_tuya_power_status(*, now: datetime | None = None) -> dict[str, Any]:
    """
    Load a sanitized Tuya power status snapshot.

    Never raises. ``state`` is one of: ``online``, ``stale``, ``limited``,
    ``error``, ``no_data``.
    """
    reference = now or datetime.now(timezone.utc)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=timezone.utc)

    result: dict[str, Any] = {
        "state": "no_data",
        "headline": _headline_for_state("no_data"),
        "warning": None,
        "updated_at": None,
        "age_seconds": None,
        "source": None,
        "limited": False,
        "device_model": None,
        "appliances": [],
    }

    poll_error = read_tuya_poll_error()

    def _empty_appliances() -> list[dict[str, Any]]:
        return [
            {
                "key": appliance_key,
                "label": _APPLIANCE_LABELS[appliance_key],
                "power_w": None,
                "voltage_v": None,
                "current_a": None,
                "energy_forward_kwh": None,
                "energy_forward_kwh_inferred": None,
                "online": None,
                "source": None,
                "state": "no_data",
            }
            for appliance_key in _APPLIANCE_ORDER
        ]

    if not TUYA_STATUS_PATH.is_file():
        result["appliances"] = _empty_appliances()
        result["warning"] = tuya_poll_warning_for_missing_data(
            state="no_data",
            poll_error=poll_error,
        ) or "Tuya power status file not found"
        return result

    try:
        text = TUYA_STATUS_PATH.read_text(encoding="utf-8")
    except OSError as exc:
        result["state"] = "error"
        result["headline"] = _headline_for_state("error")
        result["appliances"] = _empty_appliances()
        result["warning"] = (
            tuya_poll_warning_for_missing_data(state="error", poll_error=poll_error)
            or f"Could not read Tuya power status file: {exc}"
        )
        return result

    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        result["state"] = "error"
        result["headline"] = _headline_for_state("error")
        result["appliances"] = _empty_appliances()
        result["warning"] = (
            tuya_poll_warning_for_missing_data(state="error", poll_error=poll_error)
            or f"Invalid Tuya power status JSON: {exc}"
        )
        return result

    if not isinstance(raw, dict):
        result["state"] = "error"
        result["headline"] = _headline_for_state("error")
        result["appliances"] = _empty_appliances()
        result["warning"] = (
            tuya_poll_warning_for_missing_data(state="error", poll_error=poll_error)
            or "Tuya power status JSON must be an object"
        )
        return result

    appliances = _parse_appliances(raw)
    updated_at = raw.get("updated_at")
    if updated_at is not None:
        result["updated_at"] = str(updated_at)

    age_seconds: int | None = None
    if updated_at:
        parsed_time = _parse_iso(str(updated_at))
        if parsed_time is not None:
            age_seconds = max(0, int((reference - parsed_time).total_seconds()))
            result["age_seconds"] = age_seconds

    limited = bool(raw.get("limited", False))
    snapshot_stale = bool(raw.get("stale", False))
    source = raw.get("source")
    if source is not None:
        result["source"] = str(source)
    result["limited"] = limited
    device_model = raw.get("device_model")
    if device_model is not None:
        result["device_model"] = str(device_model)

    state = _compute_section_state(
        has_snapshot=True,
        age_seconds=age_seconds,
        snapshot_stale=snapshot_stale,
        limited=limited,
        appliances=appliances,
    )
    result["state"] = state
    result["headline"] = _headline_for_state(state, age_seconds=age_seconds)

    parsed_appliances: list[dict[str, Any]] = []
    for appliance_key in _APPLIANCE_ORDER:
        entry = appliances.get(appliance_key, {})
        label = str(entry.get("label") or _APPLIANCE_LABELS[appliance_key])
        appliance_state = state
        if not entry:
            appliance_state = "no_data"
        elif state == "online" and entry.get("online") is False:
            appliance_state = "limited"

        parsed_appliances.append(
            {
                "key": appliance_key,
                "label": label,
                "power_w": entry.get("power_w"),
                "voltage_v": entry.get("voltage_v"),
                "current_a": entry.get("current_a"),
                "energy_forward_kwh": entry.get("energy_forward_kwh"),
                "energy_forward_kwh_inferred": entry.get("energy_forward_kwh_inferred"),
                "online": entry.get("online"),
                "source": entry.get("source"),
                "state": appliance_state,
            }
        )

    result["appliances"] = parsed_appliances

    if state == "stale":
        result["warning"] = tuya_poll_warning_for_stale_data(
            is_stale=True,
            poll_error=poll_error,
        ) or result["headline"]
    elif state in ("no_data", "error"):
        result["warning"] = tuya_poll_warning_for_missing_data(
            state=state,
            poll_error=poll_error,
        ) or result.get("warning")

    return result
