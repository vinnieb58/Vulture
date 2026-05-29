"""
Vulture-specific read-only health checks (no hunts, no DB writes).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from crow.checks.services import (
    ServiceStatus,
    check_scheduler_process,
    check_scheduler_tmux,
)
from crow.config import VULTURE_DB_PATH, VULTURE_LOGS_DIR, VULTURE_MAIN_LOG
from crow.formatting import format_timestamp


@dataclass
class VultureHealth:
    db_path: str
    db_exists: bool
    logs_dir_exists: bool
    main_log_path: str | None
    main_log_mtime: str | None
    scheduler_main: ServiceStatus
    scheduler_tmux: ServiceStatus


def _file_mtime_iso(path: Path) -> str | None:
    if not path.is_file():
        return None
    try:
        ts = path.stat().st_mtime
        return datetime.fromtimestamp(ts, tz=timezone.utc).strftime(
            "%Y-%m-%d %H:%M:%S UTC"
        )
    except OSError:
        return None


def _latest_log_mtime(logs_dir: Path) -> tuple[str | None, str | None]:
    """Return (path, mtime) for newest .log file in logs dir, if any."""
    if not logs_dir.is_dir():
        return None, None
    latest: Path | None = None
    latest_ts = -1.0
    try:
        for p in logs_dir.glob("*.log"):
            if not p.is_file():
                continue
            try:
                ts = p.stat().st_mtime
            except OSError:
                continue
            if ts > latest_ts:
                latest_ts = ts
                latest = p
    except OSError:
        return None, None

    if latest is None:
        return None, None
    return str(latest), _file_mtime_iso(latest)


def get_vulture_health(
    *,
    db_path: Path | None = None,
    logs_dir: Path | None = None,
) -> VultureHealth:
    db = db_path or VULTURE_DB_PATH
    logs = logs_dir or VULTURE_LOGS_DIR
    main_log = (logs_dir / "vulture.log") if logs_dir else VULTURE_MAIN_LOG

    mtime = _file_mtime_iso(main_log) if main_log.is_file() else None
    latest_path, latest_mtime = _latest_log_mtime(logs)
    if mtime is None and latest_mtime:
        mtime = latest_mtime
        main_log_display = latest_path
    else:
        main_log_display = str(main_log) if main_log else None

    return VultureHealth(
        db_path=str(db),
        db_exists=db.is_file(),
        logs_dir_exists=logs.is_dir(),
        main_log_path=main_log_display,
        main_log_mtime=mtime,
        scheduler_main=check_scheduler_process(),
        scheduler_tmux=check_scheduler_tmux(),
    )


def scheduler_summary(health: VultureHealth) -> str:
    """Single line scheduler visibility from process + tmux."""
    states = {health.scheduler_main.state, health.scheduler_tmux.state}
    if "running" in states:
        return "running"
    if states == {"not detected"}:
        return "not detected"
    if "unknown" in states:
        return "unknown"
    return "not detected"


def format_vulture_health_message(health: VultureHealth) -> str:
    from crow.formatting import join_lines

    sched = scheduler_summary(health)
    lines = [
        "**Vulture health** (read-only)",
        f"Database: `{'present' if health.db_exists else 'missing'}` @ `{health.db_path}`",
        f"Logs dir: `{'present' if health.logs_dir_exists else 'missing'}`",
    ]
    if health.main_log_mtime:
        lines.append(
            f"Latest log activity: {health.main_log_mtime}"
            + (f" (`{health.main_log_path}`)" if health.main_log_path else "")
        )
    else:
        lines.append("Latest log activity: none detected")
    lines.append(f"Scheduler (combined): **{sched}**")
    lines.append(
        f"  — main.py: {health.scheduler_main.state} | "
        f"tmux scheduler: {health.scheduler_tmux.state}"
    )
    lines.append(f"Checked: {format_timestamp()}")
    return join_lines(lines)
