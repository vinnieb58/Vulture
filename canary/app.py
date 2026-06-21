"""
Canary v0.1 entrypoint — periodic read-only Raven health checks.
"""

from __future__ import annotations

import json
import logging
import sys
import time
from pathlib import Path

from canary import __version__
from canary.checks import run_all_checks
from canary.config import INTERVAL_SECONDS, LOG_PATH, LOGS_DIR, STATUS_PATH


def setup_logging() -> logging.Logger:
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("canary")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()

    formatter = logging.Formatter(
        fmt="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    file_handler = logging.FileHandler(LOG_PATH, encoding="utf-8")
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(formatter)
    logger.addHandler(stream_handler)

    return logger


def write_status(payload: dict) -> Path:
    STATUS_PATH.parent.mkdir(parents=True, exist_ok=True)
    temp_path = STATUS_PATH.with_suffix(".json.tmp")
    temp_path.write_text(json.dumps(payload, indent=2, sort_keys=False) + "\n", encoding="utf-8")
    temp_path.replace(STATUS_PATH)
    return STATUS_PATH


def run_once(logger: logging.Logger) -> dict:
    logger.info("Starting Canary check run (v%s)", __version__)
    payload = run_all_checks()
    path = write_status(payload)
    logger.info(
        "Check run complete: overall=%s host=%s wrote=%s warnings=%d critical=%d",
        payload.get("overall_status"),
        payload.get("host"),
        path,
        len(payload.get("warnings", [])),
        len(payload.get("critical", [])),
    )
    return payload


def main() -> None:
    logger = setup_logging()
    logger.info(
        "Canary v%s starting (interval=%ss, status=%s, log=%s)",
        __version__,
        INTERVAL_SECONDS,
        STATUS_PATH,
        LOG_PATH,
    )

    while True:
        started = time.monotonic()
        try:
            run_once(logger)
        except Exception:
            logger.exception("Unexpected error during check run")
        elapsed = time.monotonic() - started
        sleep_for = max(1.0, INTERVAL_SECONDS - elapsed)
        logger.info("Sleeping %.0fs until next run", sleep_for)
        time.sleep(sleep_for)


if __name__ == "__main__":
    main()
