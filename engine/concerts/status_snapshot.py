"""Write operational concert watch cycle status for dashboard visibility."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from engine.concerts.repository import count_concert_alerts, count_concert_events, count_watches

DEFAULT_STATUS_PATH = Path(
    os.environ.get("CONCERT_WATCH_STATUS_PATH", "data/concert_watch_status.json")
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_status_snapshot(
    cycle_summary: dict[str, Any],
    *,
    provider_notes: list[str] | None = None,
    previous: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Merge a watch cycle summary with watch counts and success/error timestamps."""
    active_count, paused_count = count_watches()
    errors = list(cycle_summary.get("errors") or [])
    checked_at = _now_iso()

    snapshot: dict[str, Any] = {
        "checked_at": checked_at,
        "watches_checked": int(cycle_summary.get("watches_checked", 0)),
        "events_found": int(cycle_summary.get("events_found", 0)),
        "alerts_sent": int(cycle_summary.get("alerts_sent", 0)),
        "errors": errors,
        "active_watch_count": active_count,
        "paused_watch_count": paused_count,
        "provider_notes": list(provider_notes or []),
        "last_success_at": None,
        "last_error_at": None,
    }

    prev = previous or {}
    if errors:
        snapshot["last_error_at"] = checked_at
        snapshot["last_success_at"] = prev.get("last_success_at")
    else:
        snapshot["last_success_at"] = checked_at
        snapshot["last_error_at"] = prev.get("last_error_at")

    return snapshot


def read_status_snapshot(path: Path | None = None) -> dict[str, Any] | None:
    """Load the latest status snapshot if present and valid."""
    target = path or DEFAULT_STATUS_PATH
    if not target.is_file():
        return None
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def write_status_snapshot(
    cycle_summary: dict[str, Any],
    *,
    path: Path | None = None,
    provider_notes: list[str] | None = None,
) -> dict[str, Any]:
    """Persist concert watch cycle status for dashboard/ops visibility."""
    target = path or DEFAULT_STATUS_PATH
    previous = read_status_snapshot(target)
    snapshot = build_status_snapshot(
        cycle_summary,
        provider_notes=provider_notes,
        previous=previous,
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(snapshot, indent=2) + "\n", encoding="utf-8")
    return snapshot
