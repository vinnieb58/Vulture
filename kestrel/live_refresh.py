"""Orchestrate Kestrel live refresh from Smart Meter Texas (API first, browser fallback)."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Literal

from kestrel.config import KestrelConfig
from kestrel.models import EnergyInterval, utc_now_iso
from kestrel.redact import describe_payload_shape, redact_text
from kestrel.smart_meter_texas import SmartMeterTexasError, fetch_intervals
from kestrel.smt_browser import fetch_intervals_via_browser
from kestrel.storage import upsert_intervals
from kestrel.summarize import PeakInterval, peak_interval, total_kwh

log = logging.getLogger(__name__)

RefreshSource = Literal["csv_import", "live_api", "live_browser"]
RefreshStatus = Literal["ok", "failed", "unsupported"]


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


def run_live_refresh(
    config: KestrelConfig,
    *,
    start: date,
    end: date,
    dry_run: bool = False,
    debug_safe: bool = False,
) -> LiveRefreshResult:
    """
    Attempt live refresh via portal API, then browser CSV export.

    Preserves the existing DB on failure. Upserts only on success (unless dry_run).
    """
    attempt_at = utc_now_iso()
    debug_dir = None
    if debug_safe:
        debug_dir = config.data_dir / "debug" / attempt_at.replace(":", "-")

    intervals: list[EnergyInterval] = []
    source: RefreshSource | None = None
    error_messages: list[str] = []

    try:
        intervals = fetch_intervals(config, start=start, end=end)
        source = "live_api"
        log.info("Live API fetch returned %s intervals", len(intervals))
    except SmartMeterTexasError as exc:
        safe_msg = redact_text(str(exc))
        error_messages.append(f"API: {safe_msg}")
        log.warning("Live API fetch failed: %s", safe_msg)
    except Exception as exc:  # noqa: BLE001 — probe boundary; preserve DB
        safe_msg = redact_text(str(exc))
        error_messages.append(f"API: unexpected error ({type(exc).__name__})")
        log.exception("Unexpected live API error")

    if not intervals:
        try:
            intervals = fetch_intervals_via_browser(
                config,
                start=start,
                end=end,
                debug_dir=debug_dir,
            )
            source = "live_browser"
            log.info("Browser fetch returned %s intervals", len(intervals))
        except SmartMeterTexasError as exc:
            safe_msg = redact_text(str(exc))
            error_messages.append(f"Browser: {safe_msg}")
            log.warning("Browser fetch failed: %s", safe_msg)
        except Exception as exc:  # noqa: BLE001
            error_messages.append(f"Browser: unexpected error ({type(exc).__name__})")
            log.exception("Unexpected browser fetch error")

    if not intervals:
        combined = "; ".join(error_messages) if error_messages else "No data source available"
        status: RefreshStatus = "unsupported" if not error_messages else "failed"
        metadata = RefreshMetadata(
            attempt_at=attempt_at,
            success_at=None,
            source=None,
            status=status,
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
    if dry_run:
        message = f"Dry run: {message}"

    metadata = RefreshMetadata(
        attempt_at=attempt_at,
        success_at=success_at,
        source=source,
        status="ok",
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


def log_unexpected_api_payload(payload: object) -> None:
    """Log a safe response-shape summary when the portal API shape changes."""
    log.warning(
        "Unexpected Smart Meter Texas API response shape: %s",
        describe_payload_shape(payload),
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
        status=status if status in ("ok", "failed", "unsupported") else "failed",
        message=redact_text(str(raw["last_refresh_message"])) if raw.get("last_refresh_message") else None,
    )
