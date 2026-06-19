"""Display formatting for Nest HVAC runtime and energy correlation."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from kestrel_formatting import KESTREL_DISPLAY_TZ, format_timestamp_friendly
from nest_energy_correlation import get_energy_hvac_correlation
from nest_hvac_runtime import (
    HvacRuntimeSummary,
    ThermostatRuntime,
    WINDOW_LAST_24H,
    get_hvac_runtime_summaries,
)


def _minutes_to_hours(minutes: float) -> str:
    if minutes <= 0:
        return "0h"
    hours = minutes / 60.0
    if hours < 0.05:
        return "<0.1h"
    if hours < 10:
        return f"{hours:.1f}h"
    return f"{hours:.0f}h"


def _format_percent(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{value:.0f}%"


def _format_thermostat_row(runtime: ThermostatRuntime) -> dict[str, str]:
    zone_label = runtime.zone.replace("_", " ").title()
    return {
        "zone": zone_label,
        "cooling": _minutes_to_hours(runtime.cooling_minutes),
        "heating": _minutes_to_hours(runtime.heating_minutes),
        "idle": _minutes_to_hours(runtime.idle_minutes),
        "percent_cooling": _format_percent(runtime.percent_time_cooling),
        "sample_count": str(runtime.sample_count),
    }


def _format_household_row(summary: HvacRuntimeSummary) -> dict[str, str]:
    household = summary.household
    return {
        "zone": "House any",
        "cooling": _minutes_to_hours(household.any_cooling_minutes),
        "heating": "—",
        "idle": "—",
        "percent_cooling": _format_percent(household.percent_time_any_cooling),
        "sample_count": str(household.sample_count),
        "both_cooling": _minutes_to_hours(household.both_cooling_minutes),
    }


def format_hvac_runtime_summary(summary: HvacRuntimeSummary) -> dict[str, Any]:
    rows = [_format_thermostat_row(runtime) for runtime in summary.thermostats]
    rows.append(_format_household_row(summary))
    return {
        "window": summary.window,
        "title": f"HVAC Runtime — {summary.window_label}",
        "rows": rows,
        "sample_count": summary.sample_count,
        "estimate_note": summary.estimate_note,
        "first_seen": summary.first_seen.isoformat() if summary.first_seen else None,
        "last_seen": summary.last_seen.isoformat() if summary.last_seen else None,
    }


def format_hvac_section(
    *,
    now: datetime | None = None,
    tz_name: str = KESTREL_DISPLAY_TZ,
) -> dict[str, Any]:
    """Build display payloads for HVAC runtime and energy correlation."""
    ts_now = now or datetime.now(timezone.utc)
    runtime = get_hvac_runtime_summaries(now=ts_now)
    correlation = get_energy_hvac_correlation(now=ts_now)

    summaries = [
        format_hvac_runtime_summary(summary)
        for summary in runtime.get("summaries") or []
    ]

    primary = next(
        (item for item in summaries if item.get("window") == WINDOW_LAST_24H),
        summaries[0] if summaries else None,
    )

    latest_sample_display = None
    latest_sample_at = runtime.get("latest_sample_at")
    if latest_sample_at:
        latest_sample_display = format_timestamp_friendly(
            str(latest_sample_at),
            tz_name=tz_name,
            now=ts_now,
        )

    correlation_rows = []
    for row in correlation.get("rows") or []:
        zone_actions = row.get("zone_actions") or {}
        correlation_rows.append(
            {
                "interval": row.get("interval_label") or "—",
                "kwh": row.get("kwh_display") or "—",
                "downstairs": zone_actions.get("downstairs") or "—",
                "upstairs": zone_actions.get("upstairs") or "—",
                "cooling": row.get("cooling_display") or "no",
                "note": row.get("note"),
            }
        )

    diagnostics = correlation.get("diagnostics") or {}
    correlation_diagnostics: list[dict[str, str]] = []
    window_start = diagnostics.get("window_start")
    window_end = diagnostics.get("window_end")
    if window_start and window_end:
        correlation_diagnostics.append(
            {
                "label": "Correlation window",
                "value": (
                    f"{format_timestamp_friendly(str(window_start), tz_name=tz_name, now=ts_now)}"
                    f" – {format_timestamp_friendly(str(window_end), tz_name=tz_name, now=ts_now)}"
                ),
            }
        )
    smt_latest = diagnostics.get("smt_latest")
    if smt_latest:
        correlation_diagnostics.append(
            {
                "label": "Latest SMT interval",
                "value": format_timestamp_friendly(str(smt_latest), tz_name=tz_name, now=ts_now),
            }
        )
    nest_earliest = diagnostics.get("nest_earliest")
    nest_latest = diagnostics.get("nest_latest")
    if nest_earliest:
        correlation_diagnostics.append(
            {
                "label": "Earliest Nest sample",
                "value": format_timestamp_friendly(str(nest_earliest), tz_name=tz_name, now=ts_now),
            }
        )
    if nest_latest:
        correlation_diagnostics.append(
            {
                "label": "Latest Nest sample",
                "value": format_timestamp_friendly(str(nest_latest), tz_name=tz_name, now=ts_now),
            }
        )
    interval_count = diagnostics.get("interval_count")
    if interval_count is not None:
        correlation_diagnostics.append(
            {
                "label": "SMT interval rows",
                "value": f"{int(interval_count):,}",
            }
        )

    warnings: list[str] = []
    for warning in (runtime.get("warning"), correlation.get("warning")):
        if isinstance(warning, str) and warning:
            warnings.append(warning)

    return {
        "state": runtime.get("state", "no_data"),
        "warning": warnings[0] if warnings else None,
        "warnings": warnings,
        "latest_sample_at": latest_sample_display,
        "age_minutes": runtime.get("age_minutes"),
        "primary_summary": primary,
        "summaries": summaries,
        "correlation": {
            "available": bool(correlation.get("available")),
            "status": correlation.get("status"),
            "warning": correlation.get("warning"),
            "rows": correlation_rows,
            "hours": correlation.get("hours"),
            "estimate_note": correlation.get("estimate_note"),
            "high_kwh_threshold": correlation.get("high_kwh_threshold"),
            "diagnostics": correlation_diagnostics,
        },
    }
