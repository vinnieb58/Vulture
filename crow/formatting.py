"""
Discord-friendly formatting for Crow status messages.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from crow.config import DISPLAY_TIMEZONE

# Discord message limit; leave room for truncation notice.
MAX_MESSAGE_LEN = 1900


def truncate(text: str, limit: int = MAX_MESSAGE_LEN) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "\n…*(truncated)*"


def get_display_timezone() -> ZoneInfo:
    """Timezone for user-facing timestamps (default: US Central)."""
    try:
        return ZoneInfo(DISPLAY_TIMEZONE)
    except ZoneInfoNotFoundError:
        return ZoneInfo("UTC")


def format_timestamp(dt: datetime | None = None) -> str:
    """
    Format a moment for Discord display in the configured local timezone.

    Naive datetimes are treated as UTC. Default when omitted is now (UTC).
    """
    tz = get_display_timezone()
    if dt is None:
        dt = datetime.now(timezone.utc)
    elif dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    local = dt.astimezone(tz)
    tz_label = local.tzname() or DISPLAY_TIMEZONE
    return local.strftime(f"%Y-%m-%d %H:%M:%S {tz_label}")


def format_bytes(num: int | float | None) -> str:
    if num is None:
        return "n/a"
    n = float(num)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024.0 or unit == "TB":
            if unit == "B":
                return f"{int(n)} {unit}"
            return f"{n:.1f} {unit}"
        n /= 1024.0
    return f"{n:.1f} TB"


def format_percent(used: float | None, total: float | None) -> str:
    if used is None or total is None or total <= 0:
        return "n/a"
    return f"{100.0 * used / total:.1f}%"


def disk_level(percent_used: float | None) -> str:
    """Return ok | warn | critical based on usage percent."""
    if percent_used is None:
        return "unknown"
    if percent_used >= 90:
        return "critical"
    if percent_used >= 80:
        return "warn"
    return "ok"


def disk_level_icon(level: str) -> str:
    return {
        "ok": "✓",
        "warn": "⚠",
        "critical": "🔴",
        "unknown": "?",
    }.get(level, "?")


def join_lines(parts: list[str]) -> str:
    return "\n".join(p for p in parts if p)


def safe_str(value: Any, default: str = "n/a") -> str:
    if value is None:
        return default
    return str(value)
