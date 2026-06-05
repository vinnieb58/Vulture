"""
Crow configuration — paths and thresholds only (no secrets).
"""

from __future__ import annotations

import os
from pathlib import Path

# Subprocess safety
DEFAULT_SUBPROCESS_TIMEOUT = 10.0

# Disk usage thresholds (percent used)
DISK_WARN_PERCENT = 80
DISK_CRITICAL_PERCENT = 90

# Project root — cwd when the bot runs (Raven deploy uses repo root)
PROJECT_ROOT = Path(os.getenv("CROW_PROJECT_ROOT", ".")).resolve()

# Vulture paths (relative to project root unless overridden)
VULTURE_DB_PATH = Path(
    os.getenv("CROW_VULTURE_DB_PATH", str(PROJECT_ROOT / "data" / "vulture.db"))
)
VULTURE_LOGS_DIR = Path(
    os.getenv("CROW_VULTURE_LOGS_DIR", str(PROJECT_ROOT / "logs"))
)
VULTURE_MAIN_LOG = VULTURE_LOGS_DIR / "vulture.log"

# Extra mount points to check (comma-separated absolute paths in env)
_extra_mounts = os.getenv("CROW_EXTRA_DISK_PATHS", "").strip()
EXTRA_DISK_PATHS: list[Path] = [
    Path(p.strip()) for p in _extra_mounts.split(",") if p.strip()
]

# Standard mount scan roots
MOUNT_SCAN_ROOTS = (Path("/mnt"), Path("/media"))

# Display timezone for Crow timestamps (IANA name, e.g. America/Chicago)
DISPLAY_TIMEZONE = os.getenv("CROW_TIMEZONE", "America/Chicago").strip() or "America/Chicago"

# Raven production systemd units (override for non-default host layouts)
VULTURE_BOT_SYSTEMD_UNIT = os.getenv("CROW_VULTURE_BOT_UNIT", "vulture-bot").strip() or "vulture-bot"
VULTURE_SCHEDULER_SYSTEMD_UNIT = (
    os.getenv("CROW_VULTURE_SCHEDULER_UNIT", "vulture-scheduler").strip() or "vulture-scheduler"
)

# Raven health scripts (optional fallback; Crow prefers internal checks)
RAVEN_HEALTHCHECK_SCRIPT = Path(
    os.getenv("CROW_RAVEN_HEALTHCHECK_SCRIPT", str(Path.home() / "raven_healthcheck.sh"))
)
RAVEN_POST_REBOOT_SCRIPT = Path(
    os.getenv(
        "CROW_RAVEN_POST_REBOOT_SCRIPT",
        str(Path.home() / "raven_post_reboot_check.sh"),
    )
)

# Expected storage mounts: "Label:/path" comma-separated
_DEFAULT_EXPECTED_MOUNTS = (
    "Root SSD:/,"
    "MicroSD:/mnt/microsd,"
    "portable_beast:/mnt/portable_beast,"
    "toshiba_ext:/mnt/toshiba_ext"
)
_expected_mounts_raw = os.getenv("CROW_EXPECTED_MOUNTS", _DEFAULT_EXPECTED_MOUNTS).strip()


def _parse_expected_mounts(raw: str) -> list[tuple[str, str]]:
    mounts: list[tuple[str, str]] = []
    for part in raw.split(","):
        piece = part.strip()
        if not piece:
            continue
        if ":" in piece:
            label, path = piece.split(":", 1)
            mounts.append((label.strip(), path.strip()))
        else:
            mounts.append((piece, f"/mnt/{piece}"))
    return mounts or [("Root SSD", "/")]


EXPECTED_STORAGE_MOUNTS: list[tuple[str, str]] = _parse_expected_mounts(_expected_mounts_raw)

# Summarized listening ports for /check ports
KNOWN_SERVICE_PORTS: list[tuple[int, str]] = [
    (22, "SSH"),
    (445, "Samba"),
    (9443, "Portainer"),
    (8088, "Vulture Dashboard"),
]
