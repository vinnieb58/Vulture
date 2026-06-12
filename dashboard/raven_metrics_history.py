"""Lightweight local Raven host metrics history for peak reporting.

Samples memory and load at dashboard request time, persists to a JSONL file,
prunes entries older than 24 hours, and computes 1h/24h peaks for the Nest card.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from host_status import HOST_PROC, _read_memory

HISTORY_PATH = Path(
    os.environ.get(
        "DASHBOARD_METRICS_HISTORY_PATH",
        "/app/data/raven_metrics_history.jsonl",
    )
)
RETENTION_HOURS = 24
MIN_SAMPLE_INTERVAL_SECONDS = int(
    os.environ.get("DASHBOARD_METRICS_SAMPLE_INTERVAL_SECONDS", "30")
)
COLLECTING_LABEL = "collecting data"


@dataclass(frozen=True)
class MetricsSample:
    timestamp: datetime
    load_1: float
    load_5: float
    load_15: float
    memory_used_percent: float | None
    memory_used_bytes: int | None
    memory_total_bytes: int | None

    def to_json_line(self) -> str:
        payload = {
            "timestamp": self.timestamp.isoformat(),
            "load_1": self.load_1,
            "load_5": self.load_5,
            "load_15": self.load_15,
            "memory_used_percent": self.memory_used_percent,
            "memory_used_bytes": self.memory_used_bytes,
            "memory_total_bytes": self.memory_total_bytes,
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


def collect_current_sample(now: datetime | None = None) -> MetricsSample | None:
    """Read current host load/memory; return None if load is unavailable."""
    loads = _read_load_numeric()
    if loads is None:
        return None
    pct, used_b, total_b = _read_memory_numeric()
    ts = now or datetime.now(timezone.utc)
    return MetricsSample(
        timestamp=ts,
        load_1=loads[0],
        load_5=loads[1],
        load_15=loads[2],
        memory_used_percent=pct,
        memory_used_bytes=used_b,
        memory_total_bytes=total_b,
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
    return [s for s in samples if s.timestamp >= cutoff]


def _should_append_sample(samples: list[MetricsSample], now: datetime) -> bool:
    if not samples:
        return True
    last = max(samples, key=lambda s: s.timestamp)
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
        current.sort(key=lambda s: s.timestamp)
    try:
        history_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = history_path.with_suffix(history_path.suffix + ".tmp")
        body = "\n".join(s.to_json_line() for s in current)
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


def compute_peaks(
    samples: list[MetricsSample],
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Compute 1h/24h peak memory and load for dashboard display."""
    ts_now = now or datetime.now(timezone.utc)
    samples_1h = [
        s for s in samples if s.timestamp >= ts_now - timedelta(hours=1)
    ]
    samples_24h = samples

    mem_pct_1h, mem_bytes_1h = _peak_memory(samples_1h)
    mem_pct_24h, mem_bytes_24h = _peak_memory(samples_24h)
    load_1h = _peak_load(samples_1h)
    load_24h = _peak_load(samples_24h)

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
        "peak_memory_1h": window_1h["memory"],
        "peak_memory_24h": window_24h["memory"],
        "peak_load_1h": window_1h["load"],
        "peak_load_24h": window_24h["load"],
        "sample_count": len(samples),
        "sample_count_1h": len(samples_1h),
    }


def sample_and_get_peaks(
    *,
    path: Path | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Sample current metrics (when due), persist history, return peak summary."""
    ts_now = now or datetime.now(timezone.utc)
    sample = collect_current_sample(now=ts_now)
    if sample is not None:
        samples = append_sample(sample, path=path, now=ts_now)
    else:
        samples = prune_samples(read_history(path), now=ts_now)
    return compute_peaks(samples, now=ts_now)
