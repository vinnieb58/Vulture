"""
Vulture Dashboard v0.2 — read-only operational status page.

Observability only: no hunt mutations, scheduler controls, service restarts,
or other write/admin actions. Intended for local / Tailscale access on Raven.
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
    get_docker_snapshot,
    get_raven_health,
    get_service_statuses,
    get_storage_status,
    status_display_class,
)
from log_readers import LOG_PATH, read_log_snapshot
from vulture_runtime import get_vulture_runtime

AUTO_REFRESH_SECONDS = int(os.environ.get("DASHBOARD_AUTO_REFRESH_SECONDS", "60"))

app = FastAPI(title="Vulture Dashboard", version="0.2")
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


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


@app.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    refreshed_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    logs = read_log_snapshot()
    db = read_db_snapshot(log_lines=logs.get("lines", []))
    raven = get_raven_health()
    services = get_service_statuses()
    storage = get_storage_status()
    for mount in storage:
        mount.display_class = status_display_class(
            mount.status,
            required=mount.required,
            legacy=mount.legacy,
        )
    docker = get_docker_snapshot()
    vulture = get_vulture_runtime(log_lines=logs.get("lines", []))

    warnings = _collect_warnings(db, logs, raven, vulture)
    for svc in services:
        if svc.warning:
            warnings.append(svc.warning)
    for mount in storage:
        if mount.warning:
            warnings.append(f"{mount.label}: {mount.warning}")
    if docker.warning:
        warnings.append(docker.warning)

    context = {
        "title": "Vulture Dashboard",
        "version": "0.2",
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
    return templates.TemplateResponse(request, "index.html", context)
