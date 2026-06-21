"""Normalized Raven Health details payload for HTML and JSON responses."""

from __future__ import annotations

import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from glances_client import (
    GLANCES_UNAVAILABLE_LABEL,
    fetch_glances_details_snapshot,
    fetch_glances_version,
    glances_enabled,
)
from host_cpu_metrics import NOT_AVAILABLE_LABEL, format_celsius
from host_status import HOST_PROC
from raven_metrics_history import (
    COLLECTING_LABEL,
    MetricsSample,
    get_metrics_summary,
    prune_samples,
    read_history,
)
from storage_probe import StorageStatus, get_storage_status

_CONTAINER_HOSTNAME_RE = re.compile(r"^[a-f0-9]{12}$")
_CORE_SENSOR_RE = re.compile(r"core\s*\d|coretemp", re.IGNORECASE)
_PACKAGE_SENSOR_RE = re.compile(
    r"package|x86_pkg|pkg_temp|cpu.?package",
    re.IGNORECASE,
)

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
    if not filesystems:
        fallback = _disks_from_storage_probe()
        if fallback:
            glances["filesystems"] = fallback
            glances["disks_source"] = "storage_probe"
            filesystems = fallback
    return _prepare_disks(filesystems)


def _resolve_hostname(*, raven: dict[str, Any], glances_hostname: str | None) -> str:
    """Prefer Raven host hostname over Glances/container IDs."""
    for candidate in (raven.get("hostname"), glances_hostname):
        if not candidate:
            continue
        name = str(candidate).strip()
        if not name:
            continue
        if _CONTAINER_HOSTNAME_RE.match(name):
            continue
        return name
    return str(raven.get("hostname") or glances_hostname or "raven")


def _read_host_kernel() -> str | None:
    version = HOST_PROC / "version"
    if version.is_file():
        try:
            line = version.read_text(encoding="utf-8").splitlines()[0]
            match = re.search(r"Linux version (\S+)", line)
            if match:
                return match.group(1)
        except OSError:
            pass
    return None


def _prepare_disks(disks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Sort disks by utilization and tag storage volumes."""
    prepared: list[dict[str, Any]] = []
    for disk in disks:
        mount = str(disk.get("mount") or "")
        item = dict(disk)
        item["is_storage"] = mount.startswith("/mnt/storage")
        item["category"] = "storage" if item["is_storage"] else "system"
        prepared.append(item)
    prepared.sort(
        key=lambda row: (
            -num if (num := row.get("percent")) is not None else -1.0,
            str(row.get("mount") or ""),
        )
    )
    return prepared


def _summarize_sensors(sensors: list[dict[str, Any]]) -> dict[str, Any]:
    """Collapse noisy temperature sensors into a compact summary."""
    if not sensors:
        return {"summary": [], "raw": []}

    temps = [
        s for s in sensors if s.get("value_celsius") is not None
    ]
    if not temps:
        return {"summary": [], "raw": sensors}

    highest = max(temps, key=lambda s: float(s["value_celsius"]))
    package = next(
        (s for s in temps if _PACKAGE_SENSOR_RE.search(str(s.get("label") or ""))),
        None,
    )
    core_temps = [
        s for s in temps if _CORE_SENSOR_RE.search(str(s.get("label") or ""))
    ]
    core_max = (
        max(core_temps, key=lambda s: float(s["value_celsius"]))
        if core_temps
        else None
    )

    summary: list[dict[str, Any]] = []

    def _row(label: str, sensor: dict[str, Any] | None) -> None:
        if sensor is None:
            return
        summary.append(
            {
                "label": label,
                "source_label": sensor.get("label"),
                "value_celsius": sensor.get("value_celsius"),
                "value_display": sensor.get("value_display"),
            }
        )

    _row("CPU Package", package or highest)
    _row("Core Max", core_max)
    _row(
        "Highest System Temp",
        highest,
    )

    seen: set[str] = set()
    deduped_summary: list[dict[str, Any]] = []
    for item in summary:
        key = str(item.get("source_label") or item.get("label"))
        if key in seen:
            continue
        seen.add(key)
        deduped_summary.append(item)

    return {"summary": deduped_summary, "raw": sensors}


def _build_meta(*, updated_at: str) -> dict[str, Any]:
    return {
        "dashboard_version": "1.0",
        "build_git_commit": os.environ.get("DASHBOARD_BUILD_GIT_COMMIT", "unknown"),
        "build_timestamp": os.environ.get("DASHBOARD_BUILD_TIMESTAMP", "unknown"),
        "glances_version": fetch_glances_version() if glances_enabled() else None,
        "last_updated": updated_at,
    }


def _build_summary(
    *,
    glances: dict[str, Any],
    containers: dict[str, Any],
    metrics: dict[str, Any],
) -> dict[str, Any]:
    """Compact at-a-glance health strip for the details page header."""
    running = containers.get("running")
    total = containers.get("total")
    container_label = "—"
    if running is not None and total is not None:
        unhealthy = max(0, int(total) - int(running))
        if unhealthy == 0:
            container_label = f"{running}/{total} healthy"
        else:
            container_label = f"{running}/{total} ({unhealthy} down)"

    temp_display = glances.get("temp_now") or metrics.get("temp_now") or "—"
    load_1 = glances.get("load_1")
    return {
        "cpu_display": glances.get("cpu_now") or metrics.get("cpu_now") or "—",
        "memory_display": (
            f"{glances['memory_percent']:.0f}%"
            if glances.get("memory_percent") is not None
            else (metrics.get("memory_live") or "—")
        ),
        "temp_display": temp_display,
        "load_display": f"{load_1:.2f}" if load_1 is not None else "—",
        "containers_display": container_label,
    }


def _series_for_window(
    window: list[MetricsSample],
) -> dict[str, list[dict[str, Any]]]:
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
    return {
        "cpu": cpu_series,
        "load": load_series,
        "memory": memory_series,
        "network": network_series,
    }


def _build_history_series(
    samples: list[MetricsSample],
    *,
    now: datetime,
) -> dict[str, Any]:
    """Build 1h/6h/24h chart series from JSONL samples."""
    windows: dict[str, list[MetricsSample]] = {}
    for hours, key in ((1, "1h"), (6, "6h"), (24, "24h")):
        cutoff = now - timedelta(hours=hours)
        window = [sample for sample in samples if sample.timestamp >= cutoff]
        window.sort(key=lambda sample: sample.timestamp)
        windows[key] = window

    history: dict[str, Any] = {
        "collecting": len(windows["1h"]) < HISTORY_MIN_CHART_POINTS,
        "sample_count_1h": len(windows["1h"]),
        "collecting_label": "Collecting history — check back in a few minutes",
        "default_range": "1h",
        "ranges": ["1h", "6h", "24h"],
    }

    for range_key, window in windows.items():
        series = _series_for_window(window)
        for metric, points in series.items():
            history[f"{metric}_{range_key}"] = points
            history[f"{metric}_history_{range_key}"] = points

    return history


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
    hostname = _resolve_hostname(
        raven=raven,
        glances_hostname=system_info.get("hostname"),
    )
    kernel = system_info.get("kernel") or _read_host_kernel()
    os_name = system_info.get("os")
    if os_name and system_info.get("os_version"):
        os_name = f"{os_name} {system_info['os_version']}"
    return {
        "hostname": hostname,
        "host_label": hostname,
        "uptime": uptime,
        "cpu_threads": glances.get("cpu_threads"),
        "containers_running": docker_running,
        "os": os_name,
        "kernel": kernel,
        "hardware": system_info.get("hardware"),
        "display_lines": [
            line
            for line in (
                f"Host: {hostname}" if hostname else None,
                f"OS: {os_name}" if os_name else None,
                f"Kernel: {kernel}" if kernel else None,
            )
            if line
        ],
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
    sensors_raw = glances.get("sensors") or []
    sensors = _summarize_sensors(sensors_raw)
    processes = glances.get("processes") or top_processes

    return {
        "status": status,
        "updated_at": updated_at,
        "glances_available": available,
        "glances_status": glances.get("status_message"),
        "refresh_seconds": RAVEN_HEALTH_REFRESH_SECONDS,
        "summary": _build_summary(
            glances=glances,
            containers=containers,
            metrics=metrics,
        ),
        "meta": _build_meta(updated_at=updated_at),
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
            "temperatures": sensors.get("summary") or [],
            "temperatures_raw": sensors.get("raw") or [],
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
        "processes": processes,
        "process_count": len(processes),
        "disks": disks,
        "disks_source": glances.get("disks_source"),
        "network": glances.get("network") or [],
        "sensors": sensors.get("summary") or [],
        "sensors_raw": sensors.get("raw") or [],
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
    history = _build_history_series(samples, now=ts_now)

    return normalize_glances_details(
        glances,
        raven=raven,
        docker_running=docker_running,
        metrics=metrics,
        history=history,
        updated_at=updated_at,
    )
