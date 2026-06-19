"""Read-only Raven host CPU metrics: usage %, temperature, and thread count."""

from __future__ import annotations

import os
import time
from pathlib import Path

from host_status import HOST_PROC

HOST_SYS = Path(os.environ.get("DASHBOARD_HOST_SYS", "/host/root/sys"))

# Preferred thermal zone types for CPU/package temperature (lower = higher priority).
_THERMAL_TYPE_PRIORITY: tuple[str, ...] = (
    "x86_pkg_temp",
    "coretemp",
    "k10temp",
    "cpu",
    "acpitz",
)

NOT_AVAILABLE_LABEL = "not available"


def read_cpu_thread_count() -> int | None:
    """Count logical CPUs from host /proc/cpuinfo."""
    cpuinfo = HOST_PROC / "cpuinfo"
    if not cpuinfo.is_file():
        return None
    try:
        count = sum(
            1 for line in cpuinfo.read_text(encoding="utf-8").splitlines()
            if line.startswith("processor")
        )
        return count if count > 0 else None
    except OSError:
        return None


def read_proc_stat_jiffies() -> tuple[int, int] | None:
    """Return aggregate (total_jiffies, idle_jiffies) from host /proc/stat."""
    stat_path = HOST_PROC / "stat"
    if not stat_path.is_file():
        return None
    try:
        for line in stat_path.read_text(encoding="utf-8").splitlines():
            if not line.startswith("cpu "):
                continue
            parts = line.split()
            if len(parts) < 5:
                return None
            values = [int(x) for x in parts[1:]]
            idle = values[3] + (values[4] if len(values) > 4 else 0)
            total = sum(values[: min(len(values), 10)])
            return total, idle
    except (OSError, ValueError, IndexError):
        return None
    return None


def compute_cpu_percent(
    prev_total: int,
    prev_idle: int,
    curr_total: int,
    curr_idle: int,
) -> float | None:
    """Compute CPU utilization % between two /proc/stat readings."""
    total_delta = curr_total - prev_total
    idle_delta = curr_idle - prev_idle
    if total_delta <= 0:
        return None
    used = total_delta - idle_delta
    return max(0.0, min(100.0, 100.0 * used / total_delta))


def read_cpu_percent_live(*, interval_seconds: float = 0.1) -> float | None:
    """Sample CPU % with a short blocking interval between two /proc/stat reads."""
    first = read_proc_stat_jiffies()
    if first is None:
        return None
    time.sleep(interval_seconds)
    second = read_proc_stat_jiffies()
    if second is None:
        return None
    return compute_cpu_percent(first[0], first[1], second[0], second[1])


def read_cpu_percent_from_jiffies(
    prev_total: int,
    prev_idle: int,
) -> tuple[float | None, int | None, int | None]:
    """Compute CPU % from a prior jiffies snapshot; return (pct, total, idle)."""
    current = read_proc_stat_jiffies()
    if current is None:
        return None, None, None
    pct = compute_cpu_percent(prev_total, prev_idle, current[0], current[1])
    return pct, current[0], current[1]


def _thermal_zone_priority(zone_type: str) -> int:
    lowered = zone_type.lower()
    for index, preferred in enumerate(_THERMAL_TYPE_PRIORITY):
        if preferred in lowered:
            return index
    if any(token in lowered for token in ("cpu", "core", "pkg")):
        return len(_THERMAL_TYPE_PRIORITY)
    return 100


def read_cpu_temp_celsius() -> float | None:
    """Read CPU/package temperature from sysfs thermal zones or lm-sensors paths.

    Tries /host/root/sys/class/thermal/thermal_zone*/temp first.  Returns the
    best-match zone (x86_pkg_temp, coretemp, etc.) in degrees Celsius.
    """
    thermal_base = HOST_SYS / "class" / "thermal"
    if not thermal_base.is_dir():
        return None

    candidates: list[tuple[int, float]] = []
    for zone_dir in sorted(thermal_base.glob("thermal_zone*")):
        temp_file = zone_dir / "temp"
        if not temp_file.is_file():
            continue
        zone_type = ""
        type_file = zone_dir / "type"
        if type_file.is_file():
            try:
                zone_type = type_file.read_text(encoding="utf-8").strip()
            except OSError:
                pass
        try:
            temp_milli = int(temp_file.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            continue
        if temp_milli <= 0:
            continue
        temp_c = temp_milli / 1000.0
        if temp_c > 150.0:
            continue
        candidates.append((_thermal_zone_priority(zone_type), temp_c))

    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0])
    return candidates[0][1]


def compute_load_pressure(load_1: float, cpu_threads: int | None) -> float | None:
    """Normalize load average by CPU thread count (load pressure ratio)."""
    if cpu_threads is None or cpu_threads <= 0:
        return None
    return load_1 / cpu_threads


def format_celsius(value: float | None) -> str:
    if value is None:
        return NOT_AVAILABLE_LABEL
    return f"{value:.0f}°C"


def format_cpu_percent(value: float | None) -> str:
    if value is None:
        return NOT_AVAILABLE_LABEL
    return f"{value:.0f}%"
