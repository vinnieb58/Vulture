"""Pelican monitor entrypoint for systemd and manual runs."""

from __future__ import annotations

import json
import logging
import sys

from pelican_monitor import __version__
from pelican_monitor.runner import run_monitor


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def main(argv: list[str] | None = None) -> int:
    setup_logging()
    logger = logging.getLogger("pelican_monitor")
    logger.info("Pelican monitor v%s starting", __version__)

    payload, exit_code = run_monitor()
    logger.info(
        "Pelican monitor complete: overall=%s host=%s backups=%d exit=%d",
        payload.get("overall_status"),
        payload.get("host"),
        len(payload.get("backups", {})),
        exit_code,
    )

    if "--json" in (argv or sys.argv[1:]):
        print(json.dumps(payload, indent=2, sort_keys=False))

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
