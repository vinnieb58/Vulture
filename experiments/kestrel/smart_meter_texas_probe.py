"""
Smart Meter Texas Kestrel probe
===============================
Read-only household energy probe. Stores 15-minute interval usage in Kestrel SQLite.

Does NOT modify Vulture scheduler, Crow commands, or hunt runtime.

Usage:
    python experiments/kestrel/smart_meter_texas_probe.py --import-csv /path/to/export.csv
    python experiments/kestrel/smart_meter_texas_probe.py --days 7
    python experiments/kestrel/smart_meter_texas_probe.py --from 2026-06-01 --to 2026-06-16
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


def _resolve_range(args: argparse.Namespace, config) -> tuple[date | None, date | None]:
    if args.summary_only or args.import_csv:
        return None, None

    if args.from_date or args.to_date:
        if not args.from_date or not args.to_date:
            raise SystemExit("Both --from and --to are required when specifying a custom range.")
        start = _parse_cli_date(args.from_date, "start")
        end = _parse_cli_date(args.to_date, "end")
        if end < start:
            raise SystemExit("End date must be on or after start date.")
        return start, end

    lookback = args.days if args.days is not None else config.lookback_days
    end = date.today()
    start = end - timedelta(days=lookback - 1)
    return start, end


def _print_summary(summary, *, imported: int, inserted: int, skipped: int, top5) -> None:
    print("")
    print("Kestrel Smart Meter Texas summary")
    print("--------------------------------")
    print(f"Intervals in summary : {summary.interval_count}")
    print(f"Intervals imported   : {imported}")
    print(f"Rows inserted        : {inserted}")
    print(f"Rows skipped (dupes) : {skipped}")
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
    print("")
    print("Top 5 intervals:")
    for rank, peak in enumerate(top5, start=1):
        print(
            f"  {rank}. {peak.kwh:.4f} kWh @ {peak.start_ts} "
            f"(est. {peak.estimated_peak_kw:.4f} kW)"
        )


def main() -> int:
    args = parse_args()
    try:
        config = load_config(
            require_enabled=not args.import_csv and not args.summary_only,
            require_credentials=not args.import_csv and not args.summary_only,
        )
    except KestrelConfigError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    log = setup_logging(config.log_level)
    start, end = _resolve_range(args, config)

    imported = 0
    inserted = 0
    skipped = 0
    range_start_dt: datetime | None = None
    range_end_dt: datetime | None = None

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
        log.info("Imported %s intervals from CSV (%s inserted, %s skipped)", imported, inserted, skipped)
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
    )

    status_path = config.data_dir / "kestrel_status.json"
    status_path.parent.mkdir(parents=True, exist_ok=True)
    status_snapshot = build_status_snapshot(
        summary,
        top5,
        provider=PROVIDER_SMART_METER_TEXAS,
    )
    status_path.write_text(
        json.dumps(status_snapshot, indent=2) + "\n",
        encoding="utf-8",
    )
    log.info("Wrote status snapshot to %s", status_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
