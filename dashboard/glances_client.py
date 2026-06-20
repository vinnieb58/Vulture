"""Read-only Glances REST API client for Raven host telemetry."""

from __future__ import annotations

import json
import logging
import os
import re
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from host_cpu_metrics import NOT_AVAILABLE_LABEL, format_celsius, format_cpu_percent

logger = logging.getLogger(__name__)

GLANCES_BASE_URL = os.environ.get(
    "DASHBOARD_GLANCES_URL", "http://glances:61208"
).rstrip("/")
GLANCES_UNAVAILABLE_LABEL = "Glances unavailable"
TOP_PROCESS_LIMIT = int(os.environ.get("DASHBOARD_GLANCES_TOP_PROCESSES", "5"))

GLANCES_ENDPOINTS: tuple[str, ...] = (
    "/api/4/cpu",
    "/api/4/load",
    "/api/4/mem",
    "/api/4/memswap",
    "/api/4/sensors",
    "/api/4/percpu",
    "/api/4/processlist",
)

GLANCES_DETAILS_EXTRA_ENDPOINTS: tuple[str, ...] = (
    "/api/4/fs",
    "/api/4/network",
    "/api/4/uptime",
    "/api/4/system",
    "/api/4/docker",
)

GLANCES_DETAILS_ENDPOINTS: tuple[str, ...] = (
    GLANCES_ENDPOINTS + GLANCES_DETAILS_EXTRA_ENDPOINTS
)

# Preferred CPU/package temperature labels from Glances sensors (lower = higher priority).
_TEMP_LABEL_PRIORITY: tuple[str, ...] = (
    "x86_pkg_temp",
    "package id 0",
    "package id",
    "cpu",
    "core 0",
    "coretemp",
    "k10temp",
    "acpitz",
)
_TEMP_LABEL_RE = re.compile(
    r"(pkg|package|coretemp|k10temp|x86_pkg|cpu|acpitz)",
    re.IGNORECASE,
)


def glances_enabled() -> bool:
    return os.environ.get("DASHBOARD_USE_GLANCES", "false").lower() not in (
        "0",
        "false",
        "no",
    )


def _request_timeout_seconds() -> float:
    """Per-request socket timeout; read at call time for testability."""
    return float(
        os.environ.get(
            "DASHBOARD_GLANCES_REQUEST_TIMEOUT_SECONDS",
            os.environ.get("DASHBOARD_GLANCES_TIMEOUT_SECONDS", "1.0"),
        )
    )


def _fetch_budget_seconds() -> float:
    """Shared snapshot budget; read at call time for testability."""
    return float(os.environ.get("DASHBOARD_GLANCES_FETCH_BUDGET_SECONDS", "1.5"))


def _fetch_json(path: str, *, timeout: float | None = None) -> Any | None:
    url = f"{GLANCES_BASE_URL}{path}"
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    request_timeout = (
        timeout if timeout is not None else _request_timeout_seconds()
    )
    try:
        with urllib.request.urlopen(request, timeout=request_timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError, ValueError) as exc:
        logger.debug("Glances request failed for %s: %s", path, exc)
        return None


def _fetch_json_with_budget(path: str, deadline: float) -> tuple[str, Any | None]:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        logger.debug("Glances budget exhausted before %s", path)
        return path, None
    timeout = min(_request_timeout_seconds(), remaining)
    return path, _fetch_json(path, timeout=timeout)


def _fetch_all_json(paths: tuple[str, ...] | None = None) -> dict[str, Any | None]:
    """Fetch Glances endpoints in parallel within a shared time budget."""
    endpoints = paths or GLANCES_ENDPOINTS
    budget = max(0.05, _fetch_budget_seconds())
    deadline = time.monotonic() + budget
    results: dict[str, Any | None] = {path: None for path in endpoints}
    if not endpoints:
        return results

    executor = ThreadPoolExecutor(max_workers=len(endpoints))
    futures: list = []
    try:
        futures = [
            executor.submit(_fetch_json_with_budget, path, deadline)
            for path in endpoints
        ]
        try:
            for future in as_completed(
                futures,
                timeout=max(0.01, deadline - time.monotonic()),
            ):
                try:
                    path, data = future.result()
                    results[path] = data
                except Exception:
                    logger.debug("Glances parallel fetch task failed", exc_info=True)
        except TimeoutError:
            logger.info(
                "Glances fetch budget exceeded (%.1fs); using partial results / fallback",
                budget,
            )
        for future in futures:
            if not future.done():
                future.cancel()
    finally:
        # Do not block the dashboard page on slow Glances workers.
        executor.shutdown(wait=False, cancel_futures=True)
    return results


def _coerce_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _coerce_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _format_bytes(value_bytes: int | None) -> str | None:
    if value_bytes is None:
        return None
    return f"{value_bytes / (1024**3):.1f} GB"


def _format_memory_line(
    percent: float | None,
    used_bytes: int | None,
    total_bytes: int | None,
) -> str | None:
    parts: list[str] = []
    if percent is not None:
        parts.append(f"{percent:.0f}%")
    used = _format_bytes(used_bytes)
    total = _format_bytes(total_bytes)
    if used and total:
        parts.append(f"{used} / {total}")
    elif used:
        parts.append(used)
    return " · ".join(parts) if parts else None


def _format_swap_line(
    percent: float | None,
    used_bytes: int | None,
    total_bytes: int | None,
) -> str | None:
    if total_bytes is not None and total_bytes <= 0:
        return None
    parts: list[str] = []
    if percent is not None:
        parts.append(f"{percent:.0f}%")
    used = _format_bytes(used_bytes)
    total = _format_bytes(total_bytes)
    if used and total:
        parts.append(f"{used} / {total}")
    elif used:
        parts.append(used)
    return " · ".join(parts) if parts else None


def _parse_cpu(data: dict[str, Any] | None) -> tuple[float | None, int | None]:
    if not isinstance(data, dict):
        return None, None
    total = _coerce_float(data.get("total"))
    if total is None and data.get("idle") is not None:
        idle = _coerce_float(data.get("idle"))
        if idle is not None:
            total = max(0.0, 100.0 - idle)
    return total, _coerce_int(data.get("cpucore"))


def _parse_percpu(data: list[Any] | None) -> list[dict[str, Any]]:
    if not isinstance(data, list):
        return []
    cores: list[dict[str, Any]] = []
    for entry in data:
        if not isinstance(entry, dict):
            continue
        core_number = _coerce_int(entry.get("cpu_number"))
        total = _coerce_float(entry.get("total"))
        if total is None and entry.get("idle") is not None:
            idle = _coerce_float(entry.get("idle"))
            if idle is not None:
                total = max(0.0, 100.0 - idle)
        if core_number is None or total is None:
            continue
        cores.append(
            {
                "core": core_number,
                "cpu_percent": total,
                "cpu_percent_display": format_cpu_percent(total),
            }
        )
    cores.sort(key=lambda item: item["core"])
    return cores


def _parse_load(data: dict[str, Any] | None) -> tuple[float | None, float | None, float | None, int | None]:
    if not isinstance(data, dict):
        return None, None, None, None
    return (
        _coerce_float(data.get("min1")),
        _coerce_float(data.get("min5")),
        _coerce_float(data.get("min15")),
        _coerce_int(data.get("cpucore")),
    )


def _parse_mem(data: dict[str, Any] | None) -> tuple[float | None, int | None, int | None]:
    if not isinstance(data, dict):
        return None, None, None
    total = _coerce_int(data.get("total"))
    used = None
    if total is not None:
        available = _coerce_int(data.get("available"))
        free = _coerce_int(data.get("free"))
        if available is not None:
            used = max(0, total - available)
        elif free is not None:
            used = max(0, total - free)
    return _coerce_float(data.get("percent")), used, total


def _parse_memswap(data: dict[str, Any] | None) -> tuple[float | None, int | None, int | None]:
    if not isinstance(data, dict):
        return None, None, None
    return (
        _coerce_float(data.get("percent")),
        _coerce_int(data.get("used")),
        _coerce_int(data.get("total")),
    )


def _temp_label_rank(label: str) -> int:
    normalized = label.strip().lower()
    for index, preferred in enumerate(_TEMP_LABEL_PRIORITY):
        if preferred in normalized:
            return index
    if _TEMP_LABEL_RE.search(normalized):
        return len(_TEMP_LABEL_PRIORITY)
    return len(_TEMP_LABEL_PRIORITY) + 1


def _parse_cpu_temp(data: list[Any] | None) -> float | None:
    if not isinstance(data, list):
        return None
    candidates: list[tuple[int, float]] = []
    for entry in data:
        if not isinstance(entry, dict):
            continue
        if entry.get("type") != "temperature_core":
            continue
        label = str(entry.get("label") or "")
        value = _coerce_float(entry.get("value"))
        if value is None:
            continue
        candidates.append((_temp_label_rank(label), value))
    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0])
    return candidates[0][1]


def _parse_process_entry(entry: dict[str, Any]) -> dict[str, Any] | None:
    cpu_percent = _coerce_float(entry.get("cpu_percent"))
    name = str(entry.get("name") or entry.get("username") or "unknown")
    if cpu_percent is None:
        return None
    mem_percent = _coerce_float(entry.get("memory_percent"))
    parsed: dict[str, Any] = {
        "name": name,
        "cpu_percent": cpu_percent,
        "cpu_percent_display": format_cpu_percent(cpu_percent),
    }
    if mem_percent is not None:
        parsed["memory_percent"] = mem_percent
        parsed["memory_percent_display"] = f"{mem_percent:.1f}%"
    pid = _coerce_int(entry.get("pid"))
    if pid is not None:
        parsed["pid"] = pid
    return parsed


def _parse_top_processes(
    data: list[Any] | None,
    *,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    if not isinstance(data, list):
        return []
    processes: list[dict[str, Any]] = []
    for entry in data:
        if not isinstance(entry, dict):
            continue
        parsed = _parse_process_entry(entry)
        if parsed is not None:
            processes.append(parsed)
    processes.sort(key=lambda item: item["cpu_percent"], reverse=True)
    max_items = limit if limit is not None else max(1, TOP_PROCESS_LIMIT)
    return processes[:max_items]


def _parse_cpu_breakdown(data: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(data, dict):
        return {}
    breakdown: dict[str, Any] = {}
    for key in ("user", "system", "nice", "idle", "iowait", "irq", "softirq"):
        value = _coerce_float(data.get(key))
        if value is not None:
            breakdown[key] = value
            breakdown[f"{key}_display"] = format_cpu_percent(value)
    return breakdown


def _parse_all_sensors(data: list[Any] | None) -> list[dict[str, Any]]:
    if not isinstance(data, list):
        return []
    sensors: list[dict[str, Any]] = []
    for entry in data:
        if not isinstance(entry, dict):
            continue
        sensor_type = str(entry.get("type") or "")
        if sensor_type != "temperature_core":
            continue
        label = str(entry.get("label") or "sensor")
        value = _coerce_float(entry.get("value"))
        if value is None:
            continue
        sensors.append(
            {
                "label": label,
                "value_celsius": value,
                "value_display": format_celsius(value),
                "priority": _temp_label_rank(label),
            }
        )
    sensors.sort(key=lambda item: (item["priority"], -item["value_celsius"]))
    if sensors:
        highest_value = max(item["value_celsius"] for item in sensors)
        for item in sensors:
            item["is_highest"] = item["value_celsius"] == highest_value
    return sensors


def _parse_filesystems(data: list[Any] | None) -> list[dict[str, Any]]:
    if not isinstance(data, list):
        return []
    filesystems: list[dict[str, Any]] = []
    for entry in data:
        if not isinstance(entry, dict):
            continue
        mount = str(entry.get("mnt_point") or entry.get("mountpoint") or "")
        device = str(entry.get("device_name") or entry.get("device") or "")
        percent = _coerce_float(entry.get("percent"))
        used_bytes = _coerce_int(entry.get("used"))
        total_bytes = _coerce_int(entry.get("size") or entry.get("total"))
        free_bytes = _coerce_int(entry.get("free"))
        if not mount and not device:
            continue
        filesystems.append(
            {
                "device": device or "—",
                "mount": mount or "—",
                "percent": percent,
                "percent_display": f"{percent:.0f}%" if percent is not None else None,
                "used_display": _format_bytes(used_bytes),
                "total_display": _format_bytes(total_bytes),
                "free_display": _format_bytes(free_bytes),
            }
        )
    return filesystems


def _parse_network_interfaces(data: list[Any] | None) -> list[dict[str, Any]]:
    if not isinstance(data, list):
        return []
    interfaces: list[dict[str, Any]] = []
    for entry in data:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("interface_name") or entry.get("name") or "")
        if not name:
            continue
        recv_bytes = _coerce_int(entry.get("bytes_recv"))
        sent_bytes = _coerce_int(entry.get("bytes_sent"))
        speed = _coerce_int(entry.get("speed"))
        interfaces.append(
            {
                "name": name,
                "bytes_recv_display": _format_bytes(recv_bytes),
                "bytes_sent_display": _format_bytes(sent_bytes),
                "speed_mbps": speed,
            }
        )
    return interfaces


def _parse_uptime(data: Any | None) -> float | None:
    if isinstance(data, dict):
        return _coerce_float(data.get("seconds") or data.get("uptime"))
    return _coerce_float(data)


def _parse_system_info(data: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(data, dict):
        return {}
    info: dict[str, Any] = {}
    for key, target in (
        ("hostname", "hostname"),
        ("hr_name", "hardware"),
        ("linux_distro", "os"),
        ("os_version", "os_version"),
        ("platform", "platform"),
        ("kernel_version", "kernel"),
    ):
        value = data.get(key)
        if value:
            info[target] = str(value)
    return info


def _parse_docker_containers(data: list[Any] | None) -> list[dict[str, Any]]:
    if not isinstance(data, list):
        return []
    containers: list[dict[str, Any]] = []
    for entry in data:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name") or entry.get("Names") or "")
        status = str(entry.get("status") or entry.get("State") or "")
        if not name:
            continue
        containers.append({"name": name, "status": status or "unknown"})
    return containers


def _build_glances_snapshot(payload: dict[str, Any | None]) -> dict[str, Any]:
    cpu_data = payload.get("/api/4/cpu")
    load_data = payload.get("/api/4/load")
    mem_data = payload.get("/api/4/mem")
    swap_data = payload.get("/api/4/memswap")
    sensors_data = payload.get("/api/4/sensors")
    percpu_data = payload.get("/api/4/percpu")
    process_data = payload.get("/api/4/processlist")

    cpu_total, cpu_threads = _parse_cpu(cpu_data if isinstance(cpu_data, dict) else None)
    load_1, load_5, load_15, load_threads = _parse_load(
        load_data if isinstance(load_data, dict) else None
    )
    mem_percent, mem_used, mem_total = _parse_mem(mem_data if isinstance(mem_data, dict) else None)
    swap_percent, swap_used, swap_total = _parse_memswap(
        swap_data if isinstance(swap_data, dict) else None
    )
    cpu_temp = _parse_cpu_temp(sensors_data if isinstance(sensors_data, list) else None)
    per_core = _parse_percpu(percpu_data if isinstance(percpu_data, list) else None)
    top_processes = _parse_top_processes(process_data if isinstance(process_data, list) else None)

    available = cpu_total is not None or load_1 is not None or mem_percent is not None
    if cpu_threads is None:
        cpu_threads = load_threads

    load_average = None
    if load_1 is not None and load_5 is not None and load_15 is not None:
        load_average = f"{load_1:.2f} / {load_5:.2f} / {load_15:.2f}"

    per_core_summary = None
    if per_core:
        per_core_summary = ", ".join(
            f"C{item['core']} {item['cpu_percent_display']}" for item in per_core
        )

    top_processes_summary = None
    if top_processes:
        top_processes_summary = ", ".join(
            f"{item['name']} {item['cpu_percent_display']}" for item in top_processes
        )

    mem_free = None
    mem_cached = None
    if isinstance(mem_data, dict):
        mem_free = _coerce_int(mem_data.get("free"))
        mem_cached = _coerce_int(mem_data.get("cached"))

    return {
        "available": available,
        "status_message": None if available else GLANCES_UNAVAILABLE_LABEL,
        "cpu_total_percent": cpu_total,
        "cpu_now": format_cpu_percent(cpu_total) if cpu_total is not None else None,
        "cpu_breakdown": _parse_cpu_breakdown(cpu_data if isinstance(cpu_data, dict) else None),
        "cpu_per_core": per_core,
        "cpu_per_core_summary": per_core_summary,
        "load_1": load_1,
        "load_5": load_5,
        "load_15": load_15,
        "load_average": load_average,
        "cpu_threads": cpu_threads,
        "memory_percent": mem_percent,
        "memory_used_bytes": mem_used,
        "memory_total_bytes": mem_total,
        "memory_free_bytes": mem_free,
        "memory_cached_bytes": mem_cached,
        "memory": _format_memory_line(mem_percent, mem_used, mem_total),
        "swap_percent": swap_percent,
        "swap_used_bytes": swap_used,
        "swap_total_bytes": swap_total,
        "swap_free_bytes": (
            max(0, swap_total - swap_used)
            if swap_total is not None and swap_used is not None
            else None
        ),
        "swap": _format_swap_line(swap_percent, swap_used, swap_total),
        "cpu_temp_celsius": cpu_temp,
        "temp_now": format_celsius(cpu_temp) if cpu_temp is not None else NOT_AVAILABLE_LABEL,
        "sensors": _parse_all_sensors(sensors_data if isinstance(sensors_data, list) else None),
        "top_processes": top_processes,
        "top_processes_summary": top_processes_summary,
        "processes": _parse_top_processes(
            process_data if isinstance(process_data, list) else None,
            limit=20,
        ),
        "filesystems": _parse_filesystems(payload.get("/api/4/fs")),
        "network": _parse_network_interfaces(payload.get("/api/4/network")),
        "uptime_seconds": _parse_uptime(payload.get("/api/4/uptime")),
        "system_info": _parse_system_info(
            payload.get("/api/4/system") if isinstance(payload.get("/api/4/system"), dict) else None
        ),
        "docker_containers": _parse_docker_containers(payload.get("/api/4/docker")),
    }


def fetch_glances_snapshot() -> dict[str, Any]:
    """Fetch live host metrics from Glances API v4 within a shared time budget."""
    started = time.monotonic()
    payload = _fetch_all_json()
    elapsed = time.monotonic() - started
    if elapsed > _fetch_budget_seconds():
        logger.info(
            "Glances snapshot took %.2fs (budget %.1fs)",
            elapsed,
            _fetch_budget_seconds(),
        )

    snapshot = _build_glances_snapshot(payload)
    elapsed = time.monotonic() - started
    if elapsed > _fetch_budget_seconds():
        logger.info(
            "Glances snapshot took %.2fs (budget %.1fs)",
            elapsed,
            _fetch_budget_seconds(),
        )
    return snapshot


def fetch_glances_details_snapshot() -> dict[str, Any]:
    """Fetch extended Glances telemetry for the Raven Health details page."""
    started = time.monotonic()
    payload = _fetch_all_json(GLANCES_DETAILS_ENDPOINTS)
    snapshot = _build_glances_snapshot(payload)
    elapsed = time.monotonic() - started
    if elapsed > _fetch_budget_seconds():
        logger.info(
            "Glances details snapshot took %.2fs (budget %.1fs)",
            elapsed,
            _fetch_budget_seconds(),
        )
    return snapshot
