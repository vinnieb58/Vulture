"""
Canary configuration — intervals, paths, and thresholds only (no secrets).
"""

from __future__ import annotations

import os
from pathlib import Path

DEFAULT_SUBPROCESS_TIMEOUT = 10.0
DEFAULT_INTERVAL_SECONDS = 300

DISK_WARN_PERCENT = 80
DISK_CRITICAL_PERCENT = 90

PROJECT_ROOT = Path(os.getenv("CANARY_PROJECT_ROOT", ".")).resolve()
DATA_DIR = Path(os.getenv("CANARY_DATA_DIR", str(PROJECT_ROOT / "data")))
LOGS_DIR = Path(os.getenv("CANARY_LOGS_DIR", str(PROJECT_ROOT / "logs")))
STATUS_PATH = Path(os.getenv("CANARY_STATUS_PATH", str(DATA_DIR / "canary_status.json")))
LOG_PATH = Path(os.getenv("CANARY_LOG_PATH", str(LOGS_DIR / "canary.log")))

INTERVAL_SECONDS = int(os.getenv("CANARY_INTERVAL_SECONDS", str(DEFAULT_INTERVAL_SECONDS)))

# When running in Docker against the Raven host, mount host root at /host and set CANARY_HOST_ROOT=/host
HOST_ROOT = Path(os.getenv("CANARY_HOST_ROOT", "/")).resolve()

DISPLAY_TIMEZONE = os.getenv("CANARY_TIMEZONE", "America/Chicago").strip() or "America/Chicago"

VULTURE_BOT_UNIT = os.getenv("CANARY_VULTURE_BOT_UNIT", "vulture-bot").strip() or "vulture-bot"
VULTURE_SCHEDULER_UNIT = (
    os.getenv("CANARY_VULTURE_SCHEDULER_UNIT", "vulture-scheduler").strip() or "vulture-scheduler"
)

EXPECTED_STORAGE_MOUNTS: list[tuple[str, str]] = [
    ("root", "/"),
    ("microsd", "/mnt/storage/microsd"),
    ("portable_beast", "/mnt/storage/portable_beast"),
    ("toshiba_ext", "/mnt/storage/toshiba_ext"),
]

_overrides = os.getenv("CANARY_EXPECTED_MOUNTS", "").strip()
if _overrides:
    EXPECTED_STORAGE_MOUNTS = []
    for part in _overrides.split(","):
        piece = part.strip()
        if not piece:
            continue
        if ":" in piece:
            label, path = piece.split(":", 1)
            EXPECTED_STORAGE_MOUNTS.append((label.strip(), path.strip()))
        else:
            EXPECTED_STORAGE_MOUNTS.append((piece, piece))
