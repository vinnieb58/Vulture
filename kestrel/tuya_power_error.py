"""Persistent Tuya power poll error records for dashboard visibility."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from kestrel.tuya_power import redact_tuya_message

_LOCAL_HINT = re.compile(r"(?i)\b(local read|tinytuya|deviceScan|connection|timeout|unreachable)\b")
_CLOUD_HINT = re.compile(r"(?i)\b(cloud read|tuya cloud|sign|api key|unauthorized|forbidden)\b")
_CONFIG_HINT = re.compile(r"(?i)\bmissing required tuya\b|\bconfig\b|not configured")


def tuya_error_path_for(status_path: str | Path) -> Path:
    """Return the Tuya poll error file path adjacent to the status snapshot."""
    path = Path(status_path)
    return path.with_name("kestrel_tuya_power_error.json")


def classify_tuya_error(message: str) -> str:
    """Classify a poll failure message for dashboard display."""
    if _CONFIG_HINT.search(message):
        return "config"
    if _CLOUD_HINT.search(message):
        return "cloud"
    if _LOCAL_HINT.search(message):
        return "local"
    return "api"


def read_snapshot_last_success(status_path: str | Path) -> str | None:
    """Return ``updated_at`` from the last good Tuya status snapshot, if readable."""
    path = Path(status_path)
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    updated_at = payload.get("updated_at")
    return str(updated_at) if updated_at else None


def build_tuya_error_record(
    message: str,
    *,
    last_success: str | None = None,
    failed_at: str | None = None,
) -> dict[str, Any]:
    """Build a safe Tuya poll error record for ``kestrel_tuya_power_error.json``."""
    redacted = redact_tuya_message(message) or "Tuya power poll failed"
    record: dict[str, Any] = {
        "timestamp": failed_at
        or datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "error_type": classify_tuya_error(redacted),
        "message": redacted,
    }
    if last_success:
        record["last_success"] = last_success
    return record


def write_tuya_error(path: str | Path, record: dict[str, Any]) -> None:
    """Write or update the Tuya poll error file."""
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")


def clear_tuya_error(path: str | Path) -> None:
    """Remove the Tuya poll error file after a successful poll."""
    target = Path(path)
    if target.is_file():
        target.unlink()


def record_tuya_poll_error(
    *,
    status_path: str | Path,
    message: str,
    error_path: str | Path | None = None,
) -> dict[str, Any]:
    """Write a safe poll error record without touching the status snapshot."""
    destination = Path(error_path) if error_path else tuya_error_path_for(status_path)
    record = build_tuya_error_record(
        message,
        last_success=read_snapshot_last_success(status_path),
    )
    write_tuya_error(destination, record)
    return record
