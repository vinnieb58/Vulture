"""Long-term telemetry archive retention (indefinite) vs dashboard rolling windows."""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

log = logging.getLogger(__name__)

_archive_io_lock = threading.Lock()

DEFAULT_TELEMETRY_DIR = "data/telemetry"

# Dashboard rolling windows (performance; safe to prune after archiving).
DEFAULT_NEST_DASHBOARD_RETENTION_DAYS = 14
DEFAULT_TUYA_DASHBOARD_RETENTION_DAYS = 14
DEFAULT_RAVEN_METRICS_DASHBOARD_RETENTION_HOURS = 48

# Long-term archive policy: "indefinite" keeps all collected records; "disabled" skips archive writes.
DEFAULT_ARCHIVE_POLICY = "indefinite"

DEFAULT_NEST_ARCHIVE_PATH = f"{DEFAULT_TELEMETRY_DIR}/nest_history_archive.jsonl"
DEFAULT_TUYA_ARCHIVE_PATH = f"{DEFAULT_TELEMETRY_DIR}/tuya_power_history_archive.jsonl"
DEFAULT_RAVEN_METRICS_ARCHIVE_PATH = f"{DEFAULT_TELEMETRY_DIR}/raven_metrics_history_archive.jsonl"


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        log.warning("Invalid integer for %s=%r; using default %s", name, raw, default)
        return default


def nest_dashboard_retention_days() -> int:
    return _env_int("NEST_HISTORY_DASHBOARD_RETENTION_DAYS", DEFAULT_NEST_DASHBOARD_RETENTION_DAYS)


def tuya_dashboard_retention_days() -> int:
    return _env_int("TUYA_HISTORY_DASHBOARD_RETENTION_DAYS", DEFAULT_TUYA_DASHBOARD_RETENTION_DAYS)


def raven_metrics_dashboard_retention_hours() -> int:
    return _env_int(
        "DASHBOARD_METRICS_RETENTION_HOURS",
        DEFAULT_RAVEN_METRICS_DASHBOARD_RETENTION_HOURS,
    )


def _archive_policy(env_name: str) -> str:
    return (os.getenv(env_name, DEFAULT_ARCHIVE_POLICY) or DEFAULT_ARCHIVE_POLICY).strip().lower()


def nest_archive_policy() -> str:
    return _archive_policy("NEST_HISTORY_ARCHIVE_POLICY")


def tuya_archive_policy() -> str:
    return _archive_policy("TUYA_HISTORY_ARCHIVE_POLICY")


def raven_metrics_archive_policy() -> str:
    return _archive_policy("DASHBOARD_METRICS_ARCHIVE_POLICY")


def archive_policy_enabled(policy: str) -> bool:
    return policy not in ("disabled", "off", "0", "false", "no")


def nest_archive_path() -> Path:
    return Path(
        (os.getenv("NEST_HISTORY_ARCHIVE_PATH") or DEFAULT_NEST_ARCHIVE_PATH).strip()
    )


def tuya_archive_path() -> Path:
    return Path(
        (os.getenv("TUYA_HISTORY_ARCHIVE_PATH") or DEFAULT_TUYA_ARCHIVE_PATH).strip()
    )


def raven_metrics_archive_path() -> Path:
    return Path(
        (
            os.getenv("DASHBOARD_METRICS_ARCHIVE_PATH")
            or os.getenv("RAVEN_METRICS_ARCHIVE_PATH")
            or DEFAULT_RAVEN_METRICS_ARCHIVE_PATH
        ).strip()
    )


def long_term_archive_paths() -> tuple[Path, ...]:
    return (nest_archive_path(), tuya_archive_path(), raven_metrics_archive_path())


def parse_record_timestamp(raw_line: str) -> str | None:
    line = raw_line.strip()
    if not line:
        return None
    try:
        data = json.loads(line)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    ts = data.get("timestamp")
    return str(ts) if ts else None


def _read_nonempty_lines(path: Path) -> list[str]:
    if not path.is_file():
        return []
    try:
        return [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    except OSError:
        return []


def archive_contains_timestamp(archive_path: Path, record_timestamp: str) -> bool:
    target = record_timestamp.strip()
    if not target:
        return False
    return archive_last_timestamp(archive_path) == target


def archive_last_timestamp(archive_path: Path) -> str | None:
    lines = _read_nonempty_lines(archive_path)
    if not lines:
        return None
    return parse_record_timestamp(lines[-1])


def oldest_archive_timestamp(archive_path: Path) -> datetime | None:
    oldest: datetime | None = None
    for line in _read_nonempty_lines(archive_path):
        ts_raw = parse_record_timestamp(line)
        if not ts_raw:
            continue
        try:
            parsed = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
        except ValueError:
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        else:
            parsed = parsed.astimezone(timezone.utc)
        if oldest is None or parsed < oldest:
            oldest = parsed
    return oldest


def append_jsonl_archive(
    archive_path: Path,
    json_line: str,
    *,
    record_timestamp: str,
    policy: str | None = None,
) -> bool:
    """Append one JSONL row to the long-term archive (never pruned by default)."""
    effective_policy = (policy or DEFAULT_ARCHIVE_POLICY).strip().lower()
    if not archive_policy_enabled(effective_policy):
        return True

    line = json_line.strip()
    if not line:
        return False

    with _archive_io_lock:
        if archive_contains_timestamp(archive_path, record_timestamp):
            return True
        try:
            archive_path.parent.mkdir(parents=True, exist_ok=True)
            with archive_path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        except OSError as exc:
            log.warning("Could not append telemetry archive %s: %s", archive_path, exc)
            return False
    return True


def bootstrap_archive_from_dashboard(
    archive_path: Path,
    dashboard_path: Path,
    *,
    policy: str | None = None,
    line_extractor: Callable[[str], str | None] | None = None,
) -> int:
    """
    Copy existing dashboard JSONL rows into the archive when the archive is empty.

    Returns the number of lines copied.
    """
    effective_policy = (policy or DEFAULT_ARCHIVE_POLICY).strip().lower()
    if not archive_policy_enabled(effective_policy):
        return 0
    if archive_path.is_file() and archive_path.stat().st_size > 0:
        return 0
    if not dashboard_path.is_file():
        return 0

    extract = line_extractor or parse_record_timestamp
    copied = 0
    for line in _read_nonempty_lines(dashboard_path):
        ts = extract(line)
        if ts is None:
            continue
        if append_jsonl_archive(
            archive_path,
            line,
            record_timestamp=ts,
            policy=effective_policy,
        ):
            copied += 1
    return copied


def archive_record_count(archive_path: Path) -> int:
    return len(_read_nonempty_lines(archive_path))
