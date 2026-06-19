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
    load_nest_config,
    poll_nest_thermostats,
    redact_nest_message,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Kestrel Nest SDM probe (read-only)")
    parser.add_argument(
        "--once",
        action="store_true",
        help="Poll SDM once and write data/kestrel_nest_status.json",
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

    try:
        snapshot = poll_nest_thermostats(config)
    except NestApiError as exc:
        message = redact_nest_message(str(exc)) or "Nest SDM poll failed"
        log.error("Nest poll failed: %s", message)
        print(f"ERROR: {message}", file=sys.stderr)
        return 1

    output_path = Path(config.output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(snapshot, indent=2) + "\n", encoding="utf-8")
    log.info("Wrote Nest status snapshot (%s thermostat(s))", len(snapshot.get("thermostats", {})))

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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
