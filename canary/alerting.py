"""
Shared Discord alerting with persisted deduplication for backup monitor results.

Used by pelican-monitor.service; not invoked during Canary's 5-minute infrastructure loop.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger("canary")


@dataclass(frozen=True)
class BackupAlertDecision:
    should_send: bool
    kind: str  # alert | recovery | none
    severity: str
    fingerprint: str
    message: str


def _load_state(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".json.tmp")
    temp.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temp.replace(path)


def _severity_rank(severity: str) -> int:
    if severity == "critical":
        return 2
    if severity == "warning":
        return 1
    return 0


def _effective_severity(result: dict[str, Any]) -> str:
    status = result.get("status", "ok")
    if status == "ok":
        return "healthy"
    if status in ("critical", "error"):
        return "critical"
    if status == "warning":
        return "warning"
    return status


def _fingerprint(result: dict[str, Any]) -> str:
    codes = result.get("issue_codes") or []
    if not codes:
        return "healthy"
    return "|".join(codes)


def decide_backup_alert(
    result: dict[str, Any],
    previous_state: dict[str, Any],
) -> BackupAlertDecision:
    severity = _effective_severity(result)
    fingerprint = _fingerprint(result)
    prev_severity = previous_state.get("severity", "healthy")
    prev_fingerprint = previous_state.get("fingerprint", "healthy")
    display_name = result.get("display_name", "Backup")

    if severity == "healthy":
        if prev_severity in ("warning", "critical"):
            return BackupAlertDecision(
                should_send=True,
                kind="recovery",
                severity="healthy",
                fingerprint="healthy",
                message=_build_recovery_message(display_name, result),
            )
        return BackupAlertDecision(
            should_send=False,
            kind="none",
            severity="healthy",
            fingerprint="healthy",
            message="",
        )

    if prev_severity == "healthy":
        return BackupAlertDecision(
            should_send=True,
            kind="alert",
            severity=severity,
            fingerprint=fingerprint,
            message=_build_alert_message(display_name, result, severity),
        )

    if _severity_rank(severity) > _severity_rank(prev_severity):
        return BackupAlertDecision(
            should_send=True,
            kind="alert",
            severity=severity,
            fingerprint=fingerprint,
            message=_build_alert_message(display_name, result, severity),
        )

    if fingerprint != prev_fingerprint:
        return BackupAlertDecision(
            should_send=True,
            kind="alert",
            severity=severity,
            fingerprint=fingerprint,
            message=_build_alert_message(display_name, result, severity),
        )

    return BackupAlertDecision(
        should_send=False,
        kind="none",
        severity=severity,
        fingerprint=fingerprint,
        message="",
    )


def _format_timer_line(timer: dict[str, Any]) -> str | None:
    if not timer:
        return None
    unit = timer.get("unit", "timer")
    enabled = timer.get("enabled", "unknown")
    active = timer.get("active", "unknown")
    next_run = timer.get("next_run") or "none"
    return f"timer {unit}: enabled={enabled}, active={active}, next={next_run}"


def _format_service_line(service: dict[str, Any]) -> str | None:
    if not service:
        return None
    unit = service.get("unit", "service")
    active = service.get("active", "unknown")
    result = service.get("result", "unknown")
    exec_status = service.get("exec_main_status")
    exec_part = f", exit={exec_status}" if exec_status is not None else ""
    return f"service {unit}: active={active}, result={result}{exec_part}"


def _format_archive_line(result: dict[str, Any]) -> str | None:
    archive = (result.get("details") or {}).get("archive") or {}
    name = archive.get("latest_name")
    age = result.get("backup_age_hours")
    if not name and age is None:
        return "latest backup: none"
    age_part = f", age={age:.1f}h" if isinstance(age, (int, float)) else ""
    if name:
        return f"latest backup: {name}{age_part}"
    return f"latest backup age={age:.1f}h" if isinstance(age, (int, float)) else None


def _build_alert_message(display_name: str, result: dict[str, Any], severity: str) -> str:
    lines = [
        f"**Raven / {display_name} {severity.upper()}**",
        result.get("reason") or "Backup unhealthy",
    ]
    for extra in (
        _format_timer_line(result.get("timer") or {}),
        _format_service_line(result.get("service") or {}),
        _format_archive_line(result),
    ):
        if extra:
            lines.append(extra)
    return "\n".join(lines)


def _build_recovery_message(display_name: str, result: dict[str, Any]) -> str:
    lines = [
        f"**Raven / {display_name} RECOVERED**",
        f"{display_name} monitoring returned to healthy.",
    ]
    for extra in (
        _format_timer_line(result.get("timer") or {}),
        _format_service_line(result.get("service") or {}),
        _format_archive_line(result),
    ):
        if extra:
            lines.append(extra)
    return "\n".join(lines)


def send_discord_message(webhook_url: str, content: str) -> bool:
    payload = json.dumps({"content": content}).encode("utf-8")
    request = urllib.request.Request(
        webhook_url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return 200 <= response.status < 300
    except urllib.error.HTTPError as exc:
        logger.warning("Discord webhook HTTP error: %s", exc)
        return False
    except urllib.error.URLError as exc:
        logger.warning("Discord webhook URL error: %s", exc)
        return False
    except OSError as exc:
        logger.warning("Discord webhook failed: %s", exc)
        return False


def process_backup_alerts(
    backup_results: dict[str, dict[str, Any]],
    *,
    host: str,
    state_path: Path,
    webhook_url: str,
) -> list[dict[str, Any]]:
    """Evaluate and send state-change alerts for each backup result."""
    url = webhook_url.strip()
    state = _load_state(path=state_path)
    backups_state = state.get("backups", {})
    if not isinstance(backups_state, dict):
        backups_state = {}

    outcomes: list[dict[str, Any]] = []

    for backup_id, result in backup_results.items():
        previous = backups_state.get(backup_id, {})
        if not isinstance(previous, dict):
            previous = {}

        decision = decide_backup_alert(result, previous)
        sent = False

        if decision.should_send and url:
            content = decision.message
            if host:
                content = f"{content}\nhost: {host}"
            sent = send_discord_message(url, content)
            if sent:
                logger.info(
                    "Discord %s sent for %s (severity=%s fingerprint=%s)",
                    decision.kind,
                    backup_id,
                    decision.severity,
                    decision.fingerprint,
                )
            else:
                logger.warning(
                    "Discord %s failed for %s (severity=%s fingerprint=%s)",
                    decision.kind,
                    backup_id,
                    decision.severity,
                    decision.fingerprint,
                )
        elif decision.should_send and not url:
            logger.info(
                "Alert suppressed for %s (no webhook): kind=%s severity=%s",
                backup_id,
                decision.kind,
                decision.severity,
            )

        backups_state[backup_id] = {
            "severity": _effective_severity(result),
            "fingerprint": _fingerprint(result),
            "last_kind": decision.kind if decision.should_send else previous.get("last_kind"),
            "last_sent_fingerprint": (
                decision.fingerprint if sent else previous.get("last_sent_fingerprint")
            ),
        }
        outcomes.append(
            {
                "backup_id": backup_id,
                "decision": decision.kind,
                "sent": sent,
                "severity": backups_state[backup_id]["severity"],
                "fingerprint": backups_state[backup_id]["fingerprint"],
            }
        )

    state["backups"] = backups_state
    _save_state(state_path, state)
    return outcomes
