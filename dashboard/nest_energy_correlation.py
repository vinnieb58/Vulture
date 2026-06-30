"""Correlate Smart Meter Texas interval usage with Nest HVAC history."""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from kestrel_metrics import (
    KESTREL_TIMEZONE,
    energy_db_exists,
    fetch_interval_rows,
    get_interval_count,
    get_range_bounds,
    _parse_iso,
)
from nest_history import NestHistoryRecord, read_history
from nest_hvac_runtime import HVAC_ACTION_COOLING, NEST_HISTORY_PATH, _thermostat_action

DEFAULT_CORRELATION_HOURS = 24
HIGH_KWH_THRESHOLD = float(os.environ.get("NEST_HVAC_HIGH_KWH_THRESHOLD", "1.0"))
PREFERRED_ZONES = ("downstairs", "upstairs")

STATUS_AVAILABLE = "available"
STATUS_NO_SMT_DB = "no_smt_db"
STATUS_NO_SMT_DATA = "no_smt_data"
STATUS_EMPTY_SMT = "no_smt_data"
STATUS_NO_NEST_HISTORY = "no_nest_data"
STATUS_NO_NEST_DATA = "no_nest_data"
STATUS_NO_OVERLAP = "no_overlap"
STATUS_NO_ROWS_IN_WINDOW = "no_rows_in_window"
STATUS_NO_MATCHED_ROWS = "no_matched_rows"

WARNING_NO_SMT_DB = "Smart Meter Texas database not found"
WARNING_NO_SMT_DATA = "No Smart Meter Texas interval data"
WARNING_EMPTY_SMT = WARNING_NO_SMT_DATA
WARNING_NO_NEST_DATA = "No Nest HVAC history for correlation"
WARNING_NO_NEST_HISTORY = WARNING_NO_NEST_DATA
WARNING_NO_OVERLAP = (
    "Smart Meter Texas data exists, but there is no overlap with Nest HVAC history yet."
)
WARNING_NO_ROWS_IN_WINDOW = (
    "Smart Meter Texas and Nest HVAC history overlap, but no interval rows fall in the "
    "selected correlation window."
)
WARNING_NO_MATCHED_ROWS = (
    "Smart Meter Texas intervals were found in the selected correlation window, but none "
    "could be matched to Nest HVAC samples."
)


def _format_interval_label(start: datetime, end: datetime, *, tz_name: str) -> str:
    tz = ZoneInfo(tz_name)
    start_local = start.astimezone(tz)
    end_local = end.astimezone(tz)

    def _time(dt: datetime) -> str:
        hour = dt.hour % 12 or 12
        minute = f":{dt.minute:02d}" if dt.minute else ""
        period = "AM" if dt.hour < 12 else "PM"
        return f"{hour}{minute} {period}"

    return f"{_time(start_local)}–{_time(end_local)}"


def _nest_history_bounds(
    records: list[NestHistoryRecord],
) -> tuple[datetime | None, datetime | None]:
    if not records:
        return None, None
    timestamps = [record.timestamp for record in records]
    return min(timestamps), max(timestamps)


def _ranges_overlap(
    left_start: datetime,
    left_end: datetime,
    right_start: datetime,
    right_end: datetime,
) -> bool:
    return left_start < right_end and right_start < left_end


def _compute_correlation_window(
    *,
    smt_start: datetime,
    smt_end: datetime,
    nest_start: datetime,
    nest_end: datetime,
    hours: int,
) -> tuple[datetime, datetime, datetime, datetime] | None:
    """Return global overlap bounds and the selected correlation window."""
    if not _ranges_overlap(smt_start, smt_end, nest_start, nest_end):
        return None

    global_overlap_start = max(smt_start, nest_start)
    global_overlap_end = min(smt_end, nest_end)
    window_end = global_overlap_end
    window_start = max(
        window_end - timedelta(hours=hours),
        smt_start,
        nest_start,
    )
    return global_overlap_start, global_overlap_end, window_start, window_end


def _count_nest_samples_in_window(
    records: list[NestHistoryRecord],
    *,
    window_start: datetime,
    window_end: datetime,
) -> int:
    return sum(
        1
        for record in records
        if window_start <= record.timestamp < window_end
    )


def _build_diagnostics(
    *,
    window_start: datetime,
    window_end: datetime,
    smt_start: str | None = None,
    smt_end: str | None = None,
    nest_start: datetime | None = None,
    nest_end: datetime | None = None,
    interval_count: int | None = None,
    overlap_start: datetime | None = None,
    overlap_end: datetime | None = None,
    smt_rows_in_window: int | None = None,
    nest_samples_in_window: int | None = None,
    matched_correlation_rows: int | None = None,
) -> dict[str, Any]:
    return {
        "window_start": window_start.isoformat(),
        "window_end": window_end.isoformat(),
        "overlap_start": overlap_start.isoformat() if overlap_start else None,
        "overlap_end": overlap_end.isoformat() if overlap_end else None,
        "smt_earliest": smt_start,
        "smt_latest": smt_end,
        "nest_earliest": nest_start.isoformat() if nest_start else None,
        "nest_latest": nest_end.isoformat() if nest_end else None,
        "interval_count": interval_count,
        "smt_rows_in_window": smt_rows_in_window,
        "nest_samples_in_window": nest_samples_in_window,
        "matched_correlation_rows": matched_correlation_rows,
    }


def _unavailable_result(
    *,
    status: str,
    warning: str,
    hours: int,
    diagnostics: dict[str, Any],
) -> dict[str, Any]:
    return {
        "available": False,
        "status": status,
        "warning": warning,
        "rows": [],
        "hours": hours,
        "high_kwh_threshold": HIGH_KWH_THRESHOLD,
        "diagnostics": diagnostics,
    }


def _latest_sample_in_interval(
    records: list[NestHistoryRecord],
    *,
    start: datetime,
    end: datetime,
) -> NestHistoryRecord | None:
    candidates = [
        record
        for record in records
        if start <= record.timestamp < end
    ]
    if candidates:
        return max(candidates, key=lambda record: record.timestamp)

    prior = [record for record in records if record.timestamp < end]
    if not prior:
        return None
    nearest = max(prior, key=lambda record: record.timestamp)
    if end - nearest.timestamp > timedelta(minutes=10):
        return None
    return nearest


def _zone_action(record: NestHistoryRecord | None, zone: str) -> str | None:
    if record is None:
        return None
    return _thermostat_action(record, zone)


def _correlation_note(*, kwh: float, any_cooling: bool) -> str | None:
    if any_cooling and kwh >= HIGH_KWH_THRESHOLD:
        return f"High usage ({kwh:.2f} kWh) during cooling"
    return None


def correlate_energy_intervals(
    energy_rows: list[dict[str, Any]],
    nest_records: list[NestHistoryRecord],
    *,
    zones: tuple[str, ...] = PREFERRED_ZONES,
    tz_name: str = KESTREL_TIMEZONE,
) -> list[dict[str, Any]]:
    """Join 15-minute energy intervals with Nest HVAC samples."""
    rows: list[dict[str, Any]] = []
    for energy in energy_rows:
        start = _parse_iso(str(energy["start_ts"]))
        end = _parse_iso(str(energy["end_ts"]))
        kwh = float(energy["kwh"])
        sample = _latest_sample_in_interval(nest_records, start=start, end=end)

        zone_actions: dict[str, str | None] = {
            zone: _zone_action(sample, zone) for zone in zones
        }
        cooling_flags = [
            action == HVAC_ACTION_COOLING
            for action in zone_actions.values()
            if action is not None
        ]
        any_cooling = bool(cooling_flags) and any(cooling_flags)

        rows.append(
            {
                "interval_label": _format_interval_label(start, end, tz_name=tz_name),
                "start_ts": start.isoformat(),
                "end_ts": end.isoformat(),
                "kwh": round(kwh, 4),
                "kwh_display": f"{kwh:.2f}",
                "zone_actions": zone_actions,
                "any_cooling": any_cooling,
                "cooling_display": "yes" if any_cooling else "no",
                "note": _correlation_note(kwh=kwh, any_cooling=any_cooling),
                "nest_sample_at": sample.timestamp.isoformat() if sample else None,
            }
        )
    return rows


def get_energy_hvac_correlation(
    *,
    history_path: Path | None = None,
    hours: int = DEFAULT_CORRELATION_HOURS,
    now: datetime | None = None,
    tz_name: str = KESTREL_TIMEZONE,
) -> dict[str, Any]:
    """Build Energy + HVAC correlation rows for the dashboard."""
    smt_start_raw, smt_end_raw = get_range_bounds()
    interval_count = get_interval_count()
    nest_records = read_history(history_path or NEST_HISTORY_PATH)
    nest_start, nest_end = _nest_history_bounds(nest_records)

    smt_start = _parse_iso(smt_start_raw) if smt_start_raw else None
    smt_end = _parse_iso(smt_end_raw) if smt_end_raw else None

    window_bounds: tuple[datetime, datetime] | None = None
    overlap_start: datetime | None = None
    overlap_end: datetime | None = None
    if (
        smt_start is not None
        and smt_end is not None
        and nest_start is not None
        and nest_end is not None
    ):
        computed = _compute_correlation_window(
            smt_start=smt_start,
            smt_end=smt_end,
            nest_start=nest_start,
            nest_end=nest_end,
            hours=hours,
        )
        if computed is not None:
            overlap_start, overlap_end, window_start, window_end = computed
            if window_start < window_end:
                window_bounds = window_start, window_end
            elif overlap_start < overlap_end:
                window_bounds = None
            else:
                overlap_start = overlap_end

    if window_bounds is None:
        placeholder_start = overlap_start or (now or datetime.now(timezone.utc))
        placeholder_end = overlap_end or placeholder_start
    else:
        placeholder_start, placeholder_end = window_bounds

    diagnostics = _build_diagnostics(
        window_start=placeholder_start,
        window_end=placeholder_end,
        smt_start=smt_start_raw,
        smt_end=smt_end_raw,
        nest_start=nest_start,
        nest_end=nest_end,
        interval_count=interval_count,
        overlap_start=overlap_start,
        overlap_end=overlap_end,
    )

    if not energy_db_exists():
        return _unavailable_result(
            status=STATUS_NO_SMT_DB,
            warning=WARNING_NO_SMT_DB,
            hours=hours,
            diagnostics=diagnostics,
        )

    if not smt_start_raw or not smt_end_raw or not interval_count:
        return _unavailable_result(
            status=STATUS_NO_SMT_DATA,
            warning=WARNING_NO_SMT_DATA,
            hours=hours,
            diagnostics=diagnostics,
        )

    assert smt_start is not None and smt_end is not None

    if not nest_records or nest_start is None or nest_end is None:
        return _unavailable_result(
            status=STATUS_NO_NEST_DATA,
            warning=WARNING_NO_NEST_DATA,
            hours=hours,
            diagnostics=diagnostics,
        )

    if not _ranges_overlap(smt_start, smt_end, nest_start, nest_end):
        return _unavailable_result(
            status=STATUS_NO_OVERLAP,
            warning=WARNING_NO_OVERLAP,
            hours=hours,
            diagnostics=diagnostics,
        )

    if window_bounds is None:
        diagnostics = _build_diagnostics(
            window_start=placeholder_start,
            window_end=placeholder_end,
            smt_start=smt_start_raw,
            smt_end=smt_end_raw,
            nest_start=nest_start,
            nest_end=nest_end,
            interval_count=interval_count,
            overlap_start=overlap_start,
            overlap_end=overlap_end,
        )
        return _unavailable_result(
            status=STATUS_NO_ROWS_IN_WINDOW,
            warning=WARNING_NO_ROWS_IN_WINDOW,
            hours=hours,
            diagnostics=diagnostics,
        )

    window_start, window_end = window_bounds
    energy_rows = fetch_interval_rows(
        start_ts=window_start.isoformat(),
        end_ts=window_end.isoformat(),
    )
    nest_samples_in_window = _count_nest_samples_in_window(
        nest_records,
        window_start=window_start,
        window_end=window_end,
    )
    diagnostics = _build_diagnostics(
        window_start=window_start,
        window_end=window_end,
        smt_start=smt_start_raw,
        smt_end=smt_end_raw,
        nest_start=nest_start,
        nest_end=nest_end,
        interval_count=interval_count,
        overlap_start=overlap_start,
        overlap_end=overlap_end,
        smt_rows_in_window=len(energy_rows),
        nest_samples_in_window=nest_samples_in_window,
    )

    if not energy_rows:
        return _unavailable_result(
            status=STATUS_NO_ROWS_IN_WINDOW,
            warning=WARNING_NO_ROWS_IN_WINDOW,
            hours=hours,
            diagnostics=diagnostics,
        )

    rows = correlate_energy_intervals(energy_rows, nest_records, tz_name=tz_name)
    matched_rows = sum(1 for row in rows if row.get("nest_sample_at"))
    diagnostics = _build_diagnostics(
        window_start=window_start,
        window_end=window_end,
        smt_start=smt_start_raw,
        smt_end=smt_end_raw,
        nest_start=nest_start,
        nest_end=nest_end,
        interval_count=interval_count,
        overlap_start=overlap_start,
        overlap_end=overlap_end,
        smt_rows_in_window=len(energy_rows),
        nest_samples_in_window=nest_samples_in_window,
        matched_correlation_rows=matched_rows,
    )
    if matched_rows == 0:
        return _unavailable_result(
            status=STATUS_NO_MATCHED_ROWS,
            warning=WARNING_NO_MATCHED_ROWS,
            hours=hours,
            diagnostics=diagnostics,
        )
    return {
        "available": True,
        "status": STATUS_AVAILABLE,
        "warning": None,
        "rows": rows,
        "hours": hours,
        "high_kwh_threshold": HIGH_KWH_THRESHOLD,
        "diagnostics": diagnostics,
        "estimate_note": (
            "HVAC actions reflect the latest Nest poll within each 15-minute "
            f"interval (latest overlapping {hours}h window)."
        ),
    }


def diagnose_energy_hvac_correlation(
    *,
    history_path: Path | None = None,
    hours: int = DEFAULT_CORRELATION_HOURS,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Return a diagnostic snapshot for local troubleshooting."""
    result = get_energy_hvac_correlation(
        history_path=history_path,
        hours=hours,
        now=now,
    )
    diagnostics = result.get("diagnostics") or {}
    return {
        "status": result.get("status"),
        "available": result.get("available"),
        "warning": result.get("warning"),
        "nest_earliest": diagnostics.get("nest_earliest"),
        "nest_latest": diagnostics.get("nest_latest"),
        "smt_earliest": diagnostics.get("smt_earliest"),
        "smt_latest": diagnostics.get("smt_latest"),
        "overlap_start": diagnostics.get("overlap_start"),
        "overlap_end": diagnostics.get("overlap_end"),
        "window_start": diagnostics.get("window_start"),
        "window_end": diagnostics.get("window_end"),
        "smt_rows_in_window": diagnostics.get("smt_rows_in_window"),
        "nest_samples_in_window": diagnostics.get("nest_samples_in_window"),
        "matched_correlation_rows": diagnostics.get("matched_correlation_rows"),
        "correlation_rows": len(result.get("rows") or []),
    }


def _print_correlation_diagnostics(payload: dict[str, Any]) -> None:
    fields = (
        ("status", "Status"),
        ("available", "Available"),
        ("warning", "Warning"),
        ("nest_earliest", "Nest history earliest"),
        ("nest_latest", "Nest history latest"),
        ("smt_earliest", "SMT earliest"),
        ("smt_latest", "SMT latest"),
        ("overlap_start", "Overlap start"),
        ("overlap_end", "Overlap end"),
        ("window_start", "Correlation window start"),
        ("window_end", "Correlation window end"),
        ("smt_rows_in_window", "SMT rows in window"),
        ("nest_samples_in_window", "Nest samples in window"),
        ("matched_correlation_rows", "Matched correlation rows"),
        ("correlation_rows", "Correlation rows returned"),
    )
    for key, label in fields:
        value = payload.get(key)
        if value is not None:
            print(f"{label}: {value}")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Print Nest/SMT energy correlation diagnostics.",
    )
    parser.add_argument(
        "--history-path",
        type=Path,
        default=None,
        help="Path to Nest history JSONL (defaults to NEST_HISTORY_PATH).",
    )
    parser.add_argument(
        "--hours",
        type=int,
        default=DEFAULT_CORRELATION_HOURS,
        help="Correlation window length in hours.",
    )
    args = parser.parse_args()
    _print_correlation_diagnostics(
        diagnose_energy_hvac_correlation(
            history_path=args.history_path,
            hours=args.hours,
        )
    )
