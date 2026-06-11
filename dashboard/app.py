"""
Nest v1 Dashboard — tablet-first household/Raven overview.

Observability only: no hunt mutations, scheduler controls, service restarts,
or other write/admin actions. Intended for local / Tailscale access on Raven.

Routes
------
/          Nest Overview    tablet-friendly summary cards (default)
/storage   Storage detail   per-drive status and usage
/vulture   Vulture detail   scheduler / bot / hunts
/advanced  Raven Ops        original dense operational view (v0.2)
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from db_readers import DB_PATH, read_db_snapshot
from host_status import (
    DockerSnapshot,
    ServiceStatus,
    StorageStatus,
    get_docker_snapshot,
    get_raven_health,
    get_service_statuses,
    get_storage_status,
    status_display_class,
)
from log_readers import LOG_PATH, read_log_snapshot
from vulture_runtime import get_vulture_runtime

AUTO_REFRESH_SECONDS = int(os.environ.get("DASHBOARD_AUTO_REFRESH_SECONDS", "30"))

app = FastAPI(title="Nest Dashboard", version="1.0")
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


# ---------------------------------------------------------------------------
# Shared warning collection
# ---------------------------------------------------------------------------

def _collect_warnings(*sections: dict[str, Any]) -> list[str]:
    warnings: list[str] = []
    for section in sections:
        for key in ("warning", "warnings"):
            value = section.get(key)
            if isinstance(value, str) and value:
                warnings.append(value)
            elif isinstance(value, list):
                warnings.extend(str(v) for v in value if v)
    return warnings


# ---------------------------------------------------------------------------
# Nest overview card computation
# ---------------------------------------------------------------------------

def _compute_raven_card(
    raven: dict[str, Any],
    services: list[ServiceStatus],
    docker: DockerSnapshot,
) -> dict[str, Any]:
    """Plain-English Raven health card for the Nest overview."""
    failed = raven.get("failed_units", [])
    internet_ok = raven.get("internet_ok", True)

    issues: list[str] = []
    if failed:
        plural = "s" if len(failed) != 1 else ""
        names = ", ".join(failed[:3])
        issues.append(f"{len(failed)} failed systemd unit{plural}: {names}")
    if not internet_ok:
        issues.append("Internet unreachable")

    critical_labels = {"SSH", "tailscaled", "docker", "vulture-bot"}
    for svc in services:
        if svc.label in critical_labels and svc.active not in (
            "active", "not found", "not configured", "unknown"
        ):
            issues.append(f"{svc.label} is {svc.active}")

    if issues:
        status = "FAIL" if failed else "WARN"
        headline = issues[0]
    else:
        status = "OK"
        headline = "Raven is healthy"

    memory = raven.get("memory")
    mem_str: str | None = None
    if memory:
        mem_str = f"{memory.used} / {memory.total}"
        if memory.percent_used is not None:
            mem_str += f" ({memory.percent_used:.0f}%)"

    return {
        "status": status,
        "headline": headline,
        "issues": issues,
        "hostname": raven.get("hostname", "unknown"),
        "uptime": raven.get("uptime", "unknown"),
        "load_average": raven.get("load_average"),
        "memory": mem_str,
        "containers_running": docker.running_count,
    }


def _compute_storage_card(storage: list[StorageStatus]) -> dict[str, Any]:
    """Plain-English storage card for the Nest overview."""
    issues: list[str] = []
    drive_lines: list[dict[str, Any]] = []
    overall_status = "OK"

    for mount in storage:
        if mount.legacy:
            # Legacy drives: only show if mounted
            if mount.status in ("OK", "OK_AUTOMOUNTED"):
                drive_lines.append({"label": mount.label, "line": f"{mount.label}: mounted (legacy)", "status": "OK"})
            continue

        if mount.status in ("OK", "OK_AUTOMOUNTED"):
            if mount.percent_used is not None:
                pct = mount.percent_used
                line = f"{mount.label}: {pct:.0f}% used"
                if pct >= 90:
                    drive_lines.append({"label": mount.label, "line": line, "status": "FAIL"})
                    issues.append(line)
                    if overall_status not in ("FAIL",):
                        overall_status = "WARN"
                elif pct >= 80:
                    drive_lines.append({"label": mount.label, "line": line, "status": "WARN"})
                    issues.append(line)
                    if overall_status == "OK":
                        overall_status = "WARN"
                else:
                    drive_lines.append({"label": mount.label, "line": f"{mount.label}: {pct:.0f}% used", "status": "OK"})
            else:
                drive_lines.append({"label": mount.label, "line": f"{mount.label}: mounted", "status": "OK"})
        elif mount.required:
            line = f"{mount.label}: not mounted (required)"
            drive_lines.append({"label": mount.label, "line": line, "status": "FAIL"})
            issues.append(line)
            overall_status = "FAIL"
        else:
            line = f"{mount.label}: not mounted"
            drive_lines.append({"label": mount.label, "line": line, "status": "WARN"})
            if overall_status == "OK":
                overall_status = "WARN"

    if not issues:
        if overall_status == "OK":
            headline = "All storage healthy"
        else:
            headline = "Some storage warnings"
    else:
        headline = issues[0]

    return {
        "status": overall_status,
        "headline": headline,
        "issues": issues,
        "drives": drive_lines,
    }


def _compute_vulture_card(vulture: dict[str, Any], db: dict[str, Any]) -> dict[str, Any]:
    """Plain-English Vulture status card for the Nest overview."""
    freshness = vulture.get("scheduler_freshness", {})
    sched_status = freshness.get("status", "unknown")
    next_run = freshness.get("next_run")
    next_run_relative = freshness.get("next_run_relative")

    next_run_display = next_run
    if next_run and next_run_relative:
        next_run_display = f"{next_run} ({next_run_relative})"

    if sched_status in ("fresh", "running", "seen"):
        status = "OK"
        if sched_status == "running":
            headline = "Vulture hunt cycle in progress"
        elif next_run_display:
            headline = f"Vulture scheduler active; next run {next_run_display}"
        else:
            headline = "Vulture scheduler active"
    elif sched_status == "stale":
        status = "WARN"
        headline = "Vulture scheduler may be stale"
    elif sched_status == "unhealthy":
        status = "FAIL"
        headline = "Vulture scheduler unhealthy"
    else:
        status = "UNKNOWN"
        headline = "Vulture scheduler status unknown"

    bot_running = False
    processes = vulture.get("processes", [])
    for proc in processes:
        if "bot" in proc.label.lower() and proc.running:
            bot_running = True
            break

    hunt_counts = db.get("hunt_counts", {})
    return {
        "status": status,
        "headline": headline,
        "scheduler_status": sched_status,
        "next_run": next_run_display,
        "last_success": freshness.get("last_success"),
        "bot_running": bot_running,
        "active_hunts": hunt_counts.get("active", 0),
        "total_hunts": hunt_counts.get("total", 0),
    }


def _compute_network_card(raven: dict[str, Any]) -> dict[str, Any]:
    """Plain-English network status card for the Nest overview."""
    lan_ip = raven.get("lan_ip")
    ts_ip = raven.get("tailscale_ip")
    internet_ok = raven.get("internet_ok", True)

    issues: list[str] = []
    if not lan_ip:
        issues.append("LAN IP unavailable")
    if not ts_ip:
        issues.append("Tailscale disconnected")
    if not internet_ok:
        issues.append("Internet unreachable")

    if issues:
        status = "WARN"
        headline = issues[0]
    else:
        status = "OK"
        headline = "Network OK"

    return {
        "status": status,
        "headline": headline,
        "lan_ip": lan_ip or "—",
        "tailscale_ip": ts_ip or "—",
        "internet_ok": internet_ok,
        "issues": issues,
    }


# ---------------------------------------------------------------------------
# Shared data collection
# ---------------------------------------------------------------------------

def _collect_data() -> tuple[
    str,
    dict[str, Any],
    dict[str, Any],
    dict[str, Any],
    list[ServiceStatus],
    list[StorageStatus],
    DockerSnapshot,
    dict[str, Any],
]:
    refreshed_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    logs = read_log_snapshot()
    db = read_db_snapshot(log_lines=logs.get("lines", []))
    raven = get_raven_health()
    services = get_service_statuses()
    storage = get_storage_status()
    for mount in storage:
        mount.display_class = status_display_class(  # type: ignore[attr-defined]
            mount.status,
            required=mount.required,
            legacy=mount.legacy,
        )
    docker = get_docker_snapshot()
    vulture = get_vulture_runtime(log_lines=logs.get("lines", []))
    return refreshed_at, logs, db, raven, services, storage, docker, vulture


def _build_warnings(
    db: dict[str, Any],
    logs: dict[str, Any],
    raven: dict[str, Any],
    vulture: dict[str, Any],
    services: list[ServiceStatus],
    storage: list[StorageStatus],
    docker: DockerSnapshot,
) -> list[str]:
    warnings = _collect_warnings(db, logs, raven, vulture)
    for svc in services:
        if svc.warning:
            warnings.append(svc.warning)
    for mount in storage:
        if mount.warning:
            warnings.append(f"{mount.label}: {mount.warning}")
    if docker.warning:
        warnings.append(docker.warning)
    return warnings


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def nest_overview(request: Request) -> HTMLResponse:
    """Nest Overview — tablet-friendly summary with plain-English status."""
    refreshed_at, logs, db, raven, services, storage, docker, vulture = _collect_data()
    warnings = _build_warnings(db, logs, raven, vulture, services, storage, docker)

    nest = {
        "raven": _compute_raven_card(raven, services, docker),
        "storage": _compute_storage_card(storage),
        "vulture": _compute_vulture_card(vulture, db),
        "network": _compute_network_card(raven),
    }

    context = {
        "title": "Nest",
        "version": "1.0",
        "page": "home",
        "refreshed_at": refreshed_at,
        "auto_refresh_seconds": AUTO_REFRESH_SECONDS,
        "warnings": warnings,
        "nest": nest,
        "raven": raven,
        "db": db,
    }
    return templates.TemplateResponse(request, "nest.html", context)


@app.get("/storage", response_class=HTMLResponse)
async def storage_detail(request: Request) -> HTMLResponse:
    """Storage detail — per-drive status and usage, tablet-friendly."""
    refreshed_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    storage = get_storage_status()
    for mount in storage:
        mount.display_class = status_display_class(  # type: ignore[attr-defined]
            mount.status,
            required=mount.required,
            legacy=mount.legacy,
        )
    storage_card = _compute_storage_card(storage)

    context = {
        "title": "Storage",
        "version": "1.0",
        "page": "storage",
        "refreshed_at": refreshed_at,
        "auto_refresh_seconds": AUTO_REFRESH_SECONDS,
        "storage": storage,
        "storage_card": storage_card,
    }
    return templates.TemplateResponse(request, "storage.html", context)


@app.get("/vulture", response_class=HTMLResponse)
async def vulture_detail(request: Request) -> HTMLResponse:
    """Vulture detail — scheduler, bot, hunts, recent logs."""
    refreshed_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    logs = read_log_snapshot()
    db = read_db_snapshot(log_lines=logs.get("lines", []))
    vulture = get_vulture_runtime(log_lines=logs.get("lines", []))
    vulture_card = _compute_vulture_card(vulture, db)

    context = {
        "title": "Vulture",
        "version": "1.0",
        "page": "vulture",
        "refreshed_at": refreshed_at,
        "auto_refresh_seconds": AUTO_REFRESH_SECONDS,
        "vulture": vulture,
        "db": db,
        "logs": logs,
        "vulture_card": vulture_card,
    }
    return templates.TemplateResponse(request, "vulture.html", context)


@app.get("/advanced", response_class=HTMLResponse)
async def advanced_ops(request: Request) -> HTMLResponse:
    """Raven Ops — original dense operational view for troubleshooting."""
    refreshed_at, logs, db, raven, services, storage, docker, vulture = _collect_data()
    warnings = _build_warnings(db, logs, raven, vulture, services, storage, docker)

    context = {
        "title": "Raven Ops",
        "version": "0.2",
        "page": "advanced",
        "server_time": refreshed_at,
        "refreshed_at": refreshed_at,
        "auto_refresh_seconds": AUTO_REFRESH_SECONDS,
        "db_path": str(DB_PATH),
        "log_path": str(LOG_PATH),
        "warnings": warnings,
        "db": db,
        "logs": logs,
        "raven": raven,
        "services": services,
        "storage": storage,
        "docker": docker,
        "vulture": vulture,
    }
    return templates.TemplateResponse(request, "advanced.html", context)
