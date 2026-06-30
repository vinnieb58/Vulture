"""Display formatting for Tuya appliance power on the Kestrel dashboard."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from kestrel_formatting import KESTREL_DISPLAY_TZ, format_timestamp_friendly
from tuya_power_error_status import read_tuya_poll_error
from tuya_power_history import build_appliance_power_series, read_tuya_power_history
from tuya_power_status import read_tuya_power_status

_STATUS_LABELS = {
    "online": "Online",
    "stale": "Stale",
    "limited": "Limited",
    "error": "Error",
    "no_data": "No data",
}

_STATUS_STYLES = {
    "online": "ok",
    "stale": "fail",
    "limited": "warn",
    "unknown": "unknown",
    "error": "fail",
    "no_data": "unknown",
}


def _format_watts(value: float | int | None) -> str:
    if value is None:
        return "—"
    watts = float(value)
    if watts < 10:
        return f"{watts:.1f} W"
    return f"{watts:.0f} W"


def _format_voltage(value: float | int | None) -> str:
    if value is None:
        return "—"
    return f"{float(value):.1f} V"


def _format_energy(
    *,
    energy_forward_kwh: float | int | None,
    energy_forward_kwh_inferred: float | int | None,
) -> tuple[str | None, bool]:
    if isinstance(energy_forward_kwh, (int, float)):
        return f"{float(energy_forward_kwh):.2f} kWh", False
    if isinstance(energy_forward_kwh_inferred, (int, float)):
        return f"{float(energy_forward_kwh_inferred):.2f} kWh (inferred)", True
    return None, False


def _format_age_seconds(age_seconds: int | None) -> str | None:
    if age_seconds is None:
        return None
    if age_seconds < 60:
        return "<1 minute ago"
    minutes = age_seconds // 60
    if minutes == 1:
        return "1 minute ago"
    return f"{minutes} minutes ago"


def _format_chart_time_label(
    iso_timestamp: str,
    *,
    tz_name: str = KESTREL_DISPLAY_TZ,
    now: datetime | None = None,
) -> str:
    return format_timestamp_friendly(iso_timestamp, tz_name=tz_name, now=now)


def _format_chart_series(
    series: list[dict[str, Any]],
    *,
    tz_name: str = KESTREL_DISPLAY_TZ,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    formatted: list[dict[str, Any]] = []
    for item in series:
        points = []
        for point in item.get("points") or []:
            if not isinstance(point, dict):
                continue
            timestamp = point.get("timestamp")
            watts = point.get("watts")
            if not timestamp or not isinstance(watts, (int, float)):
                continue
            points.append(
                {
                    "label": _format_chart_time_label(
                        str(timestamp),
                        tz_name=tz_name,
                        now=now,
                    ),
                    "watts": float(watts),
                }
            )
        if points:
            formatted.append(
                {
                    "key": item.get("key"),
                    "label": item.get("label"),
                    "points": points,
                }
            )
    return formatted


def format_tuya_power_section(
    *,
    now: datetime | None = None,
    tz_name: str = KESTREL_DISPLAY_TZ,
) -> dict[str, Any]:
    """Build display payloads for the Tuya appliance power dashboard section."""
    ts_now = now or datetime.now(timezone.utc)
    status = read_tuya_power_status(now=ts_now)
    poll_error = read_tuya_poll_error()
    history = read_tuya_power_history()

    updated_display = None
    if status.get("updated_at"):
        updated_display = format_timestamp_friendly(
            str(status["updated_at"]),
            tz_name=tz_name,
            now=ts_now,
        )

    appliances: list[dict[str, Any]] = []
    for entry in status.get("appliances") or []:
        if not isinstance(entry, dict):
            continue
        energy_display, energy_inferred = _format_energy(
            energy_forward_kwh=entry.get("energy_forward_kwh"),
            energy_forward_kwh_inferred=entry.get("energy_forward_kwh_inferred"),
        )
        appliance_state = str(entry.get("state") or status.get("state") or "no_data")
        appliances.append(
            {
                "key": entry.get("key"),
                "label": entry.get("label"),
                "power_w": entry.get("power_w"),
                "power_display": _format_watts(entry.get("power_w")),
                "voltage_display": _format_voltage(entry.get("voltage_v")),
                "energy_display": energy_display,
                "energy_inferred": energy_inferred,
                "status": _STATUS_LABELS.get(appliance_state, appliance_state.title()),
                "style": _STATUS_STYLES.get(appliance_state, "unknown"),
            }
        )

    state = str(status.get("state") or "no_data")
    warning = status.get("warning")
    if poll_error and state in ("stale", "no_data", "error") and not warning:
        warning = "Tuya power poll failed"

    charts = {
        "power_1h": _format_chart_series(
            build_appliance_power_series(history, hours=1, now=ts_now),
            tz_name=tz_name,
            now=ts_now,
        ),
        "power_24h": _format_chart_series(
            build_appliance_power_series(history, hours=24, now=ts_now),
            tz_name=tz_name,
            now=ts_now,
        ),
    }

    return {
        "state": state,
        "status": _STATUS_LABELS.get(state, state.title()),
        "style": _STATUS_STYLES.get(state, "unknown"),
        "headline": status.get("headline", "No Tuya power data yet"),
        "warning": warning,
        "updated_at": updated_display,
        "updated_age": _format_age_seconds(status.get("age_seconds")),
        "source": status.get("source"),
        "limited": bool(status.get("limited")),
        "device_model": status.get("device_model"),
        "appliances": appliances,
        "charts": charts,
        "has_history": bool(charts["power_1h"] or charts["power_24h"]),
        "poll_error": {
            "timestamp": poll_error.get("timestamp") if poll_error else None,
            "error_type": poll_error.get("error_type") if poll_error else None,
            "last_success": poll_error.get("last_success") if poll_error else None,
        }
        if poll_error
        else None,
    }
