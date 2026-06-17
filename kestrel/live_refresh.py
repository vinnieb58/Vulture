"""Orchestrate Kestrel live refresh from Smart Meter Texas (API first, browser fallback)."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

from kestrel.config import KestrelConfig
from kestrel.models import EnergyInterval, utc_now_iso
from kestrel.redact import redact_text
from kestrel.smart_meter_texas import (
    FetchIntervalsResult,
    SmartMeterTexasError,
    fetch_intervals_by_day,
)
from kestrel.smt_browser import fetch_intervals_via_browser
from kestrel.storage import upsert_intervals
from kestrel.summarize import PeakInterval, peak_interval, total_kwh

log = logging.getLogger(__name__)

RefreshSource = Literal["csv_import", "live_api", "live_browser"]
RefreshStatus = Literal["ok", "partial", "failed", "unsupported"]

COMPLETED_RANGE_NOTE = (
    "Note: SMT 15-minute interval data may lag 24-48 hours. "
    "Default --days range excludes the current local day."
)


@dataclass(frozen=True)
class RefreshMetadata:
    attempt_at: str
    success_at: str | None
    source: RefreshSource | None
    status: RefreshStatus
    message: str | None

    def to_dict(self) -> dict[str, str | None]:
        return {
            "last_refresh_attempt_at": self.attempt_at,
            "last_refresh_success_at": self.success_at,
            "last_refresh_source": self.source,
            "last_refresh_status": self.status,
            "last_refresh_message": self.message,
        }


@dataclass(frozen=True)
class LiveRefreshResult:
    metadata: RefreshMetadata
    intervals: list[EnergyInterval]
    inserted: int
    skipped: int
    attempted_start: date
    attempted_end: date
    min_interval_ts: str | None
    max_interval_ts: str | None
    fetched_total_kwh: float
    fetched_peak: PeakInterval | None


def resolve_live_refresh_days_range(
    *,
    days: int,
    timezone: str,
    include_current_day: bool = False,
) -> tuple[date, date]:
    """Return a completed-date lookback range in the configured local timezone."""
    if days < 1:
        raise ValueError("days must be at least 1")
    tz = ZoneInfo(timezone)
    today_local = datetime.now(tz).date()
    end = today_local if include_current_day else today_local - timedelta(days=1)
    start = end - timedelta(days=days - 1)
    return start, end


def _format_day_list(days: list[date]) -> str:
    return ", ".join(day.isoformat() for day in days)


def _should_try_browser(fetch_result: FetchIntervalsResult | None) -> bool:
    """Browser fallback is only for real API path failures, not TDSP data lag."""
    if fetch_result is None:
        return True
    if fetch_result.intervals:
        return False
    if fetch_result.data_lag_days and not fetch_result.failed_days:
        return False
    return bool(fetch_result.failed_days)


def _browser_error_message(exc: Exception, *, debug_safe: bool) -> str:
    if debug_safe:
        log.exception("Browser fallback error")
    else:
        log.warning("Browser fallback failed: %s", _safe_browser_error_label(exc))
    return f"Browser fallback failed: {_safe_browser_error_label(exc)}"


def _safe_browser_error_label(exc: Exception) -> str:
    text = redact_text(str(exc))
    if "ERR_HTTP2_PROTOCOL_ERROR" in text or "net::" in text:
        return "navigation error"
    name = type(exc).__name__
    if "Timeout" in name or "timeout" in text.lower():
        return "navigation timeout"
    return name


def run_live_refresh(
    config: KestrelConfig,
    *,
    start: date,
    end: date,
    dry_run: bool = False,
    debug_safe: bool = False,
) -> LiveRefreshResult:
    """
    Attempt live refresh via portal API, then browser CSV export for real failures.

    TDSP data lag on the newest requested day imports partial data without browser
    fallback. Preserves the existing DB on total failure.
    """
    attempt_at = utc_now_iso()
    debug_dir = None
    if debug_safe:
        debug_dir = config.data_dir / "debug" / attempt_at.replace(":", "-")

    intervals: list[EnergyInterval] = []
    source: RefreshSource | None = None
    error_messages: list[str] = []
    fetch_result: FetchIntervalsResult | None = None
    status: RefreshStatus = "failed"

    try:
        fetch_result = fetch_intervals_by_day(config, start=start, end=end)
        intervals = fetch_result.intervals
        source = "live_api"
        log.info("Live API fetch returned %s intervals", len(intervals))
    except SmartMeterTexasError as exc:
        safe_msg = redact_text(str(exc))
        error_messages.append(f"API: {safe_msg}")
        log.warning("Live API setup failed: %s", safe_msg)

    if intervals:
        if fetch_result and fetch_result.data_lag_days:
            status = "partial"
        else:
            status = "ok"
    elif fetch_result and fetch_result.data_lag_days and not fetch_result.failed_days:
        status = "failed"
        error_messages.append(
            "No published interval data for requested range (likely TDSP lag 24-48 hours)"
        )
    elif _should_try_browser(fetch_result):
        try:
            intervals = fetch_intervals_via_browser(
                config,
                start=start,
                end=end,
                debug_dir=debug_dir,
                debug_safe=debug_safe,
            )
            source = "live_browser"
            status = "ok"
            log.info("Browser fetch returned %s intervals", len(intervals))
        except SmartMeterTexasError as exc:
            safe_msg = redact_text(str(exc))
            error_messages.append(safe_msg)
            log.warning("%s", safe_msg)
        except Exception as exc:  # noqa: BLE001
            error_messages.append(_browser_error_message(exc, debug_safe=debug_safe))

    if not intervals:
        combined = "; ".join(error_messages) if error_messages else "No data source available"
        final_status: RefreshStatus = "unsupported" if not error_messages else status
        metadata = RefreshMetadata(
            attempt_at=attempt_at,
            success_at=None,
            source=None,
            status=final_status,
            message=redact_text(combined)[:500],
        )
        return LiveRefreshResult(
            metadata=metadata,
            intervals=[],
            inserted=0,
            skipped=0,
            attempted_start=start,
            attempted_end=end,
            min_interval_ts=None,
            max_interval_ts=None,
            fetched_total_kwh=0.0,
            fetched_peak=None,
        )

    inserted = 0
    skipped = 0
    if not dry_run:
        inserted, skipped = upsert_intervals(config.db_path, intervals)
    else:
        log.info("Dry run: skipping DB upsert for %s intervals", len(intervals))

    success_at = utc_now_iso()
    min_ts = min(row.start_ts for row in intervals)
    max_ts = max(row.end_ts for row in intervals)
    fetched_kwh = total_kwh(intervals)
    fetched_peak = peak_interval(intervals)

    message = (
        f"Fetched {len(intervals)} intervals for {start.isoformat()}..{end.isoformat()}; "
        f"inserted {inserted}, skipped {skipped} duplicates"
    )
    if status == "partial" and fetch_result and fetch_result.data_lag_days:
        lag_days = _format_day_list(fetch_result.data_lag_days)
        message += (
            f". Latest requested day(s) unavailable (likely TDSP lag 24-48 hours): {lag_days}"
        )
    if dry_run:
        message = f"Dry run: {message}"

    metadata = RefreshMetadata(
        attempt_at=attempt_at,
        success_at=success_at,
        source=source,
        status=status,
        message=redact_text(message),
    )
    return LiveRefreshResult(
        metadata=metadata,
        intervals=intervals,
        inserted=inserted,
        skipped=skipped,
        attempted_start=start,
        attempted_end=end,
        min_interval_ts=min_ts,
        max_interval_ts=max_ts,
        fetched_total_kwh=fetched_kwh,
        fetched_peak=fetched_peak,
    )


def build_csv_import_metadata(*, imported: int, inserted: int, skipped: int) -> RefreshMetadata:
    """Build refresh metadata after a successful CSV import."""
    now = utc_now_iso()
    message = redact_text(
        f"Imported {imported} intervals from CSV ({inserted} inserted, {skipped} skipped)"
    )
    return RefreshMetadata(
        attempt_at=now,
        success_at=now,
        source="csv_import",
        status="ok",
        message=message,
    )


def load_refresh_metadata_from_status(status_path: Path) -> RefreshMetadata | None:
    """Load prior refresh fields from an existing status JSON file."""
    if not status_path.is_file():
        return None
    try:
        import json

        raw = json.loads(status_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict):
        return None

    attempt_at = raw.get("last_refresh_attempt_at")
    if not attempt_at:
        return None

    source = raw.get("last_refresh_source")
    status = raw.get("last_refresh_status")
    return RefreshMetadata(
        attempt_at=str(attempt_at),
        success_at=str(raw["last_refresh_success_at"]) if raw.get("last_refresh_success_at") else None,
        source=source if source in ("csv_import", "live_api", "live_browser") else None,
        status=status if status in ("ok", "partial", "failed", "unsupported") else "failed",
        message=redact_text(str(raw["last_refresh_message"])) if raw.get("last_refresh_message") else None,
    )
