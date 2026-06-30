"""
Kestrel Tuya dual-meter power probe
===================================
Read-only local (TinyTuya) poller for V-WIFI-DL02-ES dual-channel energy meters.

Does NOT implement device control, timers, dashboard UI, or alerts.

Usage:
    python experiments/kestrel/tuya_power_probe.py --discover
    python experiments/kestrel/tuya_power_probe.py --once
    python experiments/kestrel/tuya_power_probe.py --once --debug-dps
    python experiments/kestrel/tuya_power_probe.py --sample --interval-seconds 60 --count 10
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from kestrel.config import setup_logging  # noqa: E402
from kestrel.tuya_power import (  # noqa: E402
    TuyaPowerApiError,
    TuyaPowerConfigError,
    TuyaPowerConfig,
    format_compact_appliance_summary,
    format_debug_dps_summary,
    format_raw_dps_lines,
    load_tuya_power_config,
    poll_tuya_power_meters,
    read_meter_with_fallback,
    redact_tuya_message,
    sanitize_tuya_payload,
    scan_local_devices,
)
from kestrel.tuya_power_error import (  # noqa: E402
    clear_tuya_error,
    record_tuya_poll_error,
    tuya_error_path_for,
)
from kestrel.tuya_power_history import append_history_from_snapshot  # noqa: E402

DEFAULT_SAMPLE_INTERVAL_SECONDS = 60
DEFAULT_SAMPLE_COUNT = 10


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Kestrel Tuya power probe (read-only)")
    parser.add_argument(
        "--discover",
        action="store_true",
        help="Scan the LAN and print raw DPS/status for configured meters (no snapshot write)",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Poll configured meters once and write data/kestrel_tuya_power_status.json",
    )
    parser.add_argument(
        "--sample",
        action="store_true",
        help="Manual sampler: poll repeatedly (--interval-seconds, --count) using the --once path",
    )
    parser.add_argument(
        "--interval-seconds",
        type=int,
        default=DEFAULT_SAMPLE_INTERVAL_SECONDS,
        help=f"Seconds between --sample polls (default: {DEFAULT_SAMPLE_INTERVAL_SECONDS})",
    )
    parser.add_argument(
        "--count",
        type=int,
        default=DEFAULT_SAMPLE_COUNT,
        help=f"Number of --sample polls to run (default: {DEFAULT_SAMPLE_COUNT})",
    )
    parser.add_argument(
        "--debug-dps",
        action="store_true",
        help="Print sanitized appliance summary after --once or each --sample",
    )
    return parser.parse_args(argv)


def _print_scan_results(devices: dict) -> None:
    if not devices:
        print("TinyTuya scan: no devices found on the local network.")
        return
    print(f"TinyTuya scan: {len(devices)} device(s) found.")
    for device_id, info in sorted(devices.items()):
        if not isinstance(info, dict):
            print(f"  device_id=[REDACTED] info={type(info).__name__}")
            continue
        ip = info.get("ip") or info.get("address") or "—"
        version = info.get("version") or "—"
        product = info.get("product") or info.get("product_name") or "—"
        print(
            f"  device_id_suffix={str(device_id)[-4:]} ip={ip} version={version} product={product}"
        )


def execute_poll_once(
    config: TuyaPowerConfig,
    *,
    log: logging.Logger,
) -> tuple[int, dict | None]:
    """Poll meters and write snapshot/history on success (same path as --once)."""
    output_path = Path(config.output_path)
    error_path = tuya_error_path_for(output_path)

    try:
        snapshot = poll_tuya_power_meters(config)
    except TuyaPowerApiError as exc:
        message = redact_tuya_message(str(exc)) or "Tuya power poll failed"
        record_tuya_poll_error(status_path=output_path, message=str(exc), error_path=error_path)
        log.error("Tuya poll failed: %s", message)
        print(f"ERROR: {message}", file=sys.stderr)
        print(f"Last good snapshot preserved: {output_path}")
        return 1, None

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(snapshot, indent=2) + "\n", encoding="utf-8")
    clear_tuya_error(error_path)
    log.info("Wrote Tuya power snapshot (%s meter(s))", len(snapshot.get("meters", {})))

    if not append_history_from_snapshot(snapshot):
        log.warning("Tuya history append failed; latest snapshot was still written")

    return 0, snapshot


def _print_once_summary(snapshot: dict, *, output_path: Path, debug_dps: bool) -> None:
    appliances = snapshot.get("appliances") or {}
    if isinstance(appliances, dict) and appliances:
        labels = [
            str(entry.get("label"))
            for entry in appliances.values()
            if isinstance(entry, dict) and entry.get("label")
        ]
        print(f"Tuya appliances: {', '.join(labels)}")
    else:
        print("Tuya appliances: none found")

    flags: list[str] = []
    if snapshot.get("limited"):
        flags.append("limited")
    if snapshot.get("stale"):
        flags.append("stale")
    if flags:
        print(f"Snapshot flags: {', '.join(flags)}")

    print(f"Snapshot: {output_path} (source={snapshot.get('source')})")

    if debug_dps:
        print("Appliance summary:")
        for line in format_debug_dps_summary(snapshot):
            print(line)


def run_discover() -> int:
    log = setup_logging("INFO")

    try:
        scan = scan_local_devices()
    except TuyaPowerApiError as exc:
        message = redact_tuya_message(str(exc)) or "TinyTuya scan failed"
        log.error("Tuya scan failed: %s", message)
        print(f"ERROR: {message}", file=sys.stderr)
        return 1

    _print_scan_results(scan)

    try:
        config = load_tuya_power_config()
    except TuyaPowerConfigError as exc:
        print(f"NOTE: {exc}", file=sys.stderr)
        print(
            "Scan complete. Run `python -m tinytuya wizard` (writes devices.json) "
            "or set .env overrides."
        )
        return 0

    print("Configured meter raw status/DPS:")
    for meter in config.meters:
        try:
            payload, source = read_meter_with_fallback(config, meter)
        except TuyaPowerApiError as exc:
            message = redact_tuya_message(str(exc)) or f"Read failed for {meter.meter_key}"
            print(f"  meter={meter.meter_key} ERROR: {message}", file=sys.stderr)
            continue

        raw_dps = payload.get("raw_dps") or {}
        print(f"  meter={meter.meter_key} source={source} dps_count={len(raw_dps)}")
        for line in format_raw_dps_lines(
            meter_key=meter.meter_key,
            raw_dps=raw_dps,
            source=source,
        ):
            print(f"    {line}")

        raw_status = payload.get("raw_status")
        if isinstance(raw_status, dict):
            print(
                f"    raw_status={json.dumps(sanitize_tuya_payload(raw_status), sort_keys=True)}"
            )

    return 0


def run_once(*, debug_dps: bool) -> int:
    log = setup_logging("INFO")

    try:
        config = load_tuya_power_config()
    except TuyaPowerConfigError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    exit_code, snapshot = execute_poll_once(config, log=log)
    if exit_code != 0 or snapshot is None:
        return exit_code

    _print_once_summary(snapshot, output_path=Path(config.output_path), debug_dps=debug_dps)
    return 0


def run_sample(*, interval_seconds: int, count: int, debug_dps: bool) -> int:
    log = setup_logging("INFO")

    if interval_seconds < 1:
        print("ERROR: --interval-seconds must be at least 1.", file=sys.stderr)
        return 1
    if count < 1:
        print("ERROR: --count must be at least 1.", file=sys.stderr)
        return 1

    try:
        config = load_tuya_power_config()
    except TuyaPowerConfigError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    output_path = Path(config.output_path)
    print(
        f"Manual sampler: {count} sample(s), interval={interval_seconds}s, output={output_path}"
    )

    for sample_index in range(1, count + 1):
        exit_code, snapshot = execute_poll_once(config, log=log)
        if exit_code != 0 or snapshot is None:
            return exit_code

        print(
            format_compact_appliance_summary(
                snapshot,
                sample_index=sample_index,
                sample_count=count,
            )
        )

        if debug_dps:
            for line in format_debug_dps_summary(snapshot):
                print(f"  {line}")

        if sample_index < count:
            time.sleep(interval_seconds)

    print(f"Sampler complete: {count} sample(s) written to {output_path}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.discover:
        return run_discover()
    if args.once:
        return run_once(debug_dps=args.debug_dps)
    if args.sample:
        return run_sample(
            interval_seconds=args.interval_seconds,
            count=args.count,
            debug_dps=args.debug_dps,
        )

    print("ERROR: Specify --discover, --once, or --sample.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
