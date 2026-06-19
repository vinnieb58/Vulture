"""Lightweight local Raven host metrics history for peak reporting.

CPU is sampled every 5 seconds in a background thread. Raw readings accumulate
in memory and roll up into 60-second buckets persisted to JSONL. Page requests
read buckets only. Retention defaults to 48 hours.
"""

from __future__ import annotations

import json
import logging
import os
import threading
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

logger = logging.getLogger(__name__)
_history_io_lock = threading.Lock()

HISTORY_PATH = Path(
    os.environ.get(
        "DASHBOARD_METRICS_HISTORY_PATH",
        "/app/data/raven_metrics_history.jsonl",
    )
)
RETENTION_HOURS = int(os.environ.get("DASHBOARD_METRICS_RETENTION_HOURS", "48"))
RAW_SAMPLE_INTERVAL_SECONDS = int(
    os.environ.get("DASHBOARD_METRICS_RAW_SAMPLE_INTERVAL_SECONDS", "5")
)
BUCKET_SECONDS = int(os.environ.get("DASHBOARD_METRICS_BUCKET_SECONDS", "60"))
# Backward-compatible alias used by older tests/config.
MIN_SAMPLE_INTERVAL_SECONDS = BUCKET_SECONDS
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
BUCKET_FORMAT_VERSION = 2


def _parse_timestamp(ts_raw: Any) -> datetime | None:
    try:
        if isinstance(ts_raw, (int, float)):
            return datetime.fromtimestamp(ts_raw, tz=timezone.utc)
        ts = datetime.fromisoformat(str(ts_raw))
        if ts.tzinfo is None:
            return ts.replace(tzinfo=timezone.utc)
        return ts.astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def _floor_to_bucket(ts: datetime, bucket_seconds: int) -> datetime:
    epoch = int(ts.timestamp())
    floored = epoch - (epoch % bucket_seconds)
    return datetime.fromtimestamp(floored, tz=timezone.utc)


@dataclass(frozen=True)
class RawSample:
    """In-memory 5-second host reading (not persisted)."""

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


@dataclass(frozen=True)
class MetricsBucket:
    """Persisted 60-second rollup bucket."""

    timestamp: datetime
    cpu_avg_percent: float | None
    cpu_peak_percent: float | None
    cpu_samples_count: int
    cpu_seconds_over_90: float
    temp_avg_celsius: float | None
    temp_peak_celsius: float | None
    memory_used_percent: float | None
    memory_used_bytes: int | None
    memory_total_bytes: int | None
    load_1: float
    load_5: float
    load_15: float
    cpu_threads: int | None = None
    format_version: int = BUCKET_FORMAT_VERSION
    bucket_seconds: int = BUCKET_SECONDS

    def to_json_line(self) -> str:
        payload = {
            "format_version": self.format_version,
            "timestamp": self.timestamp.isoformat(),
            "bucket_seconds": self.bucket_seconds,
            "cpu_avg_percent": self.cpu_avg_percent,
            "cpu_peak_percent": self.cpu_peak_percent,
            "cpu_samples_count": self.cpu_samples_count,
            "cpu_seconds_over_90": self.cpu_seconds_over_90,
            "temp_avg_celsius": self.temp_avg_celsius,
            "temp_peak_celsius": self.temp_peak_celsius,
            "memory_used_percent": self.memory_used_percent,
            "memory_used_bytes": self.memory_used_bytes,
            "memory_total_bytes": self.memory_total_bytes,
            "load_1": self.load_1,
            "load_5": self.load_5,
            "load_15": self.load_15,
            "cpu_threads": self.cpu_threads,
        }
        return json.dumps(payload, separators=(",", ":"))

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MetricsBucket | None:
        try:
            ts = _parse_timestamp(data["timestamp"])
            if ts is None:
                return None
            return cls(
                timestamp=ts,
                cpu_avg_percent=(
                    float(data["cpu_avg_percent"])
                    if data.get("cpu_avg_percent") is not None
                    else None
                ),
                cpu_peak_percent=(
                    float(data["cpu_peak_percent"])
                    if data.get("cpu_peak_percent") is not None
                    else None
                ),
                cpu_samples_count=int(data.get("cpu_samples_count", 0)),
                cpu_seconds_over_90=float(data.get("cpu_seconds_over_90", 0.0)),
                temp_avg_celsius=(
                    float(data["temp_avg_celsius"])
                    if data.get("temp_avg_celsius") is not None
                    else None
                ),
                temp_peak_celsius=(
                    float(data["temp_peak_celsius"])
                    if data.get("temp_peak_celsius") is not None
                    else None
                ),
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
                load_1=float(data["load_1"]),
                load_5=float(data["load_5"]),
                load_15=float(data["load_15"]),
                cpu_threads=(
                    int(data["cpu_threads"])
                    if data.get("cpu_threads") is not None
                    else None
                ),
                format_version=int(data.get("format_version", BUCKET_FORMAT_VERSION)),
                bucket_seconds=int(data.get("bucket_seconds", BUCKET_SECONDS)),
            )
        except (KeyError, TypeError, ValueError):
            return None


@dataclass(frozen=True)
class MetricsSample:
    """Legacy v1 persisted sample (read-only for migration)."""

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

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MetricsSample | None:
        try:
            ts = _parse_timestamp(data["timestamp"])
            if ts is None:
                return None
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


def legacy_sample_to_bucket(
    sample: MetricsSample,
    *,
    legacy_interval_seconds: int = 60,
) -> MetricsBucket:
    """Convert a legacy v1 point sample into a synthetic 60s bucket."""
    cpu_seconds_over_90 = 0.0
    if (
        sample.cpu_percent is not None
        and sample.cpu_percent > CPU_SATURATION_THRESHOLD
    ):
        cpu_seconds_over_90 = float(legacy_interval_seconds)
    return MetricsBucket(
        timestamp=_floor_to_bucket(sample.timestamp, BUCKET_SECONDS),
        cpu_avg_percent=sample.cpu_percent,
        cpu_peak_percent=sample.cpu_percent,
        cpu_samples_count=1,
        cpu_seconds_over_90=cpu_seconds_over_90,
        temp_avg_celsius=sample.cpu_temp_celsius,
        temp_peak_celsius=sample.cpu_temp_celsius,
        memory_used_percent=sample.memory_used_percent,
        memory_used_bytes=sample.memory_used_bytes,
        memory_total_bytes=sample.memory_total_bytes,
        load_1=sample.load_1,
        load_5=sample.load_5,
        load_15=sample.load_15,
        cpu_threads=sample.cpu_threads,
    )


def _finalize_bucket(
    bucket_start: datetime,
    raw_samples: list[RawSample],
    *,
    raw_interval_seconds: int = RAW_SAMPLE_INTERVAL_SECONDS,
) -> MetricsBucket | None:
    if not raw_samples:
        return None
    cpu_vals = [sample.cpu_percent for sample in raw_samples if sample.cpu_percent is not None]
    temp_vals = [
        sample.cpu_temp_celsius
        for sample in raw_samples
        if sample.cpu_temp_celsius is not None
    ]
    cpu_seconds_over_90 = 0.0
    for sample in raw_samples:
        if (
            sample.cpu_percent is not None
            and sample.cpu_percent > CPU_SATURATION_THRESHOLD
        ):
            cpu_seconds_over_90 += raw_interval_seconds
    last = raw_samples[-1]
    return MetricsBucket(
        timestamp=bucket_start,
        cpu_avg_percent=(sum(cpu_vals) / len(cpu_vals)) if cpu_vals else None,
        cpu_peak_percent=max(cpu_vals) if cpu_vals else None,
        cpu_samples_count=len(raw_samples),
        cpu_seconds_over_90=cpu_seconds_over_90,
        temp_avg_celsius=(sum(temp_vals) / len(temp_vals)) if temp_vals else None,
        temp_peak_celsius=max(temp_vals) if temp_vals else None,
        memory_used_percent=last.memory_used_percent,
        memory_used_bytes=last.memory_used_bytes,
        memory_total_bytes=last.memory_total_bytes,
        load_1=last.load_1,
        load_5=last.load_5,
        load_15=last.load_15,
        cpu_threads=last.cpu_threads,
    )


class BucketAccumulator:
    """Thread-safe in-memory accumulator for raw samples within a bucket."""

    def __init__(
        self,
        *,
        bucket_seconds: int = BUCKET_SECONDS,
        raw_interval_seconds: int = RAW_SAMPLE_INTERVAL_SECONDS,
    ) -> None:
        self.bucket_seconds = bucket_seconds
        self.raw_interval_seconds = raw_interval_seconds
        self._lock = threading.Lock()
        self._current_bucket_start: datetime | None = None
        self._raw_samples: list[RawSample] = []

    def last_raw_sample(self) -> RawSample | None:
        with self._lock:
            return self._raw_samples[-1] if self._raw_samples else None

    def add(self, sample: RawSample) -> MetricsBucket | None:
        with self._lock:
            bucket_start = _floor_to_bucket(sample.timestamp, self.bucket_seconds)
            completed: MetricsBucket | None = None
            if (
                self._current_bucket_start is not None
                and bucket_start > self._current_bucket_start
            ):
                completed = _finalize_bucket(
                    self._current_bucket_start,
                    self._raw_samples,
                    raw_interval_seconds=self.raw_interval_seconds,
                )
                self._raw_samples = []
            self._current_bucket_start = bucket_start
            self._raw_samples.append(sample)
            return completed

    def current_partial_bucket(self) -> MetricsBucket | None:
        with self._lock:
            if not self._raw_samples or self._current_bucket_start is None:
                return None
            return _finalize_bucket(
                self._current_bucket_start,
                self._raw_samples,
                raw_interval_seconds=self.raw_interval_seconds,
            )

    def reset(self) -> None:
        with self._lock:
            self._current_bucket_start = None
            self._raw_samples = []


_bucket_accumulator = BucketAccumulator()


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
                return (
                    100.0 * used_kb / total_kb,
                    used_kb * 1024,
                    total_kb * 1024,
                )
        except (OSError, ValueError):
            pass

    memory, _warn = _read_memory()
    if memory is None:
        return None, None, None
    total_b = _human_to_bytes(memory.total)
    used_b = _human_to_bytes(memory.used)
    return memory.percent_used, used_b, total_b


def _compute_cpu_percent_for_raw(
    previous: RawSample | MetricsBucket | MetricsSample | None,
) -> tuple[float | None, int | None, int | None]:
    current = read_proc_stat_jiffies()
    if current is None:
        return None, None, None
    total, idle = current
    prev_total = None
    prev_idle = None
    if previous is not None:
        if isinstance(previous, RawSample):
            prev_total = previous.cpu_total_jiffies
            prev_idle = previous.cpu_idle_jiffies
        elif isinstance(previous, MetricsBucket):
            return None, total, idle
        elif isinstance(previous, MetricsSample):
            prev_total = previous.cpu_total_jiffies
            prev_idle = previous.cpu_idle_jiffies
    if prev_total is not None and prev_idle is not None:
        pct = compute_cpu_percent(prev_total, prev_idle, total, idle)
        if pct is not None:
            return pct, total, idle
    return None, total, idle


def collect_raw_sample(
    now: datetime | None = None,
    *,
    previous: RawSample | MetricsBucket | MetricsSample | None = None,
) -> RawSample | None:
    """Read current host metrics for a 5-second raw sample."""
    loads = _read_load_numeric()
    if loads is None:
        return None
    pct_mem, used_b, total_b = _read_memory_numeric()
    cpu_pct, total_jiffies, idle_jiffies = _compute_cpu_percent_for_raw(previous)
    ts = now or datetime.now(timezone.utc)
    return RawSample(
        timestamp=ts,
        load_1=loads[0],
        load_5=loads[1],
        load_15=loads[2],
        memory_used_percent=pct_mem,
        memory_used_bytes=used_b,
        memory_total_bytes=total_b,
        cpu_percent=cpu_pct,
        cpu_temp_celsius=read_cpu_temp_celsius(),
        cpu_total_jiffies=total_jiffies,
        cpu_idle_jiffies=idle_jiffies,
        cpu_threads=read_cpu_thread_count(),
    )


def parse_history_line(data: dict[str, Any]) -> MetricsBucket | None:
    if data.get("format_version") == BUCKET_FORMAT_VERSION or "cpu_peak_percent" in data:
        return MetricsBucket.from_dict(data)
    legacy = MetricsSample.from_dict(data)
    if legacy is not None:
        return legacy_sample_to_bucket(legacy)
    return None


def parse_history_lines(text: str) -> list[MetricsBucket]:
    buckets: list[MetricsBucket] = []
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
        bucket = parse_history_line(data)
        if bucket is not None:
            buckets.append(bucket)
    return buckets


def read_history(path: Path | None = None) -> list[MetricsBucket]:
    history_path = path or HISTORY_PATH
    if not history_path.is_file():
        return []
    try:
        return parse_history_lines(history_path.read_text(encoding="utf-8"))
    except OSError:
        return []


def prune_buckets(
    buckets: list[MetricsBucket],
    *,
    now: datetime | None = None,
    retention_hours: int = RETENTION_HOURS,
) -> list[MetricsBucket]:
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(hours=retention_hours)
    return [bucket for bucket in buckets if bucket.timestamp >= cutoff]


def _latest_bucket(buckets: list[MetricsBucket]) -> MetricsBucket | None:
    if not buckets:
        return None
    return max(buckets, key=lambda bucket: bucket.timestamp)


def append_bucket(
    bucket: MetricsBucket,
    *,
    path: Path | None = None,
    now: datetime | None = None,
) -> list[MetricsBucket]:
    """Append or replace a bucket for the same timestamp, persist atomically."""
    history_path = path or HISTORY_PATH
    with _history_io_lock:
        current = prune_buckets(read_history(history_path), now=now)
        current = [b for b in current if b.timestamp != bucket.timestamp]
        current.append(bucket)
        current.sort(key=lambda item: item.timestamp)
        try:
            history_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = history_path.with_suffix(history_path.suffix + ".tmp")
            body = "\n".join(item.to_json_line() for item in current)
            if body:
                body += "\n"
            tmp.write_text(body, encoding="utf-8")
            tmp.replace(history_path)
        except OSError as exc:
            logger.warning("Could not persist metrics history: %s", exc)
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


def _peak_memory(buckets: list[MetricsBucket]) -> tuple[float | None, int | None]:
    peak_pct: float | None = None
    peak_bytes: int | None = None
    for bucket in buckets:
        if bucket.memory_used_percent is not None:
            peak_pct = (
                bucket.memory_used_percent
                if peak_pct is None
                else max(peak_pct, bucket.memory_used_percent)
            )
        if bucket.memory_used_bytes is not None:
            peak_bytes = (
                bucket.memory_used_bytes
                if peak_bytes is None
                else max(peak_bytes, bucket.memory_used_bytes)
            )
    return peak_pct, peak_bytes


def _peak_load(buckets: list[MetricsBucket]) -> float | None:
    peak: float | None = None
    for bucket in buckets:
        peak = bucket.load_1 if peak is None else max(peak, bucket.load_1)
    return peak


def _peak_cpu_percent(buckets: list[MetricsBucket]) -> float | None:
    peak: float | None = None
    for bucket in buckets:
        if bucket.cpu_peak_percent is None:
            continue
        peak = (
            bucket.cpu_peak_percent
            if peak is None
            else max(peak, bucket.cpu_peak_percent)
        )
    return peak


def _high_temp(buckets: list[MetricsBucket]) -> float | None:
    peak: float | None = None
    for bucket in buckets:
        if bucket.temp_peak_celsius is None:
            continue
        peak = (
            bucket.temp_peak_celsius
            if peak is None
            else max(peak, bucket.temp_peak_celsius)
        )
    return peak


def _average_temp(buckets: list[MetricsBucket]) -> float | None:
    weighted: list[tuple[float, int]] = []
    for bucket in buckets:
        if bucket.temp_avg_celsius is None or bucket.cpu_samples_count <= 0:
            continue
        weighted.append((bucket.temp_avg_celsius, bucket.cpu_samples_count))
    if not weighted:
        return None
    total_weight = sum(weight for _, weight in weighted)
    if total_weight <= 0:
        return None
    return sum(value * weight for value, weight in weighted) / total_weight


def seconds_cpu_above_threshold(
    buckets: list[MetricsBucket],
    *,
    window_start: datetime,
    now: datetime,
) -> float:
    """Sum bucket-level seconds above threshold within the window."""
    total_seconds = 0.0
    for bucket in buckets:
        if bucket.timestamp < window_start or bucket.timestamp > now:
            continue
        total_seconds += bucket.cpu_seconds_over_90
    return total_seconds


def minutes_cpu_above_threshold(
    buckets: list[MetricsBucket],
    *,
    threshold: float,
    window_start: datetime,
    now: datetime,
) -> float:
    """Return minutes above threshold; uses bucket seconds when available."""
    _ = threshold
    return seconds_cpu_above_threshold(buckets, window_start=window_start, now=now) / 60.0


def _format_minutes(value: float | None) -> str:
    if value is None:
        return COLLECTING_LABEL
    if value < 1.0:
        return "<1 min"
    return f"{value:.0f} min"


def _day_start_utc(now: datetime) -> datetime:
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


def _buckets_for_summary(
    path: Path | None = None,
    *,
    now: datetime | None = None,
    include_partial: bool = True,
) -> list[MetricsBucket]:
    ts_now = now or datetime.now(timezone.utc)
    buckets = prune_buckets(read_history(path), now=ts_now)
    if include_partial:
        partial = _bucket_accumulator.current_partial_bucket()
        if partial is not None and partial.timestamp >= ts_now - timedelta(hours=RETENTION_HOURS):
            buckets = [b for b in buckets if b.timestamp != partial.timestamp]
            buckets.append(partial)
            buckets.sort(key=lambda item: item.timestamp)
    return buckets


def compute_metrics_summary(
    buckets: list[MetricsBucket],
    *,
    now: datetime | None = None,
    live_cpu_percent: float | None = None,
    live_cpu_temp: float | None = None,
    live_cpu_threads: int | None = None,
    live_load_1: float | None = None,
) -> dict[str, Any]:
    """Compute peak, saturation, and temperature metrics for dashboard display."""
    ts_now = now or datetime.now(timezone.utc)
    buckets_1h = [
        bucket for bucket in buckets if bucket.timestamp >= ts_now - timedelta(hours=1)
    ]
    buckets_24h = buckets
    buckets_today = [
        bucket for bucket in buckets if bucket.timestamp >= _day_start_utc(ts_now)
    ]

    mem_pct_1h, mem_bytes_1h = _peak_memory(buckets_1h)
    mem_pct_24h, mem_bytes_24h = _peak_memory(buckets_24h)
    load_1h = _peak_load(buckets_1h)
    load_24h = _peak_load(buckets_24h)
    cpu_peak_1h = _peak_cpu_percent(buckets_1h)
    cpu_peak_24h = _peak_cpu_percent(buckets_24h)
    temp_high_today = _high_temp(buckets_today)
    temp_high_24h = _high_temp(buckets_24h)
    temp_avg_1h = _average_temp(buckets_1h)

    cpu_seconds_1h = seconds_cpu_above_threshold(
        buckets,
        window_start=ts_now - timedelta(hours=1),
        now=ts_now,
    )
    cpu_seconds_24h = seconds_cpu_above_threshold(
        buckets,
        window_start=ts_now - timedelta(hours=24),
        now=ts_now,
    )
    cpu_above_90_1h = cpu_seconds_1h / 60.0
    cpu_above_90_24h = cpu_seconds_24h / 60.0

    cpu_threads = live_cpu_threads
    if cpu_threads is None and buckets:
        latest = _latest_bucket(buckets)
        if latest is not None:
            cpu_threads = latest.cpu_threads

    load_pressure = None
    if live_load_1 is not None:
        load_pressure = compute_load_pressure(live_load_1, cpu_threads)

    cpu_now = live_cpu_percent
    if cpu_now is None and buckets_1h:
        partial = _bucket_accumulator.current_partial_bucket()
        if partial is not None and partial.cpu_peak_percent is not None:
            cpu_now = partial.cpu_avg_percent
        else:
            latest = _latest_bucket(buckets_1h)
            if latest is not None:
                cpu_now = latest.cpu_avg_percent

    temp_now = live_cpu_temp
    if temp_now is None and buckets_1h:
        latest = _latest_bucket(buckets_1h)
        if latest is not None:
            temp_now = latest.temp_avg_celsius

    def _line(
        mem_pct: float | None,
        mem_bytes: int | None,
        load: float | None,
        *,
        has_buckets: bool,
    ) -> dict[str, str | None]:
        if not has_buckets:
            return {"memory": COLLECTING_LABEL, "load": COLLECTING_LABEL}
        return {
            "memory": _format_memory_peak(mem_pct, mem_bytes) or COLLECTING_LABEL,
            "load": f"{load:.2f}" if load is not None else COLLECTING_LABEL,
        }

    window_1h = _line(mem_pct_1h, mem_bytes_1h, load_1h, has_buckets=bool(buckets_1h))
    window_24h = _line(mem_pct_24h, mem_bytes_24h, load_24h, has_buckets=bool(buckets_24h))

    return {
        "cpu_now": format_cpu_percent(cpu_now) if cpu_now is not None else COLLECTING_LABEL,
        "cpu_now_value": cpu_now,
        "cpu_above_90_minutes_1h": _format_minutes(cpu_above_90_1h if buckets_1h else None),
        "cpu_above_90_minutes_1h_raw": cpu_above_90_1h if buckets_1h else None,
        "cpu_above_90_seconds_1h_raw": cpu_seconds_1h if buckets_1h else None,
        "cpu_above_90_minutes_24h": _format_minutes(cpu_above_90_24h if buckets_24h else None),
        "cpu_above_90_minutes_24h_raw": cpu_above_90_24h if buckets_24h else None,
        "cpu_above_90_seconds_24h_raw": cpu_seconds_24h if buckets_24h else None,
        "temp_now": format_celsius(temp_now) if temp_now is not None else NOT_AVAILABLE_LABEL,
        "temp_now_celsius": temp_now,
        "temp_high_today": (
            format_celsius(temp_high_today)
            if temp_high_today is not None
            else (NOT_AVAILABLE_LABEL if not buckets_today else COLLECTING_LABEL)
        ),
        "temp_high_today_celsius": temp_high_today,
        "temp_avg_1h": (
            format_celsius(temp_avg_1h)
            if temp_avg_1h is not None
            else (COLLECTING_LABEL if buckets_1h else NOT_AVAILABLE_LABEL)
        ),
        "temp_high_24h": (
            format_celsius(temp_high_24h)
            if temp_high_24h is not None
            else (COLLECTING_LABEL if buckets_24h else NOT_AVAILABLE_LABEL)
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
        "sample_count": len(buckets),
        "sample_count_1h": len(buckets_1h),
        "thresholds": {
            "temp_warn_celsius": TEMP_WARN_CELSIUS,
            "temp_critical_celsius": TEMP_CRITICAL_CELSIUS,
            "cpu_sat_threshold": CPU_SATURATION_THRESHOLD,
            "cpu_sat_warn_minutes_1h": CPU_SAT_WARN_MINUTES_1H,
            "cpu_sat_critical_minutes_1h": CPU_SAT_CRITICAL_MINUTES_1H,
        },
    }


def compute_peaks(
    buckets: list[MetricsBucket],
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    return compute_metrics_summary(buckets, now=now)


def _collect_live_readings(
    *,
    previous: RawSample | MetricsBucket | MetricsSample | None = None,
) -> dict[str, float | int | None]:
    live_cpu_percent: float | None = None
    if isinstance(previous, RawSample):
        if previous.cpu_total_jiffies is not None and previous.cpu_idle_jiffies is not None:
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


def record_raw_sample(
    *,
    path: Path | None = None,
    now: datetime | None = None,
) -> bool:
    """Collect a 5-second raw sample and persist any completed 60s bucket."""
    ts_now = now or datetime.now(timezone.utc)
    previous = _bucket_accumulator.last_raw_sample()
    if previous is None:
        buckets = read_history(path)
        previous = _latest_bucket(buckets)
    sample = collect_raw_sample(now=ts_now, previous=previous)
    if sample is None:
        logger.warning("Raw metrics sample skipped: host load average unavailable")
        return False
    if sample.cpu_temp_celsius is None:
        logger.warning("CPU temperature sensor not available for metrics sample")
    if sample.cpu_percent is None:
        logger.warning("CPU utilization unavailable for metrics sample")
    completed_bucket = _bucket_accumulator.add(sample)
    if completed_bucket is not None:
        append_bucket(completed_bucket, path=path, now=ts_now)
        return True
    return False


def record_sample_if_due(
    *,
    path: Path | None = None,
    now: datetime | None = None,
) -> bool:
    """Backward-compatible alias for the raw sampler tick."""
    return record_raw_sample(path=path, now=now)


def get_metrics_summary(
    *,
    path: Path | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Read bucket history and compute dashboard metrics without sampling."""
    ts_now = now or datetime.now(timezone.utc)
    try:
        buckets = _buckets_for_summary(path, now=ts_now)
        previous = _bucket_accumulator.last_raw_sample()
        if previous is None:
            previous = _latest_bucket(buckets)
        live = _collect_live_readings(previous=previous)
        return compute_metrics_summary(
            buckets,
            now=ts_now,
            live_cpu_percent=live["live_cpu_percent"],
            live_cpu_temp=live["live_cpu_temp"],
            live_cpu_threads=live["live_cpu_threads"],
            live_load_1=live["live_load_1"],
        )
    except Exception:
        logger.warning("Failed to compute metrics summary", exc_info=True)
        return compute_metrics_summary([], now=ts_now)


def sample_and_get_peaks(
    *,
    path: Path | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    record_raw_sample(path=path, now=now)
    return get_metrics_summary(path=path, now=now)


def collect_current_sample(
    now: datetime | None = None,
    *,
    previous: MetricsSample | None = None,
) -> MetricsSample | None:
    """Backward-compatible wrapper returning a legacy point sample."""
    prev_raw: RawSample | MetricsSample | None = None
    if previous is not None:
        prev_raw = RawSample(
            timestamp=previous.timestamp,
            load_1=previous.load_1,
            load_5=previous.load_5,
            load_15=previous.load_15,
            memory_used_percent=previous.memory_used_percent,
            memory_used_bytes=previous.memory_used_bytes,
            memory_total_bytes=previous.memory_total_bytes,
            cpu_percent=previous.cpu_percent,
            cpu_temp_celsius=previous.cpu_temp_celsius,
            cpu_total_jiffies=previous.cpu_total_jiffies,
            cpu_idle_jiffies=previous.cpu_idle_jiffies,
            cpu_threads=previous.cpu_threads,
        )
    raw = collect_raw_sample(now=now, previous=prev_raw)
    if raw is None:
        return None
    return MetricsSample(
        timestamp=raw.timestamp,
        load_1=raw.load_1,
        load_5=raw.load_5,
        load_15=raw.load_15,
        memory_used_percent=raw.memory_used_percent,
        memory_used_bytes=raw.memory_used_bytes,
        memory_total_bytes=raw.memory_total_bytes,
        cpu_percent=raw.cpu_percent,
        cpu_temp_celsius=raw.cpu_temp_celsius,
        cpu_total_jiffies=raw.cpu_total_jiffies,
        cpu_idle_jiffies=raw.cpu_idle_jiffies,
        cpu_threads=raw.cpu_threads,
    )


prune_samples = prune_buckets


def append_sample(
    sample: MetricsSample,
    *,
    path: Path | None = None,
    now: datetime | None = None,
) -> list[MetricsBucket]:
    return append_bucket(legacy_sample_to_bucket(sample), path=path, now=now)


def build_bucket_from_raw_samples(
    raw_samples: list[RawSample],
    *,
    bucket_start: datetime | None = None,
) -> MetricsBucket:
    """Test helper: finalize a bucket from explicit raw samples."""
    if not raw_samples:
        raise ValueError("raw_samples must not be empty")
    start = bucket_start or _floor_to_bucket(raw_samples[0].timestamp, BUCKET_SECONDS)
    bucket = _finalize_bucket(start, raw_samples)
    if bucket is None:
        raise ValueError("could not build bucket")
    return bucket
