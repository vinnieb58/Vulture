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


def format_kestrel_card_display(
    kestrel: dict[str, Any],
    *,
    tz_name: str = KESTREL_DISPLAY_TZ,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Return display-ready strings for the Kestrel Nest card."""
    top_intervals = []
    for interval in kestrel.get("top_intervals") or []:
        if not isinstance(interval, dict):
            continue
        top_intervals.append(
            {
                **interval,
                "display": format_top_interval_display(interval, tz_name=tz_name),
            }
        )

    daily_totals = []
    for day, kwh in (kestrel.get("daily_totals") or {}).items():
        if not isinstance(kwh, (int, float)):
            continue
        daily_totals.append(
            {
                "day": str(day),
                "kwh": float(kwh),
                "display": format_daily_total_display(str(day), float(kwh), tz_name=tz_name),
            }
        )

    generated_at = kestrel.get("generated_at")
    generated_at_display = None
    if generated_at:
        generated_at_display = format_timestamp_friendly(
            str(generated_at),
            tz_name=tz_name,
            now=now,
        )

    return {
        "range": format_range_display(
            str(kestrel["range_start"]) if kestrel.get("range_start") else None,
            str(kestrel["range_end"]) if kestrel.get("range_end") else None,
            tz_name=tz_name,
            now=now,
        ),
        "total_kwh": format_kwh(kestrel.get("total_kwh")),
        "peak_interval_kwh": format_kwh(kestrel.get("peak_interval_kwh")),
        "estimated_peak_kw": format_kwh(kestrel.get("estimated_peak_kw")),
        "missing_interval_count": format_count(kestrel.get("missing_interval_count")),
        "interval_count": format_count(kestrel.get("interval_count")),
        "generated_at": generated_at_display,
        "top_intervals": top_intervals,
        "daily_totals": daily_totals,
    }
