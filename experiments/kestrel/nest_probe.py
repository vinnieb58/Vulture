"""
Kestrel Nest SDM probe
======================
Read-only Google Smart Device Management poller for Nest thermostats.

Does NOT implement thermostat control commands.

Usage:
    python experiments/kestrel/nest_probe.py --once
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from kestrel.config import setup_logging  # noqa: E402
from kestrel.nest import (  # noqa: E402
    NestApiError,
    NestConfigError,
    format_debug_trait_summary,
    load_nest_config,
    poll_nest_thermostats,
    redact_nest_message,
)
from kestrel.nest_error import clear_nest_error, nest_error_path_for, record_nest_poll_error  # noqa: E402
from kestrel.nest_history import append_history_from_snapshot  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Kestrel Nest SDM probe (read-only)")
    parser.add_argument(
        "--once",
        action="store_true",
        help="Poll SDM once and write data/kestrel_nest_status.json",
    )
    parser.add_argument(
        "--debug-traits",
        action="store_true",
        help="Print sanitized raw SDM trait summary per thermostat (requires --once)",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not args.once:
        print("ERROR: Specify --once to poll Nest thermostats.", file=sys.stderr)
        return 1

    log = setup_logging("INFO")

    try:
        config = load_nest_config()
    except NestConfigError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    output_path = Path(config.output_path)
    error_path = nest_error_path_for(output_path)

    try:
        snapshot = poll_nest_thermostats(config)
    except NestApiError as exc:
        message = redact_nest_message(str(exc)) or "Nest SDM poll failed"
        record_nest_poll_error(status_path=output_path, message=str(exc), error_path=error_path)
        log.error("Nest poll failed: %s", message)
        print(f"ERROR: {message}", file=sys.stderr)
        return 1

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(snapshot, indent=2) + "\n", encoding="utf-8")
    clear_nest_error(error_path)
    log.info("Wrote Nest status snapshot (%s thermostat(s))", len(snapshot.get("thermostats", {})))

    if not append_history_from_snapshot(snapshot):
        log.warning("Nest history append failed; latest snapshot was still written")

    names = [
        entry.get("name")
        for entry in snapshot.get("thermostats", {}).values()
        if isinstance(entry, dict)
    ]
    if names:
        print(f"Nest thermostats: {', '.join(str(name) for name in names)}")
    else:
        print("Nest thermostats: none found")
    print(f"Snapshot: {output_path}")

    if args.debug_traits:
        print("Raw SDM trait summary:")
        for line in format_debug_trait_summary(snapshot):
            print(line)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
