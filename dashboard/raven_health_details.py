"""Normalized Raven Health details payload for HTML and JSON responses."""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Any

from glances_client import (
    GLANCES_UNAVAILABLE_LABEL,
    fetch_glances_details_snapshot,
    glances_enabled,
)
from host_cpu_metrics import NOT_AVAILABLE_LABEL, format_celsius
from raven_metrics_history import (
    COLLECTING_LABEL,
    MetricsSample,
    get_metrics_summary,
    prune_samples,
    read_history,
)
from storage_probe import StorageStatus, get_storage_status

RAVEN_HEALTH_REFRESH_SECONDS = int(
    os.environ.get("DASHBOARD_RAVEN_HEALTH_REFRESH_SECONDS", "5")
)
HISTORY_PATH = os.environ.get(
    "DASHBOARD_METRICS_HISTORY_PATH",
    "/app/data/raven_metrics_history.jsonl",
)
HISTORY_MIN_CHART_POINTS = int(
    os.environ.get("DASHBOARD_HISTORY_MIN_CHART_POINTS", "3")
)


def _format_bytes(value_bytes: int | None) -> str | None:
    if value_bytes is None:
        return None
    return f"{value_bytes / (1024**3):.1f} GB"


def _format_uptime(seconds: float | None) -> str | None:
    if seconds is None:
        return None
    total = int(seconds)
    days, remainder = divmod(total, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, _ = divmod(remainder, 60)
    parts: list[str] = []
    if days:
        parts.append(f"{days}d")
    if hours or days:
        parts.append(f"{hours}h")
    parts.append(f"{minutes}m")
    return " ".join(parts)


def _format_rate_bps(rate_bps: float | None) -> str | None:
    if rate_bps is None:
        return None
    if rate_bps >= 1024**2:
        return f"{rate_bps / (1024**2):.1f} MB/s"
    if rate_bps >= 1024:
        return f"{rate_bps / 1024:.1f} KB/s"
    return f"{rate_bps:.0f} B/s"


def _storage_status_to_disk(status: StorageStatus) -> dict[str, Any] | None:
    """Map a storage probe row to the Glances-style disk dict used by the UI."""
    if status.percent_used is None and not status.used:
        if status.status not in ("OK", "OK_AUTOMOUNTED"):
            return None
    mount = status.path
    if mount.startswith("/host/root"):
        mount = mount.removeprefix("/host/root") or "/"
    return {
        "device": status.actual_source or status.name or "—",
        "mount": mount,
        "percent": status.percent_used,
        "percent_display": (
            f"{status.percent_used:.0f}%" if status.percent_used is not None else None
        ),
        "used_display": status.used,
        "total_display": status.size,
        "free_display": status.available,
        "source": "storage_probe",
        "status": status.status,
    }


def _disks_from_storage_probe() -> list[dict[str, Any]]:
    """Fallback disk usage when Glances /api/4/fs is empty or unavailable."""
    try:
        rows = get_storage_status()
    except Exception:
        return []
    disks: list[dict[str, Any]] = []
    for row in rows:
        if row.legacy and row.status == "LEGACY_PATH":
            continue
        if row.role == "root" or row.path.startswith("/mnt/storage"):
            disk = _storage_status_to_disk(row)
            if disk is not None:
                disks.append(disk)
    return disks


def _resolve_disks(glances: dict[str, Any]) -> list[dict[str, Any]]:
    filesystems = glances.get("filesystems") or []
    if filesystems:
        return filesystems
    fallback = _disks_from_storage_probe()
    if fallback:
        glances["filesystems"] = fallback
        glances["disks_source"] = "storage_probe"
    return fallback


def _history_series_1h(
    samples: list[MetricsSample],
    *,
    now: datetime,
) -> dict[str, Any]:
    cutoff = now - timedelta(hours=1)
    window = [sample for sample in samples if sample.timestamp >= cutoff]
    window.sort(key=lambda sample: sample.timestamp)
    cpu_series: list[dict[str, Any]] = []
    load_series: list[dict[str, Any]] = []
    memory_series: list[dict[str, Any]] = []
    network_series: list[dict[str, Any]] = []
    for sample in window:
        label = sample.timestamp.strftime("%H:%M")
        if sample.cpu_percent is not None:
            cpu_series.append(
                {"label": label, "value": round(sample.cpu_percent, 1)}
            )
        load_series.append({"label": label, "value": round(sample.load_1, 2)})
        if sample.memory_used_percent is not None:
            memory_series.append(
                {
                    "label": label,
                    "value": round(sample.memory_used_percent, 1),
                }
            )
        rx = sample.network_rx_bps
        tx = sample.network_tx_bps
        if rx is not None or tx is not None:
            total_bps = (rx or 0.0) + (tx or 0.0)
            network_series.append(
                {
                    "label": label,
                    "value": round(total_bps / (1024**2), 2),
                    "rx_bps": rx,
                    "tx_bps": tx,
                }
            )
    collecting = len(window) < HISTORY_MIN_CHART_POINTS
    return {
        "cpu_1h": cpu_series,
        "load_1h": load_series,
        "memory_1h": memory_series,
        "network_1h": network_series,
        "cpu_history_1h": cpu_series,
        "load_history_1h": load_series,
        "memory_history_1h": memory_series,
        "network_history_1h": network_series,
        "collecting": collecting,
        "sample_count_1h": len(window),
        "collecting_label": COLLECTING_LABEL,
    }


def _memory_section(glances: dict[str, Any]) -> dict[str, Any]:
    percent = glances.get("memory_percent")
    used = glances.get("memory_used_bytes")
    total = glances.get("memory_total_bytes")
    free = glances.get("memory_free_bytes")
    cached = glances.get("memory_cached_bytes")
    cached_percent = None
    if percent is not None and cached is not None and total:
        cached_percent = min(100.0, max(0.0, 100.0 * cached / total))
    free_percent = None
    if percent is not None and free is not None and total:
        free_percent = min(100.0, max(0.0, 100.0 * free / total))
    return {
        "percent": percent,
        "percent_display": f"{percent:.0f}%" if percent is not None else None,
        "used_display": _format_bytes(used),
        "total_display": _format_bytes(total),
        "free_display": _format_bytes(free),
        "free_percent": free_percent,
        "cached_display": _format_bytes(cached),
        "cached_percent": cached_percent,
        "summary": glances.get("memory"),
    }


def _swap_section(glances: dict[str, Any]) -> dict[str, Any]:
    percent = glances.get("swap_percent")
    used = glances.get("swap_used_bytes")
    total = glances.get("swap_total_bytes")
    free = glances.get("swap_free_bytes")
    return {
        "percent": percent,
        "percent_display": f"{percent:.0f}%" if percent is not None else None,
        "used_display": _format_bytes(used),
        "total_display": _format_bytes(total),
        "free_display": _format_bytes(free),
        "summary": glances.get("swap"),
    }


def _system_section(
    *,
    glances: dict[str, Any],
    raven: dict[str, Any],
    docker_running: int,
) -> dict[str, Any]:
    system_info = glances.get("system_info") or {}
    uptime = _format_uptime(glances.get("uptime_seconds")) or raven.get("uptime")
    hostname = system_info.get("hostname") or raven.get("hostname")
    kernel = system_info.get("kernel")
    os_name = system_info.get("os")
    if os_name and system_info.get("os_version"):
        os_name = f"{os_name} {system_info['os_version']}"
    return {
        "hostname": hostname,
        "uptime": uptime,
        "cpu_threads": glances.get("cpu_threads"),
        "containers_running": docker_running,
        "os": os_name,
        "kernel": kernel,
        "hardware": system_info.get("hardware"),
    }


def _containers_section(
    *,
    docker_running: int,
    docker_containers: list[dict[str, Any]],
) -> dict[str, Any]:
    total = len(docker_containers) if docker_containers else docker_running
    running = docker_running
    if docker_containers:
        running = sum(
            1
            for item in docker_containers
            if "run" in str(item.get("status", "")).lower()
        )
        total = len(docker_containers)
    percent = None
    if total > 0:
        percent = round(100.0 * running / total, 1)
    return {
        "running": running,
        "total": total,
        "percent": percent,
        "percent_display": f"{percent:.0f}%" if percent is not None else None,
        "items": docker_containers,
    }


def normalize_glances_details(
    glances: dict[str, Any],
    *,
    raven: dict[str, Any],
    docker_running: int,
    metrics: dict[str, Any],
    history: dict[str, Any],
    updated_at: str,
) -> dict[str, Any]:
    """Build a template-friendly Glances payload for the details page."""
    available = bool(glances.get("available"))
    status = "live" if available else "unavailable"
    cpu_breakdown = glances.get("cpu_breakdown") or {}
    top_processes = glances.get("top_processes") or glances.get("processes") or []
    overview_processes = top_processes[:5]
    disks = _resolve_disks(glances)
    containers = _containers_section(
        docker_running=docker_running,
        docker_containers=glances.get("docker_containers") or [],
    )

    return {
        "status": status,
        "updated_at": updated_at,
        "glances_available": available,
        "glances_status": glances.get("status_message"),
        "refresh_seconds": RAVEN_HEALTH_REFRESH_SECONDS,
        "overview": {
            "cpu": {
                "total_display": glances.get("cpu_now"),
                "total_percent": glances.get("cpu_total_percent"),
                "breakdown": cpu_breakdown,
                "core_count": len(glances.get("cpu_per_core") or []),
                "cpu_threads": glances.get("cpu_threads"),
            },
            "load": {
                "average_display": glances.get("load_average"),
                "load_1": glances.get("load_1"),
                "load_5": glances.get("load_5"),
                "load_15": glances.get("load_15"),
                "cpu_threads": glances.get("cpu_threads"),
            },
            "memory": _memory_section(glances),
            "swap": _swap_section(glances),
            "temperatures": glances.get("sensors") or [],
            "top_processes": overview_processes,
            "system": _system_section(
                glances=glances,
                raven=raven,
                docker_running=docker_running,
            ),
            "history": history,
            "peaks": {
                "peak_cpu_1h": metrics.get("peak_cpu_1h"),
                "peak_cpu_24h": metrics.get("peak_cpu_24h"),
                "peak_memory_24h": metrics.get("peak_memory_24h"),
                "temp_high_24h": metrics.get("temp_high_24h"),
            },
            "containers": containers,
            "disks": disks,
            "network": glances.get("network") or [],
        },
        "cpu": {
            "total_display": glances.get("cpu_now"),
            "total_percent": glances.get("cpu_total_percent"),
            "breakdown": cpu_breakdown,
            "per_core": glances.get("cpu_per_core") or [],
            "cpu_threads": glances.get("cpu_threads"),
        },
        "memory": _memory_section(glances),
        "swap": _swap_section(glances),
        "processes": glances.get("processes") or top_processes,
        "disks": disks,
        "disks_source": glances.get("disks_source"),
        "network": glances.get("network") or [],
        "sensors": glances.get("sensors") or [],
        "system": _system_section(
            glances=glances,
            raven=raven,
            docker_running=docker_running,
        ),
        "docker": glances.get("docker_containers") or [],
        "containers": containers,
        "history": history,
        "fallback": {
            "cpu_now": metrics.get("cpu_now"),
            "temp_now": metrics.get("temp_now"),
            "load_average": metrics.get("load_average"),
            "memory_live": metrics.get("memory_live"),
            "swap": metrics.get("swap"),
        },
    }


def _fallback_glances_from_metrics(metrics: dict[str, Any]) -> dict[str, Any]:
    cpu_value = metrics.get("cpu_now_value")
    temp_value = metrics.get("temp_now_celsius")
    return {
        "available": False,
        "status_message": metrics.get("glances_status") or GLANCES_UNAVAILABLE_LABEL,
        "cpu_total_percent": cpu_value,
        "cpu_now": metrics.get("cpu_now"),
        "cpu_breakdown": {},
        "cpu_per_core": metrics.get("cpu_per_core") or [],
        "load_1": None,
        "load_5": None,
        "load_15": None,
        "load_average": metrics.get("load_average"),
        "cpu_threads": metrics.get("cpu_threads"),
        "memory_percent": None,
        "memory_used_bytes": None,
        "memory_total_bytes": None,
        "memory_free_bytes": None,
        "memory_cached_bytes": None,
        "memory": metrics.get("memory_live"),
        "swap_percent": None,
        "swap_used_bytes": None,
        "swap_total_bytes": None,
        "swap_free_bytes": None,
        "swap": metrics.get("swap"),
        "cpu_temp_celsius": temp_value,
        "temp_now": metrics.get("temp_now") or NOT_AVAILABLE_LABEL,
        "sensors": (
            [
                {
                    "label": "CPU",
                    "value_celsius": temp_value,
                    "value_display": format_celsius(temp_value),
                    "is_highest": True,
                }
            ]
            if temp_value is not None
            else []
        ),
        "top_processes": metrics.get("top_processes") or [],
        "processes": metrics.get("top_processes") or [],
        "filesystems": [],
        "network": [],
        "uptime_seconds": None,
        "system_info": {},
        "docker_containers": [],
    }


def build_raven_health_details(
    *,
    raven: dict[str, Any],
    docker_running: int,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Collect Glances + fallback data for the Raven Health details page."""
    ts_now = now or datetime.now(timezone.utc)
    updated_at = ts_now.strftime("%Y-%m-%d %H:%M:%S UTC")
    metrics = get_metrics_summary(now=ts_now)

    glances: dict[str, Any]
    if glances_enabled():
        glances = fetch_glances_details_snapshot()
        if not glances.get("available"):
            fallback = _fallback_glances_from_metrics(metrics)
            for key, value in fallback.items():
                if glances.get(key) in (None, [], {}):
                    glances[key] = value
            glances["available"] = False
            glances["status_message"] = GLANCES_UNAVAILABLE_LABEL
    else:
        glances = _fallback_glances_from_metrics(metrics)
        glances["status_message"] = "Glances disabled"

    _resolve_disks(glances)

    try:
        samples = prune_samples(read_history(), now=ts_now)
    except OSError:
        samples = []
    history = _history_series_1h(samples, now=ts_now)

    return normalize_glances_details(
        glances,
        raven=raven,
        docker_running=docker_running,
        metrics=metrics,
        history=history,
        updated_at=updated_at,
    )
