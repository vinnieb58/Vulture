"""Read sanitized Tuya power poll error records for dashboard warnings."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

TUYA_STATUS_PATH = Path(
    os.environ.get("TUYA_STATUS_PATH", "/app/data/kestrel_tuya_power_status.json")
)
TUYA_ERROR_PATH = Path(
    os.environ.get(
        "TUYA_ERROR_PATH",
        str(TUYA_STATUS_PATH.with_name("kestrel_tuya_power_error.json")),
    )
)

_CONFIG_ERROR_TYPES = frozenset({"config"})


def read_tuya_poll_error() -> dict[str, Any] | None:
    """Load a sanitized Tuya poll error record. Never raises."""
    if not TUYA_ERROR_PATH.is_file():
        return None
    try:
        payload = json.loads(TUYA_ERROR_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None

    message = payload.get("message")
    error_type = payload.get("error_type")
    return {
        "timestamp": str(payload["timestamp"]) if payload.get("timestamp") else None,
        "error_type": str(error_type) if error_type else None,
        "message": str(message) if isinstance(message, str) else None,
        "last_success": (
            str(payload["last_success"]) if payload.get("last_success") else None
        ),
    }


def is_config_poll_error(error: dict[str, Any] | None) -> bool:
    if not error:
        return False
    return str(error.get("error_type") or "") in _CONFIG_ERROR_TYPES


def tuya_poll_warning_for_stale_data(
    *,
    is_stale: bool,
    poll_error: dict[str, Any] | None = None,
) -> str | None:
    """Return a user-facing warning when Tuya data is stale and an error file exists."""
    if not is_stale:
        return None
    error = poll_error if poll_error is not None else read_tuya_poll_error()
    if not error:
        return None
    if is_config_poll_error(error):
        return "Tuya config failure"
    return "Tuya power stale"


def tuya_poll_warning_for_missing_data(
    *,
    state: str,
    poll_error: dict[str, Any] | None = None,
) -> str | None:
    """Return a warning when status is missing or unreadable and an error sidecar exists."""
    if state not in ("no_data", "error"):
        return None
    error = poll_error if poll_error is not None else read_tuya_poll_error()
    if not error:
        return None
    if is_config_poll_error(error):
        return "Tuya config failure"
    return "Tuya power poll failed"
