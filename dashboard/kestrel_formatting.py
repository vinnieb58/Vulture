"""Human-friendly formatting for the Kestrel dashboard card."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

KESTREL_DISPLAY_TZ = "America/Chicago"


def _parse_iso(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def _to_local(dt: datetime, tz_name: str = KESTREL_DISPLAY_TZ) -> datetime:
    return dt.astimezone(ZoneInfo(tz_name))


def format_kwh(value: float | None, *, decimals: int = 2) -> str | None:
    if value is None:
        return None
    return f"{value:.{decimals}f}"


def format_count(value: int | None) -> str | None:
    if value is None:
        return None
    return str(int(value))


def _format_time_12h(dt: datetime) -> str:
    hour = dt.hour % 12 or 12
    period = "AM" if dt.hour < 12 else "PM"
    if dt.minute == 0:
        return f"{hour}:00 {period}"
    return f"{hour}:{dt.minute:02d} {period}"


def _format_day_short(dt: datetime) -> str:
    return f"{dt.strftime('%a')} {dt.month}/{dt.day}"


def _format_day_long(dt: datetime) -> str:
    return f"{dt.strftime('%a %b')} {dt.day}"


def _format_time_range(start: datetime, end: datetime) -> str:
    start_period = "AM" if start.hour < 12 else "PM"
    end_period = "AM" if end.hour < 12 else "PM"
    end_str = _format_time_12h(end)
    if start_period == end_period:
        hour = start.hour % 12 or 12
        if start.minute == 0:
            start_str = f"{hour}:00"
        else:
            start_str = f"{hour}:{start.minute:02d}"
        return f"{start_str}–{end_str}"
    return f"{_format_time_12h(start)}–{end_str}"


def format_timestamp_friendly(
    iso: str,
    *,
    tz_name: str = KESTREL_DISPLAY_TZ,
    now: datetime | None = None,
) -> str:
    """Format an ISO timestamp for humans, e.g. Mon Jun 15, 1:00 PM or Yesterday 6:15 PM."""
    local = _to_local(_parse_iso(iso), tz_name)
    tz = ZoneInfo(tz_name)
    reference = now.astimezone(tz) if now is not None else datetime.now(tz)
    local_date = local.date()
    time_str = _format_time_12h(local)

    if local_date == reference.date() - timedelta(days=1):
        return f"Yesterday {time_str}"
    if local_date == reference.date():
        return f"Today {time_str}"

    return f"{_format_day_long(local)}, {time_str}"


def format_range_display(
    range_start: str | None,
    range_end: str | None,
    *,
    tz_name: str = KESTREL_DISPLAY_TZ,
    now: datetime | None = None,
) -> str | None:
    if range_start and range_end:
        start_label = format_timestamp_friendly(range_start, tz_name=tz_name, now=now)
        end_label = format_timestamp_friendly(range_end, tz_name=tz_name, now=now)
        return f"{start_label} → {end_label}"
    if range_start:
        return f"from {format_timestamp_friendly(range_start, tz_name=tz_name, now=now)}"
    if range_end:
        return f"through {format_timestamp_friendly(range_end, tz_name=tz_name, now=now)}"
    return None


def format_top_interval_display(
    interval: dict[str, Any],
    *,
    tz_name: str = KESTREL_DISPLAY_TZ,
) -> str:
    start_ts = str(interval["start_ts"])
    end_ts = interval.get("end_ts")
    kwh = float(interval["kwh"])
    est_kw = interval.get("estimated_peak_kw")

    local_start = _to_local(_parse_iso(start_ts), tz_name)
    day_part = _format_day_short(local_start)
    if end_ts:
        local_end = _to_local(_parse_iso(str(end_ts)), tz_name)
        time_part = _format_time_range(local_start, local_end)
    else:
        time_part = _format_time_12h(local_start)

    line = f"{day_part}, {time_part} — {format_kwh(kwh)} kWh"
    if isinstance(est_kw, (int, float)):
        line += f" / est. {format_kwh(float(est_kw))} kW"
    return line


def format_daily_total_display(
    day: str,
    kwh: float,
    *,
    tz_name: str = KESTREL_DISPLAY_TZ,
) -> str:
    parsed_day = date.fromisoformat(day)
    noon_local = datetime(
        parsed_day.year,
        parsed_day.month,
        parsed_day.day,
        12,
        0,
        tzinfo=ZoneInfo(tz_name),
    )
    return f"{_format_day_short(noon_local)} — {format_kwh(kwh)} kWh"


def format_day_chart_label(day: str, *, tz_name: str = KESTREL_DISPLAY_TZ) -> str:
    parsed_day = date.fromisoformat(day)
    noon_local = datetime(
        parsed_day.year,
        parsed_day.month,
        parsed_day.day,
        12,
        0,
        tzinfo=ZoneInfo(tz_name),
    )
    return f"{noon_local.strftime('%b')} {parsed_day.day}"


def format_hour_chart_label(hour: int) -> str:
    if hour == 0:
        return "12 AM"
    if hour < 12:
        return f"{hour} AM"
    if hour == 12:
        return "12 PM"
    return f"{hour - 12} PM"


def format_average_daily_label(day_count: int, requested_days: int) -> str:
    if day_count <= 0:
        return f"Avg daily (last {requested_days} days)"
    if day_count < requested_days:
        plural = "s" if day_count != 1 else ""
        return f"Avg daily (last {day_count} day{plural})"
    return f"Avg daily (last {requested_days} days)"


def format_average_daily_display(
    avg: dict[str, Any] | Any | None,
    *,
    requested_days: int,
) -> dict[str, str | None]:
    if avg is None:
        return {
            "label": format_average_daily_label(0, requested_days),
            "value": None,
        }
    if isinstance(avg, dict):
        day_count = int(avg.get("day_count", 0))
        kwh = avg.get("kwh")
    else:
        day_count = int(getattr(avg, "day_count", 0))
        kwh = getattr(avg, "kwh", None)
    return {
        "label": format_average_daily_label(day_count, requested_days),
        "value": format_kwh(kwh) if isinstance(kwh, (int, float)) else None,
    }


def format_kestrel_card_display(
    kestrel: dict[str, Any],
    metrics: dict[str, Any] | None = None,
    *,
    tz_name: str = KESTREL_DISPLAY_TZ,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Return display-ready strings for the Kestrel Nest card."""
    metrics = metrics or {}
    recent_daily_totals = []
    for row in metrics.get("recent_daily_totals") or []:
        if not isinstance(row, dict):
            continue
        day = row.get("day")
        kwh = row.get("kwh")
        if day and isinstance(kwh, (int, float)):
            recent_daily_totals.append(
                {
                    "day": str(day),
                    "kwh": float(kwh),
                    "display": format_daily_total_display(str(day), float(kwh), tz_name=tz_name),
                }
            )

    peak_interval_7 = metrics.get("peak_interval_7")
    peak_interval_7_display = None
    if peak_interval_7 is not None:
        peak_dict = {
            "start_ts": peak_interval_7.start_ts,
            "end_ts": peak_interval_7.end_ts,
            "kwh": peak_interval_7.kwh,
            "estimated_peak_kw": peak_interval_7.estimated_peak_kw,
        }
        peak_interval_7_display = format_top_interval_display(peak_dict, tz_name=tz_name)

    generated_at = kestrel.get("generated_at")
    generated_at_display = None
    if generated_at:
        generated_at_display = format_timestamp_friendly(
            str(generated_at),
            tz_name=tz_name,
            now=now,
        )

    avg_7 = format_average_daily_display(metrics.get("avg_daily_7"), requested_days=7)
    avg_30 = format_average_daily_display(metrics.get("avg_daily_30"), requested_days=30)

    return {
        "range": format_range_display(
            str(kestrel["range_start"]) if kestrel.get("range_start") else None,
            str(kestrel["range_end"]) if kestrel.get("range_end") else None,
            tz_name=tz_name,
            now=now,
        ),
        "total_kwh": format_kwh(kestrel.get("total_kwh")),
        "missing_interval_count": format_count(kestrel.get("missing_interval_count")),
        "generated_at": generated_at_display,
        "peak_interval_7": peak_interval_7_display,
        "avg_daily_7": avg_7,
        "avg_daily_30": avg_30,
        "recent_daily_totals": recent_daily_totals,
    }


def format_kestrel_detail_display(
    status: dict[str, Any],
    metrics: dict[str, Any],
    *,
    tz_name: str = KESTREL_DISPLAY_TZ,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Return display-ready strings and chart payloads for the /kestrel page."""
    range_start = metrics.get("range_start") or status.get("range_start")
    range_end = metrics.get("range_end") or status.get("range_end")
    peak_7 = metrics.get("peak_interval_7")
    peak_display = None
    if peak_7 is not None:
        peak_display = format_top_interval_display(
            {
                "start_ts": peak_7.start_ts,
                "end_ts": peak_7.end_ts,
                "kwh": peak_7.kwh,
                "estimated_peak_kw": peak_7.estimated_peak_kw,
            },
            tz_name=tz_name,
        )

    generated_at = status.get("generated_at")
    generated_at_display = None
    if generated_at:
        generated_at_display = format_timestamp_friendly(
            str(generated_at),
            tz_name=tz_name,
            now=now,
        )

    total_kwh = metrics.get("total_kwh")
    if total_kwh is None and status.get("total_kwh") is not None:
        total_kwh = status.get("total_kwh")

    avg_7 = format_average_daily_display(metrics.get("avg_daily_7"), requested_days=7)
    avg_30 = format_average_daily_display(metrics.get("avg_daily_30"), requested_days=30)

    daily_30 = [
        {
            "label": format_day_chart_label(row["day"], tz_name=tz_name),
            "kwh": row["kwh"],
        }
        for row in metrics.get("daily_30") or []
        if isinstance(row, dict) and row.get("day") is not None
    ]
    daily_full = [
        {
            "label": format_day_chart_label(row["day"], tz_name=tz_name),
            "kwh": row["kwh"],
        }
        for row in metrics.get("daily_full") or []
        if isinstance(row, dict) and row.get("day") is not None
    ]
    hourly_30 = [
        {
            "label": format_hour_chart_label(int(row["hour"])),
            "kwh": row["kwh"],
        }
        for row in metrics.get("hourly_30") or []
        if isinstance(row, dict) and row.get("hour") is not None
    ]
    top_intervals_30 = []
    for peak in metrics.get("top_intervals_30") or []:
        top_intervals_30.append(
            {
                "display": format_top_interval_display(
                    {
                        "start_ts": peak.start_ts,
                        "end_ts": peak.end_ts,
                        "kwh": peak.kwh,
                        "estimated_peak_kw": peak.estimated_peak_kw,
                    },
                    tz_name=tz_name,
                ),
                "kwh": peak.kwh,
            }
        )

    monthly_totals = []
    for row in metrics.get("monthly_totals") or []:
        if not isinstance(row, dict):
            continue
        month = str(row.get("month", ""))
        kwh = row.get("kwh")
        if not month or not isinstance(kwh, (int, float)):
            continue
        year, month_num = month.split("-", 1)
        label = datetime(int(year), int(month_num), 1, tzinfo=ZoneInfo(tz_name)).strftime("%b %Y")
        monthly_totals.append(
            {
                "label": label,
                "kwh": float(kwh),
                "display": f"{label} — {format_kwh(float(kwh))} kWh",
            }
        )

    has_data = bool(
        metrics.get("available")
        and (
            total_kwh
            or daily_30
            or status.get("state") == "available"
        )
    )

    return {
        "has_data": has_data,
        "range": format_range_display(
            str(range_start) if range_start else None,
            str(range_end) if range_end else None,
            tz_name=tz_name,
            now=now,
        ),
        "total_kwh": format_kwh(total_kwh) if isinstance(total_kwh, (int, float)) else None,
        "avg_daily_7": avg_7,
        "avg_daily_30": avg_30,
        "peak_interval_7": peak_display,
        "missing_interval_count": format_count(status.get("missing_interval_count")),
        "generated_at": generated_at_display,
        "last_refresh_status": status.get("last_refresh_status"),
        "last_refresh_source": status.get("last_refresh_source"),
        "last_refresh_message": status.get("last_refresh_message"),
        "last_refresh_success_at": (
            format_timestamp_friendly(str(status["last_refresh_success_at"]), tz_name=tz_name, now=now)
            if status.get("last_refresh_success_at")
            else None
        ),
        "warning": status.get("warning"),
        "charts": {
            "daily_30": daily_30,
            "daily_full": daily_full,
            "hourly_30": hourly_30,
            "top_intervals_30": top_intervals_30,
            "monthly_totals": monthly_totals,
        },
        "show_monthly": bool(metrics.get("show_monthly")),
    }
