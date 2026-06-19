"""Human-friendly formatting for the House dashboard card."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

_MODE_LABELS = {
    "COOL": "Cool",
    "HEAT": "Heat",
    "HEATCOOL": "Auto",
    "OFF": "Off",
    "MANUAL_ECO": "Eco",
}

_ACTION_LABELS = {
    "COOLING": "Cooling",
    "HEATING": "Heating",
    "OFF": "Off",
}


def format_temperature(value: Any) -> str | None:
    if isinstance(value, (int, float)):
        return f"{round(value)}°F"
    return None


def format_humidity(value: Any) -> str | None:
    if isinstance(value, (int, float)):
        return f"{int(value)}%"
    return None


def format_mode(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).upper()
    return _MODE_LABELS.get(text, str(value).title())


def format_action(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).upper()
    return _ACTION_LABELS.get(text, str(value).title())


def format_updated_age(
    *,
    updated_at: str | None,
    age_minutes: int | None,
    now: datetime | None = None,
) -> str | None:
    if age_minutes is None and updated_at:
        try:
            parsed = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            reference = now or datetime.now(timezone.utc)
            age_minutes = max(0, int((reference - parsed).total_seconds() // 60))
        except ValueError:
            return None

    if age_minutes is None:
        return None

    if age_minutes < 1:
        return "Updated just now"
    if age_minutes == 1:
        return "Updated 1 minute ago"
    if age_minutes < 60:
        return f"Updated {age_minutes} minutes ago"

    hours = age_minutes // 60
    if hours == 1:
        return "Updated 1 hour ago"
    if hours < 24:
        return f"Updated {hours} hours ago"

    days = hours // 24
    if days == 1:
        return "Updated 1 day ago"
    return f"Updated {days} days ago"


def format_summary(mode: Any, action: Any) -> str:
    """Prefer active HVAC action; otherwise show mode (Eco when in manual eco)."""
    action_upper = str(action or "").upper()
    mode_upper = str(mode or "").upper()
    if action_upper in {"COOLING", "HEATING"}:
        return format_action(action) or "—"
    if mode_upper == "MANUAL_ECO":
        return "Eco"
    return format_mode(mode) or format_action(action) or "—"


def format_thermostat_row(entry: dict[str, Any]) -> dict[str, Any]:
    """Return display-ready strings for one thermostat row."""
    temperature = format_temperature(entry.get("temperature_f"))
    humidity = format_humidity(entry.get("humidity_percent"))
    mode = format_mode(entry.get("mode"))
    action = format_action(entry.get("action"))
    summary = format_summary(entry.get("mode"), entry.get("action"))

    return {
        "name": entry.get("name") or "Thermostat",
        "temperature": temperature or "—",
        "humidity": humidity or "—",
        "mode": mode or "—",
        "action": action or "—",
        "summary": summary,
        "metrics_line": (
            f"{temperature or '—'} · {humidity or '—'}"
            if temperature or humidity
            else "—"
        ),
    }


def format_house_card_display(house: dict[str, Any], *, now: datetime | None = None) -> dict[str, Any]:
    """Return display-ready House card fields."""
    state = house.get("state", "no_data")
    style_map = {
        "available": "ok",
        "stale": "warn",
        "no_data": "unknown",
        "error": "fail",
    }
    status_map = {
        "available": "OK",
        "stale": "Stale",
        "no_data": "No data",
        "error": "Error",
    }

    thermostats = [
        format_thermostat_row(entry)
        for entry in house.get("thermostats", [])
        if isinstance(entry, dict)
    ]

    return {
        "status": status_map.get(state, "No data"),
        "style": style_map.get(state, "unknown"),
        "headline": house.get("headline", "Nest data unavailable"),
        "warning": house.get("warning"),
        "updated_display": format_updated_age(
            updated_at=house.get("updated_at"),
            age_minutes=house.get("age_minutes"),
            now=now,
        ),
        "thermostats": thermostats,
        # Reserved for future household metrics (SMT, UPS, sensors, etc.)
        "sections": [],
    }
