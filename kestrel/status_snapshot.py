"""Build sanitized Kestrel status JSON for dashboard and operator snapshots."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from kestrel.summarize import IntervalSummary, PeakInterval


def _interval_dict(peak: PeakInterval) -> dict[str, Any]:
    return {
        "start_ts": peak.start_ts,
        "end_ts": peak.end_ts,
        "kwh": peak.kwh,
        "estimated_kw": peak.estimated_peak_kw,
    }


def _daily_totals_list(daily: dict[str, float]) -> list[dict[str, Any]]:
    return [{"date": day, "kwh": value} for day, value in sorted(daily.items())]


def build_status_snapshot(
    summary: IntervalSummary,
    top: list[PeakInterval],
    *,
    provider: str,
    last_updated: str | None = None,
) -> dict[str, Any]:
    """
    Build a dashboard-safe Kestrel status object.

    Never includes account identifiers, meter identifiers, hashes, credentials,
    raw_source, or database paths.
    """
    updated = last_updated or datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    has_data = summary.interval_count > 0 or summary.total_kwh > 0

    snapshot: dict[str, Any] = {
        "status": "available" if has_data else "no_data",
        "provider": provider,
        "last_updated": updated,
        "range_start": summary.range_start,
        "range_end": summary.range_end,
        "interval_count": summary.interval_count,
        "total_kwh": summary.total_kwh,
        "peak_interval": _interval_dict(summary.peak_interval) if summary.peak_interval else None,
        "estimated_peak_kw": summary.estimated_peak_kw,
        "missing_interval_count": summary.missing_interval_count,
        "top_intervals": [_interval_dict(peak) for peak in top],
        "daily_totals": _daily_totals_list(summary.daily_totals),
    }
    return snapshot
