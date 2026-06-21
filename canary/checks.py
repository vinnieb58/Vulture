"""
Read-only Raven health checks for Canary.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from canary import config
from canary.parsers import (
    combine_status,
    parse_docker_ps_lines,
    parse_lan_ipv4_from_ip_br,
    parse_systemctl_failed,
    parse_tmux_sessions,
)
from canary.pelican_backup import check_pelican_backup
from canary.storage import check_raven_storage, host_path
from canary.subprocess_util import is_timeout, run_command


def check_internet() -> dict[str, Any]:
    result: dict[str, Any] = {"status": "ok", "ping_1_1_1_1": {}, "dns_google": {}}

    ok, out = run_command(["ping", "-c", "1", "-W", "3", "1.1.1.1"], timeout=config.TIMEOUT_PING)
    result["ping_1_1_1_1"] = {
        "ok": ok,
        "detail": "timed out" if is_timeout(out) else (out if not ok else "reachable"),
    }
    if not ok:
        result["status"] = "critical" if is_timeout(out) else "warning"

    ok_dns, out_dns = run_command(
        ["ping", "-c", "1", "-W", "5", "google.com"],
        timeout=config.TIMEOUT_PING + 2,
    )
    result["dns_google"] = {
        "ok": ok_dns,
        "detail": "timed out" if is_timeout(out_dns) else (out_dns if not ok_dns else "reachable"),
        "optional": True,
    }
    if not ok_dns and result["status"] == "ok":
        result["status"] = "warning"

    return result


def check_network() -> dict[str, Any]:
    result: dict[str, Any] = {
        "status": "ok",
        "lan_ipv4": None,
        "tailscale_ipv4": None,
        "detail": {},
    }

    ok, out = run_command(["ip", "-br", "addr"], timeout=config.DEFAULT_SUBPROCESS_TIMEOUT)
    if ok:
        result["lan_ipv4"] = parse_lan_ipv4_from_ip_br(out)
        result["detail"]["ip_br_addr"] = out.splitlines()[:8]
    else:
        result["detail"]["ip_br_addr_error"] = out
        result["status"] = "warning"

    ok_ts, out_ts = run_command(["tailscale", "ip", "-4"], timeout=config.TIMEOUT_PING)
    if ok_ts and out_ts.strip():
        result["tailscale_ipv4"] = out_ts.splitlines()[0].strip()
    else:
        result["detail"]["tailscale_error"] = out_ts or "unavailable"
        if result["status"] == "ok":
            result["status"] = "warning"

    return result


def _normalize_systemctl_value(raw: str, *, ok: bool) -> str:
    text = (raw or "").strip().lower()
    if ok and text:
        return text.splitlines()[0].strip()
    lowered = text
    if "not found" in lowered or "could not be found" in lowered:
        return "not-found"
    if is_timeout(raw) or "systemd" in lowered or "bus" in lowered:
        return "unavailable"
    if text:
        return text.splitlines()[0][:80]
    return "unknown"


def _systemctl_unit_state(unit: str) -> dict[str, str]:
    active_ok, active_out = run_command(
        ["systemctl", "is-active", unit],
        timeout=config.TIMEOUT_SYSTEMCTL,
    )
    enabled_ok, enabled_out = run_command(
        ["systemctl", "is-enabled", unit],
        timeout=config.TIMEOUT_SYSTEMCTL,
    )

    active = _normalize_systemctl_value(active_out, ok=active_ok)
    enabled = _normalize_systemctl_value(enabled_out, ok=enabled_ok)

    return {
        "unit": unit,
        "active": active,
        "enabled": enabled,
        "active_bool": active == "active",
        "enabled_bool": enabled in ("enabled", "static", "masked"),
    }


def _resolve_ssh_unit() -> str:
    for candidate in ("ssh.service", "ssh.socket", "sshd.service"):
        state = _systemctl_unit_state(candidate)
        if state["active"] == "active" or state["enabled"] in ("enabled", "static"):
            return candidate
    return "ssh.service"


def _service_entry(
    label: str,
    unit: str,
    *,
    kind: str = "systemd",
    optional: bool = False,
) -> dict[str, Any]:
    state = _systemctl_unit_state(unit)
    if state["active"] == "not-found" or state["enabled"] == "not-found":
        status = "not_configured" if optional else "warning"
    elif state["active_bool"]:
        status = "ok"
    elif state["active"] in ("failed", "inactive", "dead"):
        status = "warning" if optional else "critical"
    elif state["active"] == "unavailable":
        status = "warning"
    else:
        status = "warning"

    return {
        "label": label,
        "kind": kind,
        "unit": unit,
        "active": state["active"],
        "enabled": state["enabled"],
        "status": status,
    }


def _dashboard_container_entry(containers: list[dict[str, str]]) -> dict[str, Any]:
    name = config.DASHBOARD_CONTAINER
    match = next((c for c in containers if c["name"] == name), None)
    if match is None:
        return {
            "label": "dashboard",
            "kind": "docker_container",
            "unit": name,
            "active": "not-found",
            "enabled": "n/a",
            "status": "not_configured",
            "container_status": None,
        }
    running = match["status"].lower().startswith("up")
    return {
        "label": "dashboard",
        "kind": "docker_container",
        "unit": name,
        "active": "active" if running else "inactive",
        "enabled": "n/a",
        "status": "ok" if running else "warning",
        "container_status": match["status"],
        "ports": match.get("ports", ""),
    }


def check_services() -> dict[str, Any]:
    ssh_unit = _resolve_ssh_unit()
    entries = [
        _service_entry("ssh", ssh_unit),
        _service_entry("tailscaled", "tailscaled"),
        _service_entry("smbd", "smbd"),
        _service_entry("docker", "docker"),
        _service_entry("vulture_bot", config.VULTURE_BOT_UNIT, optional=True),
        _service_entry("vulture_scheduler_timer", config.VULTURE_SCHEDULER_TIMER, optional=True),
    ]

    ok, out = run_command(
        ["docker", "ps", "-a", "--format", "{{.Names}}\t{{.Status}}\t{{.Ports}}"],
        timeout=config.TIMEOUT_DOCKER,
    )
    containers = parse_docker_ps_lines(out) if ok else []
    entries.append(_dashboard_container_entry(containers))

    statuses = [e["status"] for e in entries]
    overall = "ok"
    if any(s == "critical" for s in statuses):
        overall = "critical"
    elif any(s in ("warning", "not_configured") for s in statuses):
        overall = "warning"

    alerts = _service_alerts(entries)
    return {"status": overall, "services": entries, "alerts": alerts}


def _service_alerts(entries: list[dict[str, Any]]) -> list[dict[str, str]]:
    alerts: list[dict[str, str]] = []
    for entry in entries:
        status = entry.get("status", "ok")
        if status == "ok":
            continue
        severity = "critical" if status == "critical" else "warning"
        label = entry.get("label", "service")
        unit = entry.get("unit", "")
        active = entry.get("active", "unknown")
        alerts.append(
            {
                "severity": severity,
                "category": "services",
                "code": status.upper(),
                "volume": label,
                "mount_path": unit,
                "message": f"{label} ({unit}): {active}",
            }
        )
    return alerts


def check_storage() -> dict[str, Any]:
    return check_raven_storage()


def check_docker() -> dict[str, Any]:
    docker_svc = _systemctl_unit_state("docker")
    result: dict[str, Any] = {
        "status": "ok",
        "daemon_active": docker_svc["active_bool"],
        "daemon_state": docker_svc["active"],
        "running_count": 0,
        "stopped_count": 0,
        "containers": [],
        "alerts": [],
    }

    if not docker_svc["active_bool"]:
        result["status"] = "critical"
        result["alerts"].append(
            {
                "severity": "critical",
                "category": "docker",
                "code": "DAEMON_INACTIVE",
                "volume": "docker",
                "mount_path": "docker.service",
                "message": f"Docker daemon {docker_svc['active']}",
            }
        )

    ok, out = run_command(
        ["docker", "ps", "-a", "--format", "{{.Names}}\t{{.Status}}\t{{.Ports}}"],
        timeout=config.TIMEOUT_DOCKER,
    )
    if not ok:
        result["detail"] = out
        if is_timeout(out):
            result["status"] = "critical"
            result["alerts"].append(
                {
                    "severity": "critical",
                    "category": "docker",
                    "code": "TIMEOUT",
                    "volume": "docker",
                    "mount_path": "docker ps",
                    "message": "docker ps timed out",
                }
            )
        elif result["status"] == "ok":
            result["status"] = "warning"
        return result

    containers = parse_docker_ps_lines(out)
    result["containers"] = containers
    running = [c for c in containers if c["status"].lower().startswith("up")]
    stopped = [c for c in containers if not c["status"].lower().startswith("up")]
    result["running_count"] = len(running)
    result["stopped_count"] = len(stopped)
    return result


def _pgrep_running(pattern: str) -> dict[str, Any]:
    ok, out = run_command(["pgrep", "-af", pattern], timeout=config.DEFAULT_SUBPROCESS_TIMEOUT)
    if ok and out.strip():
        lines = [ln.strip() for ln in out.splitlines() if ln.strip()]
        return {"running": True, "matches": lines[:5], "status": "ok"}
    if not ok and "not found" in out:
        return {"running": False, "matches": [], "status": "warning", "detail": out}
    return {
        "running": False,
        "matches": [],
        "status": "warning" if not ok else "ok",
        "detail": out or None,
    }


def _latest_log_mtime(logs_dir: Path) -> dict[str, Any]:
    if not logs_dir.is_dir():
        return {"path": None, "modified_at": None, "status": "warning"}

    latest: Path | None = None
    latest_ts = -1.0
    try:
        for pattern in ("*.log", "vulture.log"):
            for path in logs_dir.glob(pattern):
                if not path.is_file():
                    continue
                try:
                    ts = path.stat().st_mtime
                except OSError:
                    continue
                if ts > latest_ts:
                    latest_ts = ts
                    latest = path
    except OSError:
        return {"path": None, "modified_at": None, "status": "warning"}

    if latest is None:
        return {"path": None, "modified_at": None, "status": "warning"}

    tz = ZoneInfo(config.DISPLAY_TIMEZONE)
    mtime = datetime.fromtimestamp(latest_ts, tz=timezone.utc).astimezone(tz).isoformat(timespec="seconds")
    return {"path": str(latest), "modified_at": mtime, "status": "ok"}


def check_vulture_runtime() -> dict[str, Any]:
    logs_dir = config.LOGS_DIR
    host_logs = Path(host_path(str(config.PROJECT_ROOT / "logs")))
    if host_logs.is_dir():
        logs_dir = host_logs

    bot = _pgrep_running("discord_bot.py")
    scheduler_main = _pgrep_running("main.py")
    scheduler_alt = _pgrep_running("scheduler")

    tmux: dict[str, Any] = {"available": False, "sessions": [], "status": "warning"}
    ok, out = run_command(["tmux", "ls"], timeout=5.0)
    if ok:
        tmux = {"available": True, "sessions": parse_tmux_sessions(out), "status": "ok"}
    elif "not found" in out:
        tmux["detail"] = "tmux not installed"
    else:
        tmux["detail"] = out or "tmux unavailable"

    log_info = _latest_log_mtime(logs_dir)
    scheduler_running = scheduler_main.get("running") or scheduler_alt.get("running")

    statuses = [
        bot.get("status", "warning"),
        scheduler_main.get("status", "warning"),
        tmux.get("status", "warning"),
        log_info.get("status", "warning"),
    ]

    return {
        "status": combine_status(*statuses),
        "discord_bot": bot,
        "scheduler_main": scheduler_main,
        "scheduler_pattern": scheduler_alt,
        "scheduler_running": scheduler_running,
        "tmux": tmux,
        "latest_log": log_info,
    }


def check_systemd_failed() -> dict[str, Any]:
    ok, out = run_command(
        ["systemctl", "--failed", "--no-pager"],
        timeout=config.TIMEOUT_SYSTEMCTL,
    )
    if not ok:
        return {
            "status": "warning",
            "count": 0,
            "units": [],
            "detail": out,
            "alerts": [],
        }

    count, names = parse_systemctl_failed(out)
    status = "ok" if count == 0 else "critical"
    alerts: list[dict[str, str]] = []
    if count:
        alerts.append(
            {
                "severity": "critical",
                "category": "systemd_failed",
                "code": "FAILED_UNITS",
                "volume": "systemd",
                "mount_path": "",
                "message": f"{count} failed systemd unit(s): {', '.join(names[:5])}",
            }
        )
    return {
        "status": status,
        "count": count,
        "units": names,
        "raw_excerpt": out.splitlines()[:20],
        "alerts": alerts,
    }


def get_hostname() -> str:
    ok, out = run_command(["hostname"], timeout=config.DEFAULT_SUBPROCESS_TIMEOUT)
    if ok and out.strip():
        return out.splitlines()[0].strip()
    try:
        host_file = Path(host_path("/etc/hostname"))
        if host_file.is_file():
            return host_file.read_text(encoding="utf-8").strip()
    except OSError:
        pass
    return os.uname().nodename


def _collect_alerts(checks: dict[str, Any]) -> list[dict[str, str]]:
    alerts: list[dict[str, str]] = []
    for section in ("storage", "services", "docker", "systemd_failed", "pelican_backup"):
        section_alerts = checks.get(section, {}).get("alerts", [])
        if isinstance(section_alerts, list):
            alerts.extend(section_alerts)
    return alerts


def run_all_checks() -> dict[str, Any]:
    """Run every check; individual failures degrade status instead of raising."""
    warnings: list[str] = []
    critical: list[str] = []

    def _safe(name: str, fn) -> dict[str, Any]:
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 — keep Canary alive
            warnings.append(f"{name}: check error ({exc})")
            return {"status": "warning", "error": str(exc), "alerts": []}

    checks = {
        "internet": _safe("internet", check_internet),
        "network": _safe("network", check_network),
        "services": _safe("services", check_services),
        "storage": _safe("storage", check_storage),
        "docker": _safe("docker", check_docker),
        "vulture_runtime": _safe("vulture_runtime", check_vulture_runtime),
        "systemd_failed": _safe("systemd_failed", check_systemd_failed),
        "pelican_backup": _safe("pelican_backup", check_pelican_backup),
    }

    section_statuses = [checks[key].get("status", "warning") for key in checks]
    overall = combine_status(*section_statuses)

    alerts = _collect_alerts(checks)

    for key, data in checks.items():
        status = data.get("status", "ok")
        if status == "critical":
            critical.append(f"{key}: {data.get('detail') or status}")
        elif status in ("warning", "not_configured", "degraded"):
            warnings.append(f"{key}: {data.get('detail') or status}")

    tz = ZoneInfo(config.DISPLAY_TIMEZONE)
    generated_at = datetime.now(tz=timezone.utc).astimezone(tz).isoformat(timespec="seconds")

    return {
        "generated_at": generated_at,
        "host": get_hostname(),
        "overall_status": overall,
        "checks": checks,
        "alerts": alerts,
        "warnings": warnings,
        "critical": critical,
    }
