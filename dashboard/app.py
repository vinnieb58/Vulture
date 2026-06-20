"""
Nest v1 Dashboard — tablet-first household/Raven overview.

Observability only: no hunt mutations, scheduler controls, service restarts,
or other write/admin actions. Intended for local / Tailscale access on Raven.

Routes
------
/          Nest Overview    tablet-friendly summary cards (default)
/kestrel   Kestrel detail   read-only energy charts and summaries
/storage   Storage detail   per-drive status and usage
/vulture   Vulture detail   scheduler / bot / hunts
/advanced  Raven Ops        original dense operational view (v0.2)
/raven/health  Raven Health Details  Glances-driven live telemetry
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
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
from house_formatting import format_house_card_display
from house_status import read_house_status
from kestrel_formatting import format_kestrel_card_display, format_kestrel_detail_display
from kestrel_metrics import get_detail_metrics, get_home_metrics
from kestrel_status import read_kestrel_status
from nest_hvac_formatting import format_hvac_section
from log_readers import LOG_PATH, read_log_snapshot
from raven_health_details import RAVEN_HEALTH_REFRESH_SECONDS, build_raven_health_details
from raven_metrics_history import (
    CPU_SAT_CRITICAL_MINUTES_1H,
    CPU_SAT_WARN_MINUTES_1H,
    TEMP_CRITICAL_CELSIUS,
    TEMP_WARN_CELSIUS,
    get_metrics_summary,
)
from metrics_sampler import start_metrics_sampler, stop_metrics_sampler
from vulture_runtime import get_vulture_runtime

AUTO_REFRESH_SECONDS = int(os.environ.get("DASHBOARD_AUTO_REFRESH_SECONDS", "30"))


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Start background metrics sampler on container startup."""
    start_metrics_sampler()
    yield
    stop_metrics_sampler()


app = FastAPI(title="Nest Dashboard", version="1.0", lifespan=lifespan)
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
app.mount(
    "/static",
    StaticFiles(directory=str(Path(__file__).parent / "static")),
    name="static",
)


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

def _raven_operating_issues(metrics: dict[str, Any]) -> list[tuple[str, str]]:
    """Return (severity, message) pairs for CPU/temp operating thresholds."""
    issues: list[tuple[str, str]] = []

    temp_now = metrics.get("temp_now_celsius")
    if temp_now is not None:
        if temp_now >= TEMP_CRITICAL_CELSIUS:
            issues.append(
                ("FAIL", f"CPU temperature critical: {metrics.get('temp_now', f'{temp_now:.0f}°C')}")
            )
        elif temp_now >= TEMP_WARN_CELSIUS:
            issues.append(
                ("WARN", f"CPU temperature high: {metrics.get('temp_now', f'{temp_now:.0f}°C')}")
            )

    cpu_sat_minutes = metrics.get("cpu_above_90_minutes_1h_raw")
    if cpu_sat_minutes is not None:
        if cpu_sat_minutes >= CPU_SAT_CRITICAL_MINUTES_1H:
            issues.append(
                (
                    "FAIL",
                    f"CPU above 90% for {metrics.get('cpu_above_90_minutes_1h', f'{cpu_sat_minutes:.0f} min')} in the last hour",
                )
            )
        elif cpu_sat_minutes >= CPU_SAT_WARN_MINUTES_1H:
            issues.append(
                (
                    "WARN",
                    f"CPU above 90% for {metrics.get('cpu_above_90_minutes_1h', f'{cpu_sat_minutes:.0f} min')} in the last hour",
                )
            )

    return issues


def _compute_raven_card(
    raven: dict[str, Any],
    services: list[ServiceStatus],
    docker: DockerSnapshot,
    metrics_peaks: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Plain-English Raven health card for the Nest overview."""
    # Actionable failed units drive the FAIL status.  Ignored units (e.g.
    # systemd-networkd-wait-online.service) are noisy on Ubuntu/headless servers
    # and are surfaced as informational items without triggering HEALTH FAIL.
    failed = raven.get("failed_units", [])
    ignored_failed = raven.get("ignored_failed_units", [])
    internet_ok = raven.get("internet_ok", True)
    metrics = metrics_peaks or {}

    issues: list[str] = []
    severities: list[str] = []
    if failed:
        plural = "s" if len(failed) != 1 else ""
        names = ", ".join(failed[:3])
        issues.append(f"{len(failed)} failed systemd unit{plural}: {names}")
        severities.append("FAIL")
    if not internet_ok:
        issues.append("Internet unreachable")
        severities.append("WARN")

    critical_labels = {"SSH", "tailscaled", "docker", "vulture-bot"}
    for svc in services:
        if svc.label in critical_labels and svc.active not in (
            "active", "not found", "not configured", "unknown"
        ):
            issues.append(f"{svc.label} is {svc.active}")
            severities.append("WARN")

    for severity, message in _raven_operating_issues(metrics):
        issues.append(message)
        severities.append(severity)

    if issues:
        if "FAIL" in severities or failed:
            status = "FAIL"
        else:
            status = "WARN"
        headline = issues[0]
    else:
        status = "OK"
        headline = "Raven is healthy"

    load_average = metrics.get("load_average") or raven.get("load_average")

    return {
        "status": status,
        "headline": headline,
        "issues": issues,
        "ignored_failed_units": ignored_failed,
        "hostname": raven.get("hostname", "unknown"),
        "uptime": raven.get("uptime", "unknown"),
        "cpu_now": metrics.get("cpu_now"),
        "cpu_above_90_1h": metrics.get("cpu_above_90_minutes_1h"),
        "temp_now": metrics.get("temp_now"),
        "load_average": load_average,
        "containers_running": docker.running_count,
        "peak_cpu_1h": metrics.get("peak_cpu_1h"),
        "peak_cpu_24h": metrics.get("peak_cpu_24h"),
        "peak_temp_24h": metrics.get("temp_high_24h"),
        "peak_memory_24h": metrics.get("peak_memory_24h"),
        "glances_status": metrics.get("glances_status"),
        "glances_available": metrics.get("glances_available"),
        "metrics_source": metrics.get("metrics_source"),
        "peaks": metrics,
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
                    # Below warning threshold — always show percentage plus capacity context
                    if mount.used and mount.size:
                        detail = f"{mount.used} / {mount.size}"
                        line_ok = f"{mount.label}: {pct:.0f}% used ({detail})"
                    else:
                        line_ok = f"{mount.label}: {pct:.0f}% used"
                    drive_lines.append({"label": mount.label, "line": line_ok, "status": "OK"})
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

    if sched_status in ("fresh", "running", "seen"):
        status = "OK"
        if sched_status == "running":
            headline = "Vulture hunt cycle in progress"
        elif next_run:
            headline = f"Vulture scheduler active; next run {next_run}"
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
        "next_run": next_run,
        "last_success": freshness.get("last_success"),
        "bot_running": bot_running,
        "active_hunts": hunt_counts.get("active", 0),
        "total_hunts": hunt_counts.get("total", 0),
    }


def _compute_house_card(house: dict[str, Any]) -> dict[str, Any]:
    """Plain-English House card for household climate (Nest thermostats)."""
    return format_house_card_display(house)


def _compute_kestrel_card(kestrel: dict[str, Any]) -> dict[str, Any]:
    """Plain-English Kestrel energy card for the Nest overview."""
    state = kestrel.get("state", "no_data")
    status_labels = {
        "available": "Available",
        "no_data": "No data",
        "error": "Error",
    }
    style_map = {
        "available": "ok",
        "no_data": "unknown",
        "error": "fail",
    }
    try:
        metrics = get_home_metrics()
    except Exception:
        metrics = {}
    display = format_kestrel_card_display(kestrel, metrics)
    return {
        "status": status_labels.get(state, "No data"),
        "style": style_map.get(state, "unknown"),
        "headline": kestrel.get("headline", "No energy data yet"),
        **display,
    }


def _compute_kestrel_detail(kestrel: dict[str, Any]) -> dict[str, Any]:
    """Read-only Kestrel detail page context."""
    state = kestrel.get("state", "no_data")
    status_labels = {
        "available": "Available",
        "no_data": "No data",
        "error": "Error",
    }
    style_map = {
        "available": "ok",
        "no_data": "unknown",
        "error": "fail",
    }
    try:
        metrics = get_detail_metrics()
    except Exception:
        metrics = {"available": False}
    display = format_kestrel_detail_display(kestrel, metrics)
    try:
        hvac = format_hvac_section()
    except Exception:
        hvac = {
            "state": "error",
            "warning": "Could not load HVAC runtime data",
            "summaries": [],
            "collection": {
                "status": "Missing",
                "status_key": "missing",
                "style": "unknown",
                "samples_last_30m_display": "0",
                "latest_age": None,
                "zones": "—",
                "missing": True,
            },
            "correlation": {"available": False, "rows": []},
        }
    return {
        "status": status_labels.get(state, "No data"),
        "style": style_map.get(state, "unknown"),
        "headline": kestrel.get("headline", "No energy data yet"),
        "hvac": hvac,
        **display,
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
    metrics_peaks = get_metrics_summary()
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
    return refreshed_at, logs, db, raven, metrics_peaks, services, storage, docker, vulture


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

@app.get("/health")
async def health() -> JSONResponse:
    """Lightweight process health probe used by Docker HEALTHCHECK and curl.

    Returns HTTP 200 with JSON payload if the dashboard process is running.
    Does NOT collect host data — this is a liveness check only.

    ``build_git_commit`` and ``build_timestamp`` are baked in at image build time
    so deploy scripts can confirm the running container matches the expected code.
    """
    return JSONResponse(
        {
            "status": "ok",
            "server_time": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
            "build_git_commit": os.environ.get("DASHBOARD_BUILD_GIT_COMMIT", "unknown"),
            "build_timestamp": os.environ.get("DASHBOARD_BUILD_TIMESTAMP", "unknown"),
        }
    )


@app.get("/scheduler-health")
async def scheduler_health() -> JSONResponse:
    """Scheduler health debug endpoint.

    Returns rich evidence about the Vulture scheduler so operators can diagnose
    WARN/stale conditions without needing host shell access.  Fields document
    which data source drove the status determination.

    This endpoint performs host commands (systemctl, journalctl) and is NOT
    suitable as a liveness probe — use ``/health`` for that.
    """
    from log_readers import read_log_snapshot
    from vulture_runtime import _evaluate_scheduler_health

    logs = read_log_snapshot()
    sched = _evaluate_scheduler_health(logs.get("lines", []))
    return JSONResponse(
        {
            "scheduler_status": sched["status"],
            "detail": sched["detail"],
            "warning": sched.get("warning"),
            "timer_active": sched.get("timer_active"),
            "timer_enabled": sched.get("timer_enabled"),
            "service_active": sched.get("service_active"),
            "next_run": sched.get("next_run"),
            "last_success": sched.get("last_success"),
            "last_success_source": sched.get("last_success_source"),
            "journal_available": sched.get("journal_available"),
            "log_mtime_age_minutes": sched.get("log_mtime_age_minutes"),
            "scheduler_status_reason": sched.get("scheduler_status_reason"),
            "server_time": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        }
    )


@app.get("/", response_class=HTMLResponse)
async def nest_overview(request: Request) -> HTMLResponse:
    """Nest Overview — tablet-friendly summary with plain-English status."""
    refreshed_at, logs, db, raven, metrics_peaks, services, storage, docker, vulture = _collect_data()
    warnings = _build_warnings(db, logs, raven, vulture, services, storage, docker)

    kestrel = read_kestrel_status()
    house = read_house_status()
    nest = {
        "raven": _compute_raven_card(raven, services, docker, metrics_peaks),
        "storage": _compute_storage_card(storage),
        "vulture": _compute_vulture_card(vulture, db),
        "house": _compute_house_card(house),
        "network": _compute_network_card(raven),
        "kestrel": _compute_kestrel_card(kestrel),
    }

    house_warning = house.get("warning")
    if isinstance(house_warning, str) and house_warning:
        warnings.append(house_warning)

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


@app.get("/kestrel", response_class=HTMLResponse)
async def kestrel_detail(request: Request) -> HTMLResponse:
    """Kestrel detail — read-only energy charts and summaries."""
    refreshed_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    kestrel_status = read_kestrel_status()
    kestrel = _compute_kestrel_detail(kestrel_status)

    context = {
        "title": "Kestrel",
        "version": "1.0",
        "page": "kestrel",
        "refreshed_at": refreshed_at,
        "auto_refresh_seconds": AUTO_REFRESH_SECONDS,
        "kestrel": kestrel,
    }
    return templates.TemplateResponse(request, "kestrel.html", context)


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


@app.get("/api/raven/health/glances")
async def raven_health_glances_api() -> JSONResponse:
    """Normalized Glances telemetry for the Raven Health details page."""
    raven = get_raven_health()
    docker = get_docker_snapshot()
    details = build_raven_health_details(
        raven=raven,
        docker_running=docker.running_count,
    )
    return JSONResponse(details)


@app.get("/raven/health", response_class=HTMLResponse)
async def raven_health_detail(request: Request) -> HTMLResponse:
    """Raven Health Details — live Glances telemetry with auto-refresh."""
    raven = get_raven_health()
    docker = get_docker_snapshot()
    details = build_raven_health_details(
        raven=raven,
        docker_running=docker.running_count,
    )
    context = {
        "title": "Raven Health Details (Glances)",
        "version": "1.0",
        "page": "raven_health",
        "refreshed_at": details["updated_at"],
        "auto_refresh_seconds": RAVEN_HEALTH_REFRESH_SECONDS,
        "details": details,
    }
    return templates.TemplateResponse(request, "raven_health.html", context)


@app.get("/advanced", response_class=HTMLResponse)
async def advanced_ops(request: Request) -> HTMLResponse:
    """Raven Ops — original dense operational view for troubleshooting."""
    refreshed_at, logs, db, raven, metrics_peaks, services, storage, docker, vulture = _collect_data()
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
