"""Read sanitized Nest poll error records for dashboard warnings."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

NEST_STATUS_PATH = Path(
    os.environ.get("NEST_STATUS_PATH", "/app/data/kestrel_nest_status.json")
)
NEST_ERROR_PATH = Path(
    os.environ.get(
        "NEST_ERROR_PATH",
        str(NEST_STATUS_PATH.with_name("kestrel_nest_error.json")),
    )
)

_AUTH_ERROR_TYPES = frozenset({"oauth", "config"})


def read_nest_poll_error() -> dict[str, Any] | None:
    """Load a sanitized Nest poll error record. Never raises."""
    if not NEST_ERROR_PATH.is_file():
        return None
    try:
        payload = json.loads(NEST_ERROR_PATH.read_text(encoding="utf-8"))
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


def is_auth_poll_error(error: dict[str, Any] | None) -> bool:
    if not error:
        return False
    return str(error.get("error_type") or "") in _AUTH_ERROR_TYPES


def nest_poll_warning_for_stale_data(
    *,
    is_stale: bool,
    poll_error: dict[str, Any] | None = None,
) -> str | None:
    """Return a user-facing warning when Nest data is stale and an error file exists."""
    if not is_stale:
        return None
    error = poll_error if poll_error is not None else read_nest_poll_error()
    if not error:
        return None
    if is_auth_poll_error(error):
        return "Nest auth failure"
    return "Nest stale"


def apply_poll_error_to_house_status(
    house: dict[str, Any],
    *,
    poll_error: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Adjust House card state when stale data coincides with a poll error file."""
    is_stale = house.get("state") == "stale"
    warning = nest_poll_warning_for_stale_data(is_stale=is_stale, poll_error=poll_error)
    if not warning:
        return house

    updated = dict(house)
    if warning == "Nest auth failure":
        updated["state"] = "auth_failure"
        updated["headline"] = "Nest auth failure"
        updated["warning"] = warning
    else:
        updated["state"] = "stale"
        updated["headline"] = f"Nest stale ({updated.get('age_minutes')} min old)"
        updated["warning"] = warning
    return updated
