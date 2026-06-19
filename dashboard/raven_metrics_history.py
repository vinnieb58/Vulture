"""Lightweight local Raven host metrics history for peak reporting.

A background sampler in the dashboard container appends samples every 60 seconds.
Page requests read the same JSONL file and compute live summaries without
writing new samples. Retention defaults to 48 hours.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from host_cpu_metrics import (
    NOT_AVAILABLE_LABEL,
    compute_cpu_percent,
    compute_load_pressure,
    format_celsius,
    format_cpu_percent,
    read_cpu_percent_from_jiffies,
    read_cpu_percent_live,
    read_cpu_temp_celsius,
    read_cpu_thread_count,
    read_proc_stat_jiffies,
)
from host_status import HOST_PROC, _read_memory

HISTORY_PATH = Path(
    os.environ.get(
        "DASHBOARD_METRICS_HISTORY_PATH",
        "/app/data/raven_metrics_history.jsonl",
    )
)
RETENTION_HOURS = int(os.environ.get("DASHBOARD_METRICS_RETENTION_HOURS", "48"))
MIN_SAMPLE_INTERVAL_SECONDS = int(
    os.environ.get("DASHBOARD_METRICS_SAMPLE_INTERVAL_SECONDS", "60")
)
CPU_SATURATION_THRESHOLD = float(
    os.environ.get("DASHBOARD_CPU_SAT_THRESHOLD", "90")
)
TEMP_WARN_CELSIUS = float(os.environ.get("DASHBOARD_TEMP_WARN_CELSIUS", "80"))
TEMP_CRITICAL_CELSIUS = float(os.environ.get("DASHBOARD_TEMP_CRITICAL_CELSIUS", "90"))
CPU_SAT_WARN_MINUTES_1H = float(
    os.environ.get("DASHBOARD_CPU_SAT_WARN_MINUTES_1H", "10")
)
CPU_SAT_CRITICAL_MINUTES_1H = float(
    os.environ.get("DASHBOARD_CPU_SAT_CRITICAL_MINUTES_1H", "30")
)
COLLECTING_LABEL = "collecting data"
LOAD_HELP_TEXT = "Load is runnable work, not CPU %. Compare load to CPU threads."


@dataclass(frozen=True)
class MetricsSample:
    timestamp: datetime
    load_1: float
    load_5: float
    load_15: float
    memory_used_percent: float | None
    memory_used_bytes: int | None
    memory_total_bytes: int | None
    cpu_percent: float | None = None
    cpu_temp_celsius: float | None = None
    cpu_total_jiffies: int | None = None
    cpu_idle_jiffies: int | None = None
    cpu_threads: int | None = None

    def to_json_line(self) -> str:
        payload = {
            "timestamp": self.timestamp.isoformat(),
            "load_1": self.load_1,
            "load_5": self.load_5,
            "load_15": self.load_15,
            "memory_used_percent": self.memory_used_percent,
            "memory_used_bytes": self.memory_used_bytes,
            "memory_total_bytes": self.memory_total_bytes,
            "cpu_percent": self.cpu_percent,
            "cpu_temp_celsius": self.cpu_temp_celsius,
            "cpu_total_jiffies": self.cpu_total_jiffies,
            "cpu_idle_jiffies": self.cpu_idle_jiffies,
            "cpu_threads": self.cpu_threads,
        }
        return json.dumps(payload, separators=(",", ":"))

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MetricsSample | None:
        try:
            ts_raw = data["timestamp"]
            if isinstance(ts_raw, (int, float)):
                ts = datetime.fromtimestamp(ts_raw, tz=timezone.utc)
            else:
                ts = datetime.fromisoformat(str(ts_raw))
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                else:
                    ts = ts.astimezone(timezone.utc)
            return cls(
                timestamp=ts,
                load_1=float(data["load_1"]),
                load_5=float(data["load_5"]),
                load_15=float(data["load_15"]),
                memory_used_percent=(
                    float(data["memory_used_percent"])
                    if data.get("memory_used_percent") is not None
                    else None
                ),
                memory_used_bytes=(
                    int(data["memory_used_bytes"])
                    if data.get("memory_used_bytes") is not None
                    else None
                ),
                memory_total_bytes=(
                    int(data["memory_total_bytes"])
                    if data.get("memory_total_bytes") is not None
                    else None
                ),
                cpu_percent=(
                    float(data["cpu_percent"])
                    if data.get("cpu_percent") is not None
                    else None
                ),
                cpu_temp_celsius=(
                    float(data["cpu_temp_celsius"])
                    if data.get("cpu_temp_celsius") is not None
                    else None
                ),
                cpu_total_jiffies=(
                    int(data["cpu_total_jiffies"])
                    if data.get("cpu_total_jiffies") is not None
                    else None
                ),
                cpu_idle_jiffies=(
                    int(data["cpu_idle_jiffies"])
                    if data.get("cpu_idle_jiffies") is not None
                    else None
                ),
                cpu_threads=(
                    int(data["cpu_threads"])
                    if data.get("cpu_threads") is not None
                    else None
                ),
            )
        except (KeyError, TypeError, ValueError):
            return None


def _read_load_numeric() -> tuple[float, float, float] | None:
    try:
        import os as _os

        one, five, fifteen = _os.getloadavg()
        return float(one), float(five), float(fifteen)
    except (AttributeError, OSError):
        pass
    loadavg = HOST_PROC / "loadavg"
    if loadavg.is_file():
        try:
            parts = loadavg.read_text(encoding="utf-8").split()
            if len(parts) >= 3:
                return float(parts[0]), float(parts[1]), float(parts[2])
        except (OSError, ValueError):
            pass
    return None


def _human_to_bytes(value: str) -> int | None:
    value = value.strip()
    if not value:
        return None
    multipliers = {
        "Ki": 1024,
        "Mi": 1024**2,
        "Gi": 1024**3,
        "Ti": 1024**4,
        "K": 1000,
        "M": 1000**2,
        "G": 1000**3,
        "T": 1000**4,
    }
    for suffix, mult in multipliers.items():
        if value.endswith(suffix):
            try:
                return int(float(value[: -len(suffix)]) * mult)
            except ValueError:
                return None
    try:
        return int(float(value))
    except ValueError:
        return None


def _read_memory_numeric() -> tuple[float | None, int | None, int | None]:
    """Return (percent_used, used_bytes, total_bytes)."""
    meminfo = HOST_PROC / "meminfo"
    if meminfo.is_file():
        try:
            data: dict[str, int] = {}
            for line in meminfo.read_text(encoding="utf-8").splitlines():
                parts = line.split(":")
                if len(parts) != 2:
                    continue
                key = parts[0].strip()
                val_parts = parts[1].strip().split()
                if val_parts:
                    data[key] = int(val_parts[0])
            total_kb = data.get("MemTotal")
            avail_kb = data.get("MemAvailable") or data.get("MemFree")
            if total_kb is not None and avail_kb is not None and total_kb > 0:
                used_kb = total_kb - avail_kb
                total_bytes = total_kb * 1024
                used_bytes = used_kb * 1024
                pct = 100.0 * used_kb / total_kb
                return pct, used_bytes, total_bytes
        except (OSError, ValueError):
            pass

    memory, _warn = _read_memory()
    if memory is None:
        return None, None, None
    total_b = _human_to_bytes(memory.total)
    used_b = _human_to_bytes(memory.used)
    return memory.percent_used, used_b, total_b


def _latest_sample(samples: list[MetricsSample]) -> MetricsSample | None:
    if not samples:
        return None
    return max(samples, key=lambda sample: sample.timestamp)


def _compute_cpu_percent_for_sample(
    previous: MetricsSample | None,
) -> tuple[float | None, int | None, int | None]:
    """Derive CPU % from the prior sample's jiffies when possible."""
    current = read_proc_stat_jiffies()
    if current is None:
        return None, None, None
    total, idle = current
    if (
        previous is not None
        and previous.cpu_total_jiffies is not None
        and previous.cpu_idle_jiffies is not None
    ):
        pct = compute_cpu_percent(
            previous.cpu_total_jiffies,
            previous.cpu_idle_jiffies,
            total,
            idle,
        )
        if pct is not None:
            return pct, total, idle
    return None, total, idle


def collect_current_sample(
    now: datetime | None = None,
    *,
    previous: MetricsSample | None = None,
) -> MetricsSample | None:
    """Read current host metrics; return None if load is unavailable."""
    loads = _read_load_numeric()
    if loads is None:
        return None
    pct_mem, used_b, total_b = _read_memory_numeric()
    cpu_pct, total_jiffies, idle_jiffies = _compute_cpu_percent_for_sample(previous)
    cpu_threads = read_cpu_thread_count()
    cpu_temp = read_cpu_temp_celsius()
    ts = now or datetime.now(timezone.utc)
    return MetricsSample(
        timestamp=ts,
        load_1=loads[0],
        load_5=loads[1],
        load_15=loads[2],
        memory_used_percent=pct_mem,
        memory_used_bytes=used_b,
        memory_total_bytes=total_b,
        cpu_percent=cpu_pct,
        cpu_temp_celsius=cpu_temp,
        cpu_total_jiffies=total_jiffies,
        cpu_idle_jiffies=idle_jiffies,
        cpu_threads=cpu_threads,
    )


def parse_history_lines(text: str) -> list[MetricsSample]:
    samples: list[MetricsSample] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(data, dict):
            continue
        sample = MetricsSample.from_dict(data)
        if sample is not None:
            samples.append(sample)
    return samples


def read_history(path: Path | None = None) -> list[MetricsSample]:
    history_path = path or HISTORY_PATH
    if not history_path.is_file():
        return []
    try:
        return parse_history_lines(history_path.read_text(encoding="utf-8"))
    except OSError:
        return []


def prune_samples(
    samples: list[MetricsSample],
    *,
    now: datetime | None = None,
    retention_hours: int = RETENTION_HOURS,
) -> list[MetricsSample]:
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(hours=retention_hours)
    return [sample for sample in samples if sample.timestamp >= cutoff]


def _should_append_sample(samples: list[MetricsSample], now: datetime) -> bool:
    if not samples:
        return True
    last = max(samples, key=lambda sample: sample.timestamp)
    return (now - last.timestamp).total_seconds() >= MIN_SAMPLE_INTERVAL_SECONDS


def append_sample(
    sample: MetricsSample,
    *,
    path: Path | None = None,
    now: datetime | None = None,
) -> list[MetricsSample]:
    """Append a sample when due, prune to retention window, persist atomically."""
    history_path = path or HISTORY_PATH
    current = prune_samples(read_history(history_path), now=now)
    ts_now = now or sample.timestamp
    if _should_append_sample(current, ts_now):
        current.append(sample)
        current.sort(key=lambda sample: sample.timestamp)
    try:
        history_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = history_path.with_suffix(history_path.suffix + ".tmp")
        body = "\n".join(sample.to_json_line() for sample in current)
        if body:
            body += "\n"
        tmp.write_text(body, encoding="utf-8")
        tmp.replace(history_path)
    except OSError:
        pass
    return current


def _format_gb(value_bytes: int | None) -> str | None:
    if value_bytes is None:
        return None
    return f"{value_bytes / (1024**3):.1f} GB"


def _format_memory_peak(
    percent: float | None,
    used_bytes: int | None,
) -> str | None:
    parts: list[str] = []
    if percent is not None:
        parts.append(f"{percent:.0f}%")
    gb = _format_gb(used_bytes)
    if gb is not None:
        parts.append(gb)
    return " / ".join(parts) if parts else None


def _peak_memory(samples: list[MetricsSample]) -> tuple[float | None, int | None]:
    peak_pct: float | None = None
    peak_bytes: int | None = None
    for sample in samples:
        if sample.memory_used_percent is not None:
            peak_pct = (
                sample.memory_used_percent
                if peak_pct is None
                else max(peak_pct, sample.memory_used_percent)
            )
        if sample.memory_used_bytes is not None:
            peak_bytes = (
                sample.memory_used_bytes
                if peak_bytes is None
                else max(peak_bytes, sample.memory_used_bytes)
            )
    return peak_pct, peak_bytes


def _peak_load(samples: list[MetricsSample]) -> float | None:
    peak: float | None = None
    for sample in samples:
        peak = sample.load_1 if peak is None else max(peak, sample.load_1)
    return peak


def _peak_cpu_percent(samples: list[MetricsSample]) -> float | None:
    peak: float | None = None
    for sample in samples:
        if sample.cpu_percent is None:
            continue
        peak = sample.cpu_percent if peak is None else max(peak, sample.cpu_percent)
    return peak


def _high_temp(samples: list[MetricsSample]) -> float | None:
    peak: float | None = None
    for sample in samples:
        if sample.cpu_temp_celsius is None:
            continue
        peak = (
            sample.cpu_temp_celsius
            if peak is None
            else max(peak, sample.cpu_temp_celsius)
        )
    return peak


def _average_temp(samples: list[MetricsSample]) -> float | None:
    temps = [
        sample.cpu_temp_celsius
        for sample in samples
        if sample.cpu_temp_celsius is not None
    ]
    if not temps:
        return None
    return sum(temps) / len(temps)


def minutes_cpu_above_threshold(
    samples: list[MetricsSample],
    *,
    threshold: float,
    window_start: datetime,
    now: datetime,
) -> float:
    """Estimate minutes above CPU threshold using sample interval weighting."""
    minutes = 0.0
    sample_minutes = MIN_SAMPLE_INTERVAL_SECONDS / 60.0
    for sample in samples:
        if sample.timestamp < window_start or sample.timestamp > now:
            continue
        if sample.cpu_percent is not None and sample.cpu_percent > threshold:
            minutes += sample_minutes
    return minutes


def _format_minutes(value: float | None) -> str:
    if value is None:
        return COLLECTING_LABEL
    if value < 1.0:
        return "<1 min"
    return f"{value:.0f} min"


def _day_start_utc(now: datetime) -> datetime:
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


def compute_metrics_summary(
    samples: list[MetricsSample],
    *,
    now: datetime | None = None,
    live_cpu_percent: float | None = None,
    live_cpu_temp: float | None = None,
    live_cpu_threads: int | None = None,
    live_load_1: float | None = None,
) -> dict[str, Any]:
    """Compute peak, saturation, and temperature metrics for dashboard display."""
    ts_now = now or datetime.now(timezone.utc)
    samples_1h = [
        sample for sample in samples if sample.timestamp >= ts_now - timedelta(hours=1)
    ]
    samples_24h = samples
    samples_today = [
        sample for sample in samples if sample.timestamp >= _day_start_utc(ts_now)
    ]

    mem_pct_1h, mem_bytes_1h = _peak_memory(samples_1h)
    mem_pct_24h, mem_bytes_24h = _peak_memory(samples_24h)
    load_1h = _peak_load(samples_1h)
    load_24h = _peak_load(samples_24h)
    cpu_peak_1h = _peak_cpu_percent(samples_1h)
    cpu_peak_24h = _peak_cpu_percent(samples_24h)
    temp_high_today = _high_temp(samples_today)
    temp_high_24h = _high_temp(samples_24h)
    temp_avg_1h = _average_temp(samples_1h)

    cpu_above_90_1h = minutes_cpu_above_threshold(
        samples,
        threshold=CPU_SATURATION_THRESHOLD,
        window_start=ts_now - timedelta(hours=1),
        now=ts_now,
    )
    cpu_above_90_24h = minutes_cpu_above_threshold(
        samples,
        threshold=CPU_SATURATION_THRESHOLD,
        window_start=ts_now - timedelta(hours=24),
        now=ts_now,
    )

    cpu_threads = live_cpu_threads
    if cpu_threads is None and samples:
        latest = _latest_sample(samples)
        if latest is not None:
            cpu_threads = latest.cpu_threads

    load_pressure = None
    if live_load_1 is not None:
        load_pressure = compute_load_pressure(live_load_1, cpu_threads)

    cpu_now = live_cpu_percent
    if cpu_now is None and samples_1h:
        latest = _latest_sample(samples_1h)
        if latest is not None:
            cpu_now = latest.cpu_percent

    temp_now = live_cpu_temp
    if temp_now is None and samples_1h:
        latest = _latest_sample(samples_1h)
        if latest is not None:
            temp_now = latest.cpu_temp_celsius

    def _line(
        mem_pct: float | None,
        mem_bytes: int | None,
        load: float | None,
        *,
        has_samples: bool,
    ) -> dict[str, str | None]:
        if not has_samples:
            return {
                "memory": COLLECTING_LABEL,
                "load": COLLECTING_LABEL,
            }
        return {
            "memory": _format_memory_peak(mem_pct, mem_bytes) or COLLECTING_LABEL,
            "load": f"{load:.2f}" if load is not None else COLLECTING_LABEL,
        }

    window_1h = _line(mem_pct_1h, mem_bytes_1h, load_1h, has_samples=bool(samples_1h))
    window_24h = _line(mem_pct_24h, mem_bytes_24h, load_24h, has_samples=bool(samples_24h))

    return {
        "cpu_now": format_cpu_percent(cpu_now) if cpu_now is not None else COLLECTING_LABEL,
        "cpu_now_value": cpu_now,
        "cpu_above_90_minutes_1h": _format_minutes(cpu_above_90_1h if samples_1h else None),
        "cpu_above_90_minutes_1h_raw": cpu_above_90_1h if samples_1h else None,
        "cpu_above_90_minutes_24h": _format_minutes(cpu_above_90_24h if samples_24h else None),
        "cpu_above_90_minutes_24h_raw": cpu_above_90_24h if samples_24h else None,
        "temp_now": format_celsius(temp_now) if temp_now is not None else NOT_AVAILABLE_LABEL,
        "temp_now_celsius": temp_now,
        "temp_high_today": (
            format_celsius(temp_high_today)
            if temp_high_today is not None
            else (NOT_AVAILABLE_LABEL if not samples_today else COLLECTING_LABEL)
        ),
        "temp_high_today_celsius": temp_high_today,
        "temp_avg_1h": (
            format_celsius(temp_avg_1h)
            if temp_avg_1h is not None
            else (COLLECTING_LABEL if samples_1h else NOT_AVAILABLE_LABEL)
        ),
        "temp_high_24h": (
            format_celsius(temp_high_24h)
            if temp_high_24h is not None
            else (COLLECTING_LABEL if samples_24h else NOT_AVAILABLE_LABEL)
        ),
        "peak_cpu_1h": (
            format_cpu_percent(cpu_peak_1h)
            if cpu_peak_1h is not None
            else COLLECTING_LABEL
        ),
        "peak_cpu_24h": (
            format_cpu_percent(cpu_peak_24h)
            if cpu_peak_24h is not None
            else COLLECTING_LABEL
        ),
        "peak_memory_1h": window_1h["memory"],
        "peak_memory_24h": window_24h["memory"],
        "peak_load_avg_1h": window_1h["load"],
        "peak_load_avg_24h": window_24h["load"],
        "peak_load_1h": window_1h["load"],
        "peak_load_24h": window_24h["load"],
        "load_pressure": (
            f"{load_pressure:.2f}" if load_pressure is not None else COLLECTING_LABEL
        ),
        "load_pressure_value": load_pressure,
        "cpu_threads": cpu_threads,
        "load_help": LOAD_HELP_TEXT,
        "sample_count": len(samples),
        "sample_count_1h": len(samples_1h),
        "thresholds": {
            "temp_warn_celsius": TEMP_WARN_CELSIUS,
            "temp_critical_celsius": TEMP_CRITICAL_CELSIUS,
            "cpu_sat_threshold": CPU_SATURATION_THRESHOLD,
            "cpu_sat_warn_minutes_1h": CPU_SAT_WARN_MINUTES_1H,
            "cpu_sat_critical_minutes_1h": CPU_SAT_CRITICAL_MINUTES_1H,
        },
    }


def compute_peaks(
    samples: list[MetricsSample],
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Backward-compatible alias for peak-only callers."""
    return compute_metrics_summary(samples, now=now)


def _collect_live_readings(
    *,
    previous: MetricsSample | None = None,
) -> dict[str, float | int | None]:
    """Read current host CPU/load values without persisting a sample."""
    live_cpu_percent: float | None = None
    if (
        previous is not None
        and previous.cpu_total_jiffies is not None
        and previous.cpu_idle_jiffies is not None
    ):
        live_cpu_percent, _, _ = read_cpu_percent_from_jiffies(
            previous.cpu_total_jiffies,
            previous.cpu_idle_jiffies,
        )
    if live_cpu_percent is None:
        live_cpu_percent = read_cpu_percent_live()

    loads = _read_load_numeric()
    return {
        "live_cpu_percent": live_cpu_percent,
        "live_cpu_temp": read_cpu_temp_celsius(),
        "live_cpu_threads": read_cpu_thread_count(),
        "live_load_1": loads[0] if loads else None,
    }


def record_sample_if_due(
    *,
    path: Path | None = None,
    now: datetime | None = None,
) -> bool:
    """Collect and append one sample when the minimum interval has elapsed."""
    ts_now = now or datetime.now(timezone.utc)
    history = prune_samples(read_history(path), now=ts_now)
    if not _should_append_sample(history, ts_now):
        return False
    previous = _latest_sample(history)
    sample = collect_current_sample(now=ts_now, previous=previous)
    if sample is None:
        return False
    append_sample(sample, path=path, now=ts_now)
    return True


def get_metrics_summary(
    *,
    path: Path | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Read history and compute dashboard metrics without appending a sample."""
    ts_now = now or datetime.now(timezone.utc)
    samples = prune_samples(read_history(path), now=ts_now)
    previous = _latest_sample(samples)
    live = _collect_live_readings(previous=previous)
    return compute_metrics_summary(
        samples,
        now=ts_now,
        live_cpu_percent=live["live_cpu_percent"],
        live_cpu_temp=live["live_cpu_temp"],
        live_cpu_threads=live["live_cpu_threads"],
        live_load_1=live["live_load_1"],
    )


def sample_and_get_peaks(
    *,
    path: Path | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Backward-compatible helper: record when due, then return summary."""
    record_sample_if_due(path=path, now=now)
    return get_metrics_summary(path=path, now=now)
