"""
Smart Meter Texas Kestrel probe
===============================
Read-only household energy probe. Stores 15-minute interval usage in Kestrel SQLite.

Does NOT modify Vulture scheduler, Crow commands, or hunt runtime.

Usage:
    python experiments/kestrel/smart_meter_texas_probe.py --import-csv /path/to/export.csv
    python experiments/kestrel/smart_meter_texas_probe.py --live-refresh --days 7
    python experiments/kestrel/smart_meter_texas_probe.py --live-refresh --from 2026-06-01 --to 2026-06-16
    python experiments/kestrel/smart_meter_texas_probe.py --live-refresh --days 2 --dry-run
    python experiments/kestrel/smart_meter_texas_probe.py --live-refresh --debug-safe
    python experiments/kestrel/smart_meter_texas_probe.py --summary-only
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from kestrel.config import (  # noqa: E402
    KestrelConfigError,
    PROVIDER_SMART_METER_TEXAS,
    load_config,
    setup_logging,
)
from kestrel.live_refresh import (  # noqa: E402
    COMPLETED_RANGE_NOTE,
    LiveRefreshResult,
    RefreshMetadata,
    build_csv_import_metadata,
    load_refresh_metadata_from_status,
    resolve_live_refresh_days_range,
    run_live_refresh,
)
from kestrel.smart_meter_texas import (  # noqa: E402
    SmartMeterTexasError,
    fetch_intervals,
    import_csv_file,
)
from kestrel.status_snapshot import build_status_snapshot
from kestrel.storage import fetch_intervals as load_stored_intervals  # noqa: E402
from kestrel.storage import upsert_intervals  # noqa: E402
from kestrel.summarize import summarize_intervals, top_intervals  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Kestrel Smart Meter Texas probe (read-only)")
    parser.add_argument("--days", type=int, help="Lookback days ending today (default: KESTREL_LOOKBACK_DAYS)")
    parser.add_argument("--from", dest="from_date", metavar="DATE", help="Start date YYYY-MM-DD")
    parser.add_argument("--to", dest="to_date", metavar="DATE", help="End date YYYY-MM-DD (inclusive)")
    parser.add_argument("--import-csv", metavar="PATH", help="Import a Smart Meter Texas CSV export")
    parser.add_argument(
        "--live-refresh",
        action="store_true",
        help="Log into Smart Meter Texas and fetch recent interval data (API, then browser fallback)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="With --live-refresh: fetch and summarize without writing to SQLite",
    )
    parser.add_argument(
        "--debug-safe",
        action="store_true",
        help="With --live-refresh: save redacted browser debug artifacts under data/kestrel/debug/",
    )
    parser.add_argument(
        "--include-current-day",
        action="store_true",
        help="With --live-refresh --days: include the current local day (may be unpublished)",
    )
    parser.add_argument(
        "--no-browser-fallback",
        action="store_true",
        help="With --live-refresh: API only; do not use Playwright browser fallback",
    )
    parser.add_argument(
        "--summary-only",
        action="store_true",
        help="Summarize existing SQLite data without fetching or importing",
    )
    parser.add_argument(
        "--anomaly-threshold-kwh",
        type=float,
        default=None,
        help="Flag intervals at or above this kWh value",
    )
    return parser.parse_args()


def _parse_cli_date(value: str, label: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise SystemExit(f"Invalid {label} date {value!r}; use YYYY-MM-DD.") from exc


def _resolve_range(
    args: argparse.Namespace, config
) -> tuple[date | None, date | None, str | None]:
    if args.summary_only or args.import_csv:
        return None, None, None

    if args.from_date or args.to_date:
        if not args.from_date or not args.to_date:
            raise SystemExit("Both --from and --to are required when specifying a custom range.")
        start = _parse_cli_date(args.from_date, "start")
        end = _parse_cli_date(args.to_date, "end")
        if end < start:
            raise SystemExit("End date must be on or after start date.")
        return start, end, None

    lookback = args.days if args.days is not None else config.lookback_days
    if args.live_refresh:
        start, end = resolve_live_refresh_days_range(
            days=lookback,
            timezone=config.timezone,
            include_current_day=args.include_current_day,
        )
        note = None if args.include_current_day else COMPLETED_RANGE_NOTE
        return start, end, note

    end = date.today()
    start = end - timedelta(days=lookback - 1)
    return start, end, None


def _requires_live_access(args: argparse.Namespace) -> bool:
    if args.import_csv or args.summary_only:
        return False
    return args.live_refresh or not args.import_csv


def _print_summary(
    summary,
    *,
    imported: int,
    inserted: int,
    skipped: int,
    top5,
    refresh: RefreshMetadata | None = None,
    live_result: LiveRefreshResult | None = None,
) -> None:
    print("")
    print("Kestrel Smart Meter Texas summary")
    print("--------------------------------")
    if live_result is not None:
        print(f"Attempted range      : {live_result.attempted_start} -> {live_result.attempted_end}")
        print(f"Intervals fetched    : {len(live_result.intervals)}")
    print(f"Intervals in summary : {summary.interval_count}")
    print(f"Intervals imported   : {imported}")
    print(f"Rows inserted        : {inserted}")
    print(f"Rows skipped (dupes) : {skipped}")
    if live_result and live_result.min_interval_ts and live_result.max_interval_ts:
        print(f"Fetched min interval : {live_result.min_interval_ts}")
        print(f"Fetched max interval : {live_result.max_interval_ts}")
        print(f"Fetched total kWh    : {live_result.fetched_total_kwh:.4f}")
        if live_result.fetched_peak:
            peak = live_result.fetched_peak
            print(
                f"Fetched peak interval: {peak.kwh:.4f} kWh "
                f"({peak.start_ts} -> {peak.end_ts})"
            )
    if summary.range_start and summary.range_end:
        print(f"Range                : {summary.range_start} -> {summary.range_end}")
    print(f"Total kWh            : {summary.total_kwh:.4f}")
    if summary.peak_interval:
        peak = summary.peak_interval
        print(
            f"Peak 15-min interval : {peak.kwh:.4f} kWh "
            f"({peak.start_ts} -> {peak.end_ts})"
        )
        print(
            f"Estimated peak kW    : {peak.estimated_peak_kw:.4f} "
            "(from 15-minute interval data; not instantaneous demand)"
        )
    print(f"Missing intervals    : {summary.missing_interval_count}")
    if refresh is not None:
        print("")
        print("Last refresh status")
        print("-------------------")
        print(f"Attempt at           : {refresh.attempt_at}")
        print(f"Success at           : {refresh.success_at or '—'}")
        print(f"Source               : {refresh.source or '—'}")
        print(f"Status               : {refresh.status}")
        if refresh.message:
            print(f"Message              : {refresh.message}")
    print("")
    print("Top 5 intervals:")
    for rank, peak in enumerate(top5, start=1):
        print(
            f"  {rank}. {peak.kwh:.4f} kWh @ {peak.start_ts} "
            f"(est. {peak.estimated_peak_kw:.4f} kW)"
        )


def main() -> int:
    args = parse_args()

    if args.dry_run and not args.live_refresh:
        print("ERROR: --dry-run requires --live-refresh.", file=sys.stderr)
        return 1
    if args.debug_safe and not args.live_refresh:
        print("ERROR: --debug-safe requires --live-refresh.", file=sys.stderr)
        return 1
    if args.include_current_day and not args.live_refresh:
        print("ERROR: --include-current-day requires --live-refresh.", file=sys.stderr)
        return 1
    if args.no_browser_fallback and not args.live_refresh:
        print("ERROR: --no-browser-fallback requires --live-refresh.", file=sys.stderr)
        return 1

    live_access = _requires_live_access(args)
    try:
        config = load_config(
            require_enabled=live_access,
            require_credentials=live_access,
        )
    except KestrelConfigError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    log = setup_logging(config.log_level)
    start, end, range_note = _resolve_range(args, config)
    status_path = config.data_dir / "kestrel_status.json"
    status_path.parent.mkdir(parents=True, exist_ok=True)

    imported = 0
    inserted = 0
    skipped = 0
    range_start_dt: datetime | None = None
    range_end_dt: datetime | None = None
    refresh_metadata: RefreshMetadata | None = load_refresh_metadata_from_status(status_path)
    live_result: LiveRefreshResult | None = None

    if args.import_csv:
        try:
            intervals = import_csv_file(
                args.import_csv,
                tz_name=config.timezone,
                account_id=config.smt_account_id,
            )
        except SmartMeterTexasError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1
        imported = len(intervals)
        inserted, skipped = upsert_intervals(config.db_path, intervals)
        refresh_metadata = build_csv_import_metadata(
            imported=imported,
            inserted=inserted,
            skipped=skipped,
        )
        log.info("Imported %s intervals from CSV (%s inserted, %s skipped)", imported, inserted, skipped)
    elif args.live_refresh:
        assert start is not None and end is not None
        if range_note:
            print(range_note)
        range_start_dt = datetime.combine(start, datetime.min.time(), tzinfo=timezone.utc)
        range_end_dt = datetime.combine(end + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc)
        live_result = run_live_refresh(
            config,
            start=start,
            end=end,
            dry_run=args.dry_run,
            debug_safe=args.debug_safe,
            no_browser_fallback=args.no_browser_fallback,
        )
        refresh_metadata = live_result.metadata
        imported = len(live_result.intervals)
        inserted = live_result.inserted
        skipped = live_result.skipped
        if live_result.metadata.status in ("failed", "unsupported"):
            print(f"ERROR: Live refresh {live_result.metadata.status}.", file=sys.stderr)
            if live_result.metadata.message:
                print(f"  {live_result.metadata.message}", file=sys.stderr)
            print(
                "TIP: Export a 15-minute interval CSV from the Smart Meter Texas dashboard and run "
                "with --import-csv /path/to/export.csv",
                file=sys.stderr,
            )
        elif live_result.metadata.status == "partial" and live_result.metadata.message:
            print(f"WARNING: {live_result.metadata.message}", file=sys.stderr)
    elif not args.summary_only:
        assert start is not None and end is not None
        range_start_dt = datetime.combine(start, datetime.min.time(), tzinfo=timezone.utc)
        range_end_dt = datetime.combine(end + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc)
        try:
            intervals = fetch_intervals(config, start=start, end=end)
        except SmartMeterTexasError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            print(
                "TIP: Export a 15-minute interval CSV from the Smart Meter Texas dashboard and run "
                "with --import-csv /path/to/export.csv",
                file=sys.stderr,
            )
            return 1
        imported = len(intervals)
        inserted, skipped = upsert_intervals(config.db_path, intervals)
        log.info("Fetched %s intervals from SMT (%s inserted, %s skipped)", imported, inserted, skipped)

    query_start = range_start_dt.isoformat() if range_start_dt else None
    query_end = range_end_dt.isoformat() if range_end_dt else None
    stored = load_stored_intervals(
        config.db_path,
        provider=PROVIDER_SMART_METER_TEXAS,
        start_ts=query_start,
        end_ts=query_end,
    )

    if args.summary_only and not stored:
        print("No stored Smart Meter Texas intervals found.", file=sys.stderr)
        return 1

    summary = summarize_intervals(
        stored,
        tz_name=config.timezone,
        range_start=range_start_dt,
        range_end=range_end_dt,
        anomaly_threshold_kwh=args.anomaly_threshold_kwh,
        anomaly_top_n=5,
    )

    top5 = top_intervals(stored, 5)
    _print_summary(
        summary,
        imported=imported,
        inserted=inserted,
        skipped=skipped,
        top5=top5,
        refresh=refresh_metadata,
        live_result=live_result,
    )

    status_snapshot = build_status_snapshot(
        summary,
        top5,
        provider=PROVIDER_SMART_METER_TEXAS,
        refresh=refresh_metadata,
    )
    status_path.write_text(
        json.dumps(status_snapshot, indent=2) + "\n",
        encoding="utf-8",
    )
    log.info("Wrote status snapshot")

    if live_result is not None and live_result.metadata.status in ("failed", "unsupported"):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
