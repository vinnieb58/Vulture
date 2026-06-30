"""Defensive Nest thermostat snapshot reader for the House dashboard card."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from nest_error_status import apply_poll_error_to_house_status, read_nest_poll_error

NEST_STATUS_PATH = Path(
    os.environ.get("NEST_STATUS_PATH", "/app/data/kestrel_nest_status.json")
)

STALE_AFTER_MINUTES = 15

_THERMOSTAT_ORDER = ("downstairs", "upstairs")


def _parse_iso(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def _ordered_thermostat_items(thermostats: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    items = [(key, value) for key, value in thermostats.items() if isinstance(value, dict)]
    order_index = {key: index for index, key in enumerate(_THERMOSTAT_ORDER)}

    def sort_key(item: tuple[str, dict[str, Any]]) -> tuple[int, str]:
        room_key, entry = item
        display = str(entry.get("name") or room_key)
        return (order_index.get(room_key, 100), display.lower())

    return sorted(items, key=sort_key)


def read_house_status(*, now: datetime | None = None) -> dict[str, Any]:
    """
    Load a sanitized House status snapshot from the Nest SDM poller output.

    Never raises. ``state`` is one of: ``available``, ``stale``, ``auth_failure``, ``no_data``, ``error``.
    """
    reference = now or datetime.now(timezone.utc)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=timezone.utc)

    result: dict[str, Any] = {
        "state": "no_data",
        "headline": "Nest data unavailable",
        "warning": None,
        "updated_at": None,
        "age_minutes": None,
        "thermostats": [],
    }

    if not NEST_STATUS_PATH.exists():
        result["warning"] = "Nest thermostat snapshot not found"
        return result

    try:
        text = NEST_STATUS_PATH.read_text(encoding="utf-8")
    except OSError as exc:
        result["state"] = "error"
        result["headline"] = "Could not read Nest snapshot"
        result["warning"] = f"Could not read Nest snapshot: {exc}"
        return result

    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        result["state"] = "error"
        result["headline"] = "Nest snapshot unavailable"
        result["warning"] = f"Invalid Nest snapshot JSON: {exc}"
        return result

    if not isinstance(raw, dict):
        result["state"] = "error"
        result["headline"] = "Nest snapshot unavailable"
        result["warning"] = "Nest snapshot JSON must be an object"
        return result

    updated_at = raw.get("updated_at")
    if updated_at is not None:
        result["updated_at"] = str(updated_at)

    thermostats_raw = raw.get("thermostats")
    if not isinstance(thermostats_raw, dict):
        thermostats_raw = {}

    parsed_thermostats: list[dict[str, Any]] = []
    for room_key, entry in _ordered_thermostat_items(thermostats_raw):
        parsed_thermostats.append(
            {
                "room_key": room_key,
                "name": str(entry.get("name") or room_key.replace("_", " ").title()),
                "temperature_f": entry.get("temperature"),
                "humidity_percent": entry.get("humidity"),
                "mode": entry.get("mode"),
                "action": entry.get("action"),
                "online": entry.get("online"),
            }
        )

    result["thermostats"] = parsed_thermostats

    if not parsed_thermostats:
        result["warning"] = result["warning"] or "No Nest thermostats in snapshot"
        return result

    if updated_at:
        parsed_time = _parse_iso(str(updated_at))
        if parsed_time is not None:
            age_minutes = max(0, int((reference - parsed_time).total_seconds() // 60))
            result["age_minutes"] = age_minutes
            if age_minutes > STALE_AFTER_MINUTES:
                result["state"] = "stale"
                result["headline"] = f"Nest data stale ({age_minutes} min old)"
                result["warning"] = result["headline"]
                return apply_poll_error_to_house_status(
                    result,
                    poll_error=read_nest_poll_error(),
                )

    result["state"] = "available"
    result["headline"] = "House climate available"
    return result
