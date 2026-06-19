"""
Unit tests for Raven metrics history sampling and bucket rollup calculation.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

DASHBOARD_DIR = Path(__file__).resolve().parent.parent / "dashboard"
sys.path.insert(0, str(DASHBOARD_DIR))

from host_cpu_metrics import NOT_AVAILABLE_LABEL  # noqa: E402
from raven_metrics_history import (  # noqa: E402
    BUCKET_SECONDS,
    COLLECTING_LABEL,
    MetricsBucket,
    MetricsSample,
    RawSample,
    append_bucket,
    append_sample,
    build_bucket_from_raw_samples,
    compute_metrics_summary,
    compute_peaks,
    get_metrics_summary,
    legacy_sample_to_bucket,
    minutes_cpu_above_threshold,
    parse_history_lines,
    prune_buckets,
    read_history,
    record_raw_sample,
    sample_and_get_peaks,
    seconds_cpu_above_threshold,
    _bucket_accumulator,
    _floor_to_bucket,
)
from metrics_sampler import (  # noqa: E402
    MetricsSampler,
    start_metrics_sampler,
    stop_metrics_sampler,
)


def _bucket(
    *,
    offset_hours: float = 0,
    cpu_peak: float = 50.0,
    cpu_avg: float | None = None,
    cpu_seconds_over_90: float = 0.0,
    cpu_samples_count: int = 12,
    mem_pct: float = 50.0,
    mem_used: int = 5_000_000_000,
    mem_total: int = 8_000_000_000,
    load_1: float = 1.0,
    temp_peak: float | None = 55.0,
    temp_avg: float | None = None,
    now: datetime,
) -> MetricsBucket:
    ts = _floor_to_bucket(now - timedelta(hours=offset_hours), BUCKET_SECONDS)
    return MetricsBucket(
        timestamp=ts,
        cpu_avg_percent=cpu_avg if cpu_avg is not None else cpu_peak,
        cpu_peak_percent=cpu_peak,
        cpu_samples_count=cpu_samples_count,
        cpu_seconds_over_90=cpu_seconds_over_90,
        temp_avg_celsius=temp_avg if temp_avg is not None else temp_peak,
        temp_peak_celsius=temp_peak,
        memory_used_percent=mem_pct,
        memory_used_bytes=mem_used,
        memory_total_bytes=mem_total,
        load_1=load_1,
        load_5=load_1 * 0.8,
        load_15=load_1 * 0.6,
        cpu_threads=4,
    )


def _legacy_sample(
    *,
    offset_hours: float = 0,
    load_1: float = 1.0,
    mem_pct: float = 50.0,
    cpu_percent: float = 50.0,
    now: datetime,
) -> MetricsSample:
    return MetricsSample(
        timestamp=now - timedelta(hours=offset_hours),
        load_1=load_1,
        load_5=load_1 * 0.8,
        load_15=load_1 * 0.6,
        memory_used_percent=mem_pct,
        memory_used_bytes=5_000_000_000,
        memory_total_bytes=8_000_000_000,
        cpu_percent=cpu_percent,
        cpu_temp_celsius=55.0,
        cpu_threads=4,
    )


@pytest.fixture(autouse=True)
def _reset_accumulator():
    _bucket_accumulator.reset()
    yield
    _bucket_accumulator.reset()


class TestBucketRollup:
    def test_spike_reflected_in_cpu_peak_percent(self):
        now = datetime(2026, 6, 12, 12, 0, tzinfo=timezone.utc)
        bucket_start = _floor_to_bucket(now, BUCKET_SECONDS)
        raw_samples: list[RawSample] = []
        for index in range(12):
            offset_seconds = index * 5
            cpu = 100.0 if 20 <= offset_seconds < 30 else 40.0
            raw_samples.append(
                RawSample(
                    timestamp=bucket_start + timedelta(seconds=offset_seconds),
                    load_1=1.0,
                    load_5=1.0,
                    load_15=1.0,
                    memory_used_percent=50.0,
                    memory_used_bytes=5_000_000_000,
                    memory_total_bytes=8_000_000_000,
                    cpu_percent=cpu,
                    cpu_temp_celsius=60.0,
                    cpu_threads=4,
                )
            )
        bucket = build_bucket_from_raw_samples(raw_samples, bucket_start=bucket_start)
        assert bucket.cpu_peak_percent == 100.0
        assert bucket.cpu_seconds_over_90 == 10.0
        assert bucket.cpu_samples_count == 12

    def test_spike_adds_ten_seconds_over_90(self):
        now = datetime(2026, 6, 12, 12, 0, tzinfo=timezone.utc)
        bucket_start = _floor_to_bucket(now, BUCKET_SECONDS)
        raw_samples = [
            RawSample(
                timestamp=bucket_start + timedelta(seconds=offset),
                load_1=1.0,
                load_5=1.0,
                load_15=1.0,
                memory_used_percent=50.0,
                memory_used_bytes=5_000_000_000,
                memory_total_bytes=8_000_000_000,
                cpu_percent=100.0 if offset in (20, 25) else 30.0,
                cpu_temp_celsius=60.0,
                cpu_threads=4,
            )
            for offset in range(0, 60, 5)
        ]
        bucket = build_bucket_from_raw_samples(raw_samples, bucket_start=bucket_start)
        summary = compute_metrics_summary([bucket], now=now + timedelta(seconds=59))
        assert summary["peak_cpu_1h"] == "100%"
        assert summary["cpu_above_90_seconds_1h_raw"] == 10.0
        assert summary["cpu_above_90_minutes_1h_raw"] == pytest.approx(10.0 / 60.0)


class TestMetricsHistoryBasics:
    def test_empty_no_history_file(self, tmp_path: Path):
        path = tmp_path / "missing.jsonl"
        peaks = compute_peaks(read_history(path))
        assert peaks["peak_memory_1h"] == COLLECTING_LABEL
        assert peaks["cpu_now"] == COLLECTING_LABEL
        assert peaks["temp_now"] == NOT_AVAILABLE_LABEL

    def test_malformed_history_rows_ignored_safely(self, tmp_path: Path):
        path = tmp_path / "history.jsonl"
        now = datetime(2026, 6, 12, 12, 0, tzinfo=timezone.utc)
        good = _bucket(offset_hours=0.5, load_1=2.0, cpu_peak=60.0, mem_pct=60.0, now=now)
        path.write_text(
            "\n".join(["not json", json.dumps({"timestamp": "bad"}), good.to_json_line(), "{broken"])
            + "\n",
            encoding="utf-8",
        )
        buckets = read_history(path)
        assert len(buckets) == 1
        peaks = compute_peaks(buckets, now=now)
        assert peaks["peak_load_avg_1h"] == "2.00"
        assert "60%" in peaks["peak_memory_1h"]

    def test_prune_older_than_retention_window(self):
        now = datetime(2026, 6, 12, 12, 0, tzinfo=timezone.utc)
        buckets = [
            _bucket(offset_hours=49, cpu_peak=90.0, now=now),
            _bucket(offset_hours=47, cpu_peak=70.0, now=now),
            _bucket(offset_hours=1, cpu_peak=60.0, now=now),
        ]
        pruned = prune_buckets(buckets, now=now)
        assert len(pruned) == 2

    def test_peak_1h_calculation(self):
        now = datetime(2026, 6, 12, 12, 0, tzinfo=timezone.utc)
        buckets = [
            _bucket(offset_hours=0.5, cpu_peak=88.0, mem_pct=62.0, mem_used=5_100_000_000, now=now),
            _bucket(offset_hours=0.25, cpu_peak=72.0, mem_pct=55.0, mem_used=4_500_000_000, now=now),
            _bucket(offset_hours=2.0, cpu_peak=95.0, mem_pct=71.0, mem_used=5_800_000_000, now=now),
        ]
        peaks = compute_peaks(buckets, now=now)
        assert peaks["peak_load_avg_1h"] == "1.00"
        assert peaks["peak_cpu_1h"] == "88%"
        assert peaks["peak_cpu_24h"] == "95%"

    def test_append_bucket_persists_and_prunes(self, tmp_path: Path):
        now = datetime(2026, 6, 12, 12, 0, tzinfo=timezone.utc)
        path = tmp_path / "history.jsonl"
        old = _bucket(offset_hours=50, cpu_peak=99.0, now=now)
        append_bucket(old, path=path, now=now)
        new = _bucket(offset_hours=0, cpu_peak=45.0, now=now)
        buckets = append_bucket(new, path=path, now=now)
        assert len(buckets) == 1
        assert buckets[0].cpu_peak_percent == 45.0
        assert path.is_file()


class TestCpuSaturationAggregation:
    def test_seconds_above_90_from_buckets(self):
        now = datetime(2026, 6, 12, 12, 0, tzinfo=timezone.utc)
        buckets = [
            _bucket(offset_hours=0.9, cpu_seconds_over_90=50.0, now=now),
            _bucket(offset_hours=0.5, cpu_seconds_over_90=10.0, now=now),
            _bucket(offset_hours=2.0, cpu_seconds_over_90=120.0, now=now),
        ]
        seconds = seconds_cpu_above_threshold(
            buckets,
            window_start=now - timedelta(hours=1),
            now=now,
        )
        assert seconds == 60.0
        minutes = minutes_cpu_above_threshold(
            buckets,
            threshold=90.0,
            window_start=now - timedelta(hours=1),
            now=now,
        )
        assert minutes == 1.0

    def test_summary_reports_cpu_saturation_minutes(self):
        now = datetime(2026, 6, 12, 12, 0, tzinfo=timezone.utc)
        buckets = [_bucket(offset_hours=0.25, cpu_seconds_over_90=720.0, now=now)]
        summary = compute_metrics_summary(buckets, now=now)
        assert summary["cpu_above_90_minutes_1h"] == "12 min"
        assert summary["cpu_above_90_minutes_1h_raw"] == 12.0


class TestLegacyMigration:
    def test_legacy_sparse_samples_still_parse(self, tmp_path: Path):
        now = datetime(2026, 6, 12, 12, 0, tzinfo=timezone.utc)
        legacy = _legacy_sample(offset_hours=0.5, cpu_percent=85.0, now=now)
        path = tmp_path / "history.jsonl"
        path.write_text(json.dumps({
            "timestamp": legacy.timestamp.isoformat(),
            "load_1": legacy.load_1,
            "load_5": legacy.load_5,
            "load_15": legacy.load_15,
            "memory_used_percent": legacy.memory_used_percent,
            "memory_used_bytes": legacy.memory_used_bytes,
            "memory_total_bytes": legacy.memory_total_bytes,
            "cpu_percent": legacy.cpu_percent,
            "cpu_temp_celsius": legacy.cpu_temp_celsius,
            "cpu_threads": legacy.cpu_threads,
        }) + "\n", encoding="utf-8")
        buckets = read_history(path)
        assert len(buckets) == 1
        assert buckets[0].cpu_peak_percent == 85.0
        summary = compute_metrics_summary(buckets, now=now)
        assert summary["peak_cpu_1h"] == "85%"

    def test_legacy_sample_to_bucket_assumes_60s_over_90(self):
        now = datetime(2026, 6, 12, 12, 0, tzinfo=timezone.utc)
        legacy = _legacy_sample(cpu_percent=95.0, now=now)
        bucket = legacy_sample_to_bucket(legacy)
        assert bucket.cpu_seconds_over_90 == 60.0

    def test_append_sample_legacy_wrapper(self, tmp_path: Path):
        now = datetime(2026, 6, 12, 12, 0, tzinfo=timezone.utc)
        path = tmp_path / "history.jsonl"
        legacy = _legacy_sample(cpu_percent=70.0, now=now)
        append_sample(legacy, path=path, now=now)
        buckets = read_history(path)
        assert len(buckets) == 1
        assert buckets[0].format_version == 2


class TestRegularVsSparseSampling:
    def test_regular_buckets_aggregate_accurately(self):
        now = datetime(2026, 6, 12, 12, 0, tzinfo=timezone.utc)
        buckets = [
            _bucket(
                offset_hours=minute / 60.0,
                cpu_seconds_over_90=60.0 if minute < 12 else 0.0,
                now=now,
            )
            for minute in range(20)
        ]
        summary = compute_metrics_summary(buckets, now=now)
        assert summary["cpu_above_90_minutes_1h_raw"] == 12.0

    def test_sparse_legacy_buckets_undercount_saturation(self):
        now = datetime(2026, 6, 12, 12, 0, tzinfo=timezone.utc)
        buckets = [
            legacy_sample_to_bucket(_legacy_sample(offset_hours=0.9, cpu_percent=95.0, now=now)),
            legacy_sample_to_bucket(_legacy_sample(offset_hours=0.5, cpu_percent=95.0, now=now)),
            legacy_sample_to_bucket(_legacy_sample(offset_hours=0.1, cpu_percent=95.0, now=now)),
        ]
        summary = compute_metrics_summary(buckets, now=now)
        assert summary["cpu_above_90_minutes_1h_raw"] == 3.0

    def test_get_metrics_summary_does_not_append(self, tmp_path: Path, monkeypatch):
        history_path = tmp_path / "history.jsonl"
        now = datetime(2026, 6, 12, 12, 0, tzinfo=timezone.utc)
        bucket = _bucket(offset_hours=0.1, cpu_peak=70.0, now=now)
        history_path.write_text(bucket.to_json_line() + "\n", encoding="utf-8")
        monkeypatch.setattr("raven_metrics_history.HISTORY_PATH", history_path)

        with patch("raven_metrics_history.read_cpu_percent_live", return_value=33.0):
            with patch("raven_metrics_history.read_cpu_temp_celsius", return_value=60.0):
                with patch("raven_metrics_history.read_cpu_thread_count", return_value=4):
                    summary = get_metrics_summary(path=history_path, now=now)

        assert summary["cpu_now_value"] == 33.0
        assert history_path.read_text(encoding="utf-8").count("\n") == 1


class TestBackgroundSampler:
    def test_record_raw_sample_persists_completed_bucket(self, tmp_path: Path, monkeypatch):
        history_path = tmp_path / "history.jsonl"
        monkeypatch.setattr("raven_metrics_history.HISTORY_PATH", history_path)
        now = datetime(2026, 6, 12, 12, 0, tzinfo=timezone.utc)
        bucket_start = _floor_to_bucket(now, BUCKET_SECONDS)
        next_bucket_start = bucket_start + timedelta(seconds=BUCKET_SECONDS)

        with patch(
            "raven_metrics_history.collect_raw_sample",
            side_effect=[
                RawSample(
                    timestamp=bucket_start + timedelta(seconds=55),
                    load_1=1.0,
                    load_5=1.0,
                    load_15=1.0,
                    memory_used_percent=50.0,
                    memory_used_bytes=5_000_000_000,
                    memory_total_bytes=8_000_000_000,
                    cpu_percent=40.0,
                    cpu_temp_celsius=60.0,
                    cpu_threads=4,
                ),
                RawSample(
                    timestamp=next_bucket_start,
                    load_1=2.0,
                    load_5=2.0,
                    load_15=2.0,
                    memory_used_percent=55.0,
                    memory_used_bytes=5_100_000_000,
                    memory_total_bytes=8_000_000_000,
                    cpu_percent=100.0,
                    cpu_temp_celsius=65.0,
                    cpu_threads=4,
                ),
            ],
        ):
            assert record_raw_sample(path=history_path, now=bucket_start + timedelta(seconds=55)) is False
            assert record_raw_sample(path=history_path, now=next_bucket_start) is True

        buckets = read_history(history_path)
        assert len(buckets) == 1
        assert buckets[0].cpu_peak_percent == 40.0

    def test_sampler_thread_invokes_record_raw_sample(self, monkeypatch):
        calls: list[bool] = []

        def _fake_record() -> bool:
            calls.append(True)
            return False

        monkeypatch.setattr("metrics_sampler.record_raw_sample", _fake_record)
        sampler = MetricsSampler(interval_seconds=0.05)
        sampler.start()
        try:
            import time

            time.sleep(0.15)
        finally:
            sampler.stop()
        assert calls

    def test_sampler_disabled_by_env(self, monkeypatch):
        monkeypatch.setenv("DASHBOARD_METRICS_SAMPLER_ENABLED", "0")
        monkeypatch.setattr("metrics_sampler.SAMPLER_ENABLED", False)
        start_metrics_sampler()
        try:
            from metrics_sampler import is_metrics_sampler_running

            assert is_metrics_sampler_running() is False
        finally:
            stop_metrics_sampler()


class TestDashboardPeaksIntegration:
    def test_dashboard_renders_new_metrics_without_crashing(self, tmp_path: Path, monkeypatch):
        from fastapi.testclient import TestClient

        import app as dashboard_app

        history_path = tmp_path / "raven_metrics_history.jsonl"
        now = datetime(2026, 6, 12, 12, 0, tzinfo=timezone.utc)
        bucket = _bucket(offset_hours=0.25, cpu_peak=85.0, load_1=2.14, now=now)
        history_path.write_text(bucket.to_json_line() + "\n", encoding="utf-8")

        monkeypatch.setenv("DASHBOARD_METRICS_HISTORY_PATH", str(history_path))
        monkeypatch.setattr("raven_metrics_history.HISTORY_PATH", history_path)

        with patch("host_status.run_host_command", return_value=(False, "unavailable")):
            with patch("host_status.run_systemctl", return_value=(False, "unavailable")):
                with patch("raven_metrics_history.get_metrics_summary") as summary_mock:
                    summary_mock.return_value = {
                        "cpu_now": "42%",
                        "temp_now": "60°C",
                        "peak_load_avg_1h": "2.14",
                        "load_help": "Load is runnable work, not CPU %. Compare load to CPU threads.",
                        "peak_memory_1h": "62%",
                    }
                    client = TestClient(dashboard_app.app)
                    response = client.get("/")

        assert response.status_code == 200
        assert "CPU now" in response.text
        assert "Details" in response.text

    def test_sample_and_get_peaks_collecting_when_empty(self, tmp_path: Path, monkeypatch):
        history_path = tmp_path / "raven_metrics_history.jsonl"
        monkeypatch.setenv("DASHBOARD_METRICS_HISTORY_PATH", str(history_path))
        monkeypatch.setattr("raven_metrics_history.HISTORY_PATH", history_path)

        with patch("raven_metrics_history.collect_raw_sample", return_value=None):
            peaks = sample_and_get_peaks(path=history_path)
        assert peaks["peak_memory_1h"] == COLLECTING_LABEL
