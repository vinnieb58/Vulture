"""
Canary Discord alerting with persisted deduplication for Pelican backup checks.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from canary import config

logger = logging.getLogger("canary")


@dataclass(frozen=True)
class PelicanAlertDecision:
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


def _fingerprint_from_check(pelican_check: dict[str, Any]) -> str:
    codes = pelican_check.get("issue_codes") or []
    if not codes:
        return "healthy"
    return "|".join(codes)


def _effective_severity(pelican_check: dict[str, Any]) -> str:
    status = pelican_check.get("status", "ok")
    if status == "ok":
        return "healthy"
    return status


def decide_pelican_alert(
    pelican_check: dict[str, Any],
    previous_state: dict[str, Any],
) -> PelicanAlertDecision:
    severity = _effective_severity(pelican_check)
    fingerprint = _fingerprint_from_check(pelican_check)
    prev_severity = previous_state.get("severity", "healthy")
    prev_fingerprint = previous_state.get("fingerprint", "healthy")

    if severity == "healthy":
        if prev_severity in ("warning", "critical"):
            return PelicanAlertDecision(
                should_send=True,
                kind="recovery",
                severity="healthy",
                fingerprint="healthy",
                message=_build_recovery_message(pelican_check),
            )
        return PelicanAlertDecision(
            should_send=False,
            kind="none",
            severity="healthy",
            fingerprint="healthy",
            message="",
        )

    # Unhealthy paths
    if prev_severity == "healthy":
        return PelicanAlertDecision(
            should_send=True,
            kind="alert",
            severity=severity,
            fingerprint=fingerprint,
            message=_build_alert_message(pelican_check, severity),
        )

    if _severity_rank(severity) > _severity_rank(prev_severity):
        return PelicanAlertDecision(
            should_send=True,
            kind="alert",
            severity=severity,
            fingerprint=fingerprint,
            message=_build_alert_message(pelican_check, severity),
        )

    if fingerprint != prev_fingerprint:
        return PelicanAlertDecision(
            should_send=True,
            kind="alert",
            severity=severity,
            fingerprint=fingerprint,
            message=_build_alert_message(pelican_check, severity),
        )

    return PelicanAlertDecision(
        should_send=False,
        kind="none",
        severity=severity,
        fingerprint=fingerprint,
        message="",
    )


def _format_timer_line(timer: dict[str, Any]) -> str:
    enabled = timer.get("enabled", "unknown")
    active = timer.get("active", "unknown")
    next_run = timer.get("next_run") or "none"
    return f"timer {timer.get('unit', config.PELICAN_TIMER_UNIT)}: enabled={enabled}, active={active}, next={next_run}"


def _format_service_line(service: dict[str, Any]) -> str:
    active = service.get("active", "unknown")
    result = service.get("result", "unknown")
    exec_status = service.get("exec_main_status")
    exec_part = f", exit={exec_status}" if exec_status is not None else ""
    return f"service {service.get('unit', config.PELICAN_SERVICE_UNIT)}: active={active}, result={result}{exec_part}"


def _format_archive_line(archive: dict[str, Any]) -> str:
    name = archive.get("latest_name")
    age = archive.get("age_hours")
    if not name:
        return "latest backup: none"
    age_part = f", age={age:.1f}h" if isinstance(age, (int, float)) else ""
    return f"latest backup: {name}{age_part}"


def _primary_reason(pelican_check: dict[str, Any]) -> str:
    alerts = pelican_check.get("alerts") or []
    if not alerts:
        return "Pelican backup healthy"
    # Prefer critical messages first.
    ordered = sorted(alerts, key=lambda a: _severity_rank(a.get("severity", "warning")), reverse=True)
    return ordered[0].get("message", "Pelican backup unhealthy")


def _build_alert_message(pelican_check: dict[str, Any], severity: str) -> str:
    timer = pelican_check.get("timer") or {}
    service = pelican_check.get("service") or {}
    archive = pelican_check.get("archive") or {}
    reason = _primary_reason(pelican_check)
    lines = [
        f"**Raven / Pelican backup {severity.upper()}**",
        reason,
        _format_timer_line(timer),
        _format_service_line(service),
        _format_archive_line(archive),
    ]
    return "\n".join(lines)


def _build_recovery_message(pelican_check: dict[str, Any]) -> str:
    timer = pelican_check.get("timer") or {}
    service = pelican_check.get("service") or {}
    archive = pelican_check.get("archive") or {}
    lines = [
        "**Raven / Pelican backup RECOVERED**",
        "Pelican backup monitoring returned to healthy.",
        _format_timer_line(timer),
        _format_service_line(service),
        _format_archive_line(archive),
    ]
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


def process_pelican_alerts(
    pelican_check: dict[str, Any],
    *,
    host: str,
    state_path: Path | None = None,
    webhook_url: str | None = None,
) -> dict[str, Any]:
    """
    Evaluate Pelican alert transitions, optionally send Discord notification,
    and persist dedup state.
    """
    path = state_path or config.ALERT_STATE_PATH
    url = (webhook_url if webhook_url is not None else config.DISCORD_WEBHOOK_URL).strip()
    state = _load_state(path)
    pelican_state = state.get("pelican_backup", {})
    if not isinstance(pelican_state, dict):
        pelican_state = {}

    decision = decide_pelican_alert(pelican_check, pelican_state)
    sent = False

    if decision.should_send and url:
        content = decision.message
        if host:
            content = f"{content}\nhost: {host}"
        sent = send_discord_message(url, content)
        if sent:
            logger.info(
                "Pelican Discord %s sent (severity=%s fingerprint=%s)",
                decision.kind,
                decision.severity,
                decision.fingerprint,
            )
        else:
            logger.warning(
                "Pelican Discord %s failed to send (severity=%s fingerprint=%s)",
                decision.kind,
                decision.severity,
                decision.fingerprint,
            )
    elif decision.should_send and not url:
        logger.info(
            "Pelican alert suppressed (no webhook): kind=%s severity=%s fingerprint=%s",
            decision.kind,
            decision.severity,
            decision.fingerprint,
        )

    current_severity = _effective_severity(pelican_check)
    current_fingerprint = _fingerprint_from_check(pelican_check)
    new_pelican_state = {
        "severity": current_severity,
        "fingerprint": current_fingerprint,
        "last_kind": decision.kind if decision.should_send else pelican_state.get("last_kind"),
        "last_sent_fingerprint": (
            decision.fingerprint if sent else pelican_state.get("last_sent_fingerprint")
        ),
    }
    state["pelican_backup"] = new_pelican_state
    _save_state(path, state)

    return {
        "decision": decision.kind,
        "sent": sent,
        "severity": new_pelican_state["severity"],
        "fingerprint": new_pelican_state["fingerprint"],
    }
