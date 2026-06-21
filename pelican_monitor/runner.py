"""
Pelican monitor runner — checks all enabled backup definitions and writes aggregate status.
"""

from __future__ import annotations

import json
import logging
import os
import socket
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from pelican_monitor import config
from canary.alerting import process_backup_alerts
from pelican_monitor.definitions import BackupDefinition, enabled_backup_definitions, registered_backup_definitions
from pelican_monitor.results import BackupCheckResult, checker_error_result, combine_status

logger = logging.getLogger("pelican_monitor")


def get_hostname() -> str:
    try:
        return socket.gethostname()
    except OSError:
        return os.uname().nodename


def _checked_at() -> str:
    tz = ZoneInfo(config.DISPLAY_TIMEZONE)
    return datetime.now(tz=timezone.utc).astimezone(tz).isoformat(timespec="seconds")


def run_backup_check(defn: BackupDefinition) -> BackupCheckResult:
    checked_at = _checked_at()
    try:
        return defn.checker()
    except Exception as exc:  # noqa: BLE001 — isolate checker failures
        logger.exception("Checker failed for %s", defn.backup_id)
        return checker_error_result(
            backup_id=defn.backup_id,
            display_name=defn.display_name,
            checked_at=checked_at,
            exc=exc,
            warn_threshold_hours=defn.warn_threshold_hours,
            critical_threshold_hours=defn.critical_threshold_hours,
        )


def write_status(payload: dict[str, Any], path: Path | None = None) -> Path:
    target = path or config.STATUS_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_suffix(".json.tmp")
    temp.write_text(json.dumps(payload, indent=2, sort_keys=False) + "\n", encoding="utf-8")
    temp.replace(target)
    return target


def run_monitor(
    *,
    definitions: list[BackupDefinition] | None = None,
    host: str | None = None,
    send_alerts: bool = True,
) -> tuple[dict[str, Any], int]:
    """
    Check all enabled backups, optionally send alerts, write aggregate status.

    Returns (payload, exit_code). Exit code is nonzero when any enabled backup is critical/error.
    """
    enabled = definitions if definitions is not None else enabled_backup_definitions()
    hostname = host or get_hostname()
    generated_at = _checked_at()

    results: dict[str, BackupCheckResult] = {}
    for defn in enabled:
        logger.info("Checking backup %s (%s)", defn.backup_id, defn.display_name)
        results[defn.backup_id] = run_backup_check(defn)

    backup_payload = {backup_id: result.to_dict() for backup_id, result in results.items()}
    statuses = [result.status for result in results.values()]
    overall = combine_status(*statuses) if statuses else "ok"

    alert_outcomes: list[dict[str, Any]] = []
    if send_alerts and backup_payload:
        alert_outcomes = process_backup_alerts(
            backup_payload,
            host=hostname,
            state_path=config.ALERT_STATE_PATH,
            webhook_url=config.DISCORD_WEBHOOK_URL,
        )

    registered = registered_backup_definitions()
    payload: dict[str, Any] = {
        "generated_at": generated_at,
        "host": hostname,
        "overall_status": overall,
        "enabled_backups": [d.backup_id for d in enabled],
        "registered_backups": [
            {
                "backup_id": d.backup_id,
                "display_name": d.display_name,
                "enabled": d.backup_id in {e.backup_id for e in enabled},
                "target_path": d.target_path,
            }
            for d in registered
        ],
        "backups": backup_payload,
        "alerts": alert_outcomes,
    }

    write_status(payload)
    exit_code = 1 if any(s in ("critical", "error") for s in statuses) else 0
    return payload, exit_code
