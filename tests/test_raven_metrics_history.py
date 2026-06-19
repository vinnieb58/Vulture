"""
Unit tests for Raven metrics history sampling and peak calculation.
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

from host_cpu_metrics import (  # noqa: E402
    NOT_AVAILABLE_LABEL,
    compute_load_pressure,
    read_cpu_temp_celsius,
)
from raven_metrics_history import (  # noqa: E402
    COLLECTING_LABEL,
    MetricsSample,
    append_sample,
    compute_metrics_summary,
    compute_peaks,
    get_metrics_summary,
    minutes_cpu_above_threshold,
    parse_history_lines,
    prune_samples,
    read_history,
    record_sample_if_due,
    sample_and_get_peaks,
)
from metrics_sampler import (  # noqa: E402
    MetricsSampler,
    is_sampler_enabled,
    start_metrics_sampler,
    stop_metrics_sampler,
)


def _sample(
    *,
    offset_hours: float = 0,
    load_1: float = 1.0,
    mem_pct: float = 50.0,
    mem_used: int = 5_000_000_000,
    mem_total: int = 8_000_000_000,
    cpu_percent: float | None = 50.0,
    cpu_temp_celsius: float | None = 55.0,
    cpu_threads: int | None = 4,
    now: datetime,
) -> MetricsSample:
    return MetricsSample(
        timestamp=now - timedelta(hours=offset_hours),
        load_1=load_1,
        load_5=load_1 * 0.8,
        load_15=load_1 * 0.6,
        memory_used_percent=mem_pct,
        memory_used_bytes=mem_used,
        memory_total_bytes=mem_total,
        cpu_percent=cpu_percent,
        cpu_temp_celsius=cpu_temp_celsius,
        cpu_total_jiffies=1000,
        cpu_idle_jiffies=500,
        cpu_threads=cpu_threads,
    )


class TestMetricsHistoryBasics:
    def test_empty_no_history_file(self, tmp_path: Path):
        path = tmp_path / "missing.jsonl"
        peaks = compute_peaks(read_history(path))
        assert peaks["peak_memory_1h"] == COLLECTING_LABEL
        assert peaks["peak_memory_24h"] == COLLECTING_LABEL
        assert peaks["peak_load_avg_1h"] == COLLECTING_LABEL
        assert peaks["peak_load_avg_24h"] == COLLECTING_LABEL
        assert peaks["cpu_now"] == COLLECTING_LABEL
        assert peaks["temp_now"] == NOT_AVAILABLE_LABEL

    def test_malformed_history_rows_ignored_safely(self, tmp_path: Path):
        path = tmp_path / "history.jsonl"
        now = datetime(2026, 6, 12, 12, 0, tzinfo=timezone.utc)
        good = _sample(offset_hours=0.5, load_1=2.0, mem_pct=60.0, now=now)
        path.write_text(
            "\n".join(
                [
                    "not json",
                    json.dumps({"timestamp": "bad"}),
                    good.to_json_line(),
                    "{broken",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        samples = read_history(path)
        assert len(samples) == 1
        peaks = compute_peaks(samples, now=now)
        assert peaks["peak_load_avg_1h"] == "2.00"
        assert "60%" in peaks["peak_memory_1h"]

    def test_prune_older_than_retention_window(self):
        now = datetime(2026, 6, 12, 12, 0, tzinfo=timezone.utc)
        samples = [
            _sample(offset_hours=49, load_1=9.0, mem_pct=90.0, now=now),
            _sample(offset_hours=47, load_1=3.0, mem_pct=70.0, now=now),
            _sample(offset_hours=1, load_1=2.0, mem_pct=60.0, now=now),
        ]
        pruned = prune_samples(samples, now=now, retention_hours=48)
        assert len(pruned) == 2
        assert all(sample.load_1 != 9.0 for sample in pruned)

    def test_peak_1h_calculation(self):
        now = datetime(2026, 6, 12, 12, 0, tzinfo=timezone.utc)
        samples = [
            _sample(offset_hours=0.5, load_1=2.14, mem_pct=62.0, mem_used=5_100_000_000, cpu_percent=88.0, now=now),
            _sample(offset_hours=0.25, load_1=1.50, mem_pct=55.0, mem_used=4_500_000_000, cpu_percent=72.0, now=now),
            _sample(offset_hours=2.0, load_1=3.02, mem_pct=71.0, mem_used=5_800_000_000, cpu_percent=95.0, now=now),
        ]
        peaks = compute_peaks(samples, now=now)
        assert peaks["peak_load_avg_1h"] == "2.14"
        assert peaks["peak_memory_1h"] == "62% / 4.7 GB"
        assert peaks["peak_load_avg_24h"] == "3.02"
        assert peaks["peak_memory_24h"] == "71% / 5.4 GB"
        assert peaks["peak_cpu_1h"] == "88%"
        assert peaks["peak_cpu_24h"] == "95%"

    def test_peak_24h_calculation(self):
        now = datetime(2026, 6, 12, 12, 0, tzinfo=timezone.utc)
        samples = [
            _sample(offset_hours=20, load_1=1.0, mem_pct=40.0, now=now),
            _sample(offset_hours=10, load_1=3.02, mem_pct=71.0, mem_used=5_800_000_000, now=now),
            _sample(offset_hours=49, load_1=9.99, mem_pct=99.0, now=now),
        ]
        pruned = prune_samples(samples, now=now)
        peaks = compute_peaks(pruned, now=now)
        assert peaks["peak_load_avg_24h"] == "3.02"
        assert peaks["peak_memory_24h"] == "71% / 5.4 GB"

    def test_append_sample_persists_and_prunes(self, tmp_path: Path):
        now = datetime(2026, 6, 12, 12, 0, tzinfo=timezone.utc)
        path = tmp_path / "history.jsonl"
        old = _sample(offset_hours=50, load_1=9.0, mem_pct=99.0, now=now)
        append_sample(old, path=path, now=now)
        new = _sample(offset_hours=0, load_1=1.2, mem_pct=45.0, now=now)
        samples = append_sample(new, path=path, now=now)
        assert len(samples) == 1
        assert samples[0].load_1 == 1.2
        assert path.is_file()


class TestCpuSaturationAggregation:
    def test_minutes_above_90_last_hour(self):
        now = datetime(2026, 6, 12, 12, 0, tzinfo=timezone.utc)
        samples = [
            _sample(offset_hours=0.9, cpu_percent=95.0, now=now),
            _sample(offset_hours=0.7, cpu_percent=92.0, now=now),
            _sample(offset_hours=0.5, cpu_percent=40.0, now=now),
            _sample(offset_hours=0.3, cpu_percent=91.0, now=now),
            _sample(offset_hours=2.0, cpu_percent=99.0, now=now),
        ]
        minutes = minutes_cpu_above_threshold(
            samples,
            threshold=90.0,
            window_start=now - timedelta(hours=1),
            now=now,
        )
        assert minutes == 3.0

    def test_minutes_above_90_none_when_no_samples(self):
        now = datetime(2026, 6, 12, 12, 0, tzinfo=timezone.utc)
        minutes = minutes_cpu_above_threshold(
            [],
            threshold=90.0,
            window_start=now - timedelta(hours=1),
            now=now,
        )
        assert minutes == 0.0

    def test_summary_reports_cpu_saturation(self):
        now = datetime(2026, 6, 12, 12, 0, tzinfo=timezone.utc)
        samples = [
            _sample(offset_hours=0.5, cpu_percent=95.0, now=now),
            _sample(offset_hours=0.25, cpu_percent=92.0, now=now),
            _sample(offset_hours=0.1, cpu_percent=91.0, now=now),
        ]
        summary = compute_metrics_summary(samples, now=now, live_cpu_percent=45.0)
        assert summary["cpu_above_90_minutes_1h"] == "3 min"
        assert summary["cpu_above_90_minutes_1h_raw"] == 3.0


class TestTemperatureAggregation:
    def test_high_temp_today(self):
        now = datetime(2026, 6, 12, 15, 0, tzinfo=timezone.utc)
        samples = [
            _sample(offset_hours=2.0, cpu_temp_celsius=62.0, now=now),
            _sample(offset_hours=1.0, cpu_temp_celsius=78.0, now=now),
            _sample(offset_hours=0.5, cpu_temp_celsius=71.0, now=now),
            _sample(offset_hours=30.0, cpu_temp_celsius=99.0, now=now),
        ]
        summary = compute_metrics_summary(samples, now=now, live_cpu_temp=65.0)
        assert summary["temp_high_today"] == "78°C"
        assert summary["temp_high_today_celsius"] == 78.0

    def test_average_temp_last_hour(self):
        now = datetime(2026, 6, 12, 12, 0, tzinfo=timezone.utc)
        samples = [
            _sample(offset_hours=0.75, cpu_temp_celsius=60.0, now=now),
            _sample(offset_hours=0.5, cpu_temp_celsius=70.0, now=now),
            _sample(offset_hours=0.25, cpu_temp_celsius=80.0, now=now),
            _sample(offset_hours=3.0, cpu_temp_celsius=95.0, now=now),
        ]
        summary = compute_metrics_summary(samples, now=now)
        assert summary["temp_avg_1h"] == "70°C"

    def test_missing_temp_sensor_handling(self, tmp_path: Path, monkeypatch):
        monkeypatch.setattr("host_cpu_metrics.HOST_SYS", tmp_path / "missing-sys")
        assert read_cpu_temp_celsius() is None

        now = datetime(2026, 6, 12, 12, 0, tzinfo=timezone.utc)
        summary = compute_metrics_summary([], now=now, live_cpu_temp=None)
        assert summary["temp_now"] == NOT_AVAILABLE_LABEL
        assert summary["temp_high_today"] == NOT_AVAILABLE_LABEL


class TestLoadNormalization:
    def test_load_pressure_by_cpu_threads(self):
        assert compute_load_pressure(6.0, 4) == pytest.approx(1.5)
        assert compute_load_pressure(2.0, 8) == pytest.approx(0.25)
        assert compute_load_pressure(2.0, None) is None

    def test_summary_includes_load_pressure(self):
        now = datetime(2026, 6, 12, 12, 0, tzinfo=timezone.utc)
        samples = [_sample(offset_hours=0.1, load_1=6.0, cpu_threads=4, now=now)]
        summary = compute_metrics_summary(
            samples,
            now=now,
            live_load_1=6.0,
            live_cpu_threads=4,
        )
        assert summary["load_pressure"] == "1.50"
        assert summary["cpu_threads"] == 4


class TestRegularVsSparseSampling:
    def test_regular_minute_samples_aggregate_accurately(self, monkeypatch):
        monkeypatch.setattr("raven_metrics_history.MIN_SAMPLE_INTERVAL_SECONDS", 60)
        now = datetime(2026, 6, 12, 12, 0, tzinfo=timezone.utc)
        samples = [
            _sample(
                offset_hours=minute / 60.0,
                cpu_percent=95.0 if minute < 12 else 40.0,
                now=now,
            )
            for minute in range(20)
        ]
        summary = compute_metrics_summary(samples, now=now)
        assert summary["cpu_above_90_minutes_1h_raw"] == 12.0
        assert summary["peak_cpu_1h"] == "95%"
        assert summary["sample_count_1h"] == 20

    def test_sparse_samples_undercount_saturation(self, monkeypatch):
        monkeypatch.setattr("raven_metrics_history.MIN_SAMPLE_INTERVAL_SECONDS", 60)
        now = datetime(2026, 6, 12, 12, 0, tzinfo=timezone.utc)
        samples = [
            _sample(offset_hours=0.9, cpu_percent=95.0, now=now),
            _sample(offset_hours=0.5, cpu_percent=95.0, now=now),
            _sample(offset_hours=0.1, cpu_percent=95.0, now=now),
        ]
        summary = compute_metrics_summary(samples, now=now)
        assert summary["cpu_above_90_minutes_1h_raw"] == 3.0
        assert summary["cpu_above_90_minutes_1h"] == "3 min"

    def test_get_metrics_summary_does_not_append(self, tmp_path: Path, monkeypatch):
        history_path = tmp_path / "history.jsonl"
        now = datetime(2026, 6, 12, 12, 0, tzinfo=timezone.utc)
        sample = _sample(offset_hours=0.1, load_1=1.5, now=now)
        history_path.write_text(sample.to_json_line() + "\n", encoding="utf-8")
        monkeypatch.setattr("raven_metrics_history.HISTORY_PATH", history_path)

        with patch("raven_metrics_history.read_cpu_percent_live", return_value=33.0):
            with patch("raven_metrics_history.read_cpu_temp_celsius", return_value=60.0):
                with patch("raven_metrics_history.read_cpu_thread_count", return_value=4):
                    summary = get_metrics_summary(path=history_path, now=now)

        assert summary["cpu_now_value"] == 33.0
        assert history_path.read_text(encoding="utf-8").count("\n") == 1

    def test_record_sample_if_due_respects_interval(self, tmp_path: Path, monkeypatch):
        history_path = tmp_path / "history.jsonl"
        now = datetime(2026, 6, 12, 12, 0, tzinfo=timezone.utc)
        sample = _sample(offset_hours=0, load_1=1.0, now=now)
        append_sample(sample, path=history_path, now=now)

        with patch(
            "raven_metrics_history.collect_current_sample",
            return_value=_sample(offset_hours=0, load_1=2.0, now=now),
        ) as collect_mock:
            assert record_sample_if_due(path=history_path, now=now) is False
            collect_mock.assert_not_called()

        later = now + timedelta(seconds=61)
        with patch(
            "raven_metrics_history.collect_current_sample",
            return_value=_sample(offset_hours=0, load_1=2.0, now=later),
        ):
            assert record_sample_if_due(path=history_path, now=later) is True
        lines = [line for line in history_path.read_text(encoding="utf-8").splitlines() if line]
        assert len(lines) == 2


class TestBackgroundSampler:
    def test_record_sample_if_due_runs_without_page_view(self, tmp_path: Path, monkeypatch):
        """Background sampling logic appends on interval without HTTP requests."""
        history_path = tmp_path / "history.jsonl"
        monkeypatch.setattr("raven_metrics_history.HISTORY_PATH", history_path)
        monkeypatch.setattr("raven_metrics_history.MIN_SAMPLE_INTERVAL_SECONDS", 1)
        now = datetime(2026, 6, 12, 12, 0, tzinfo=timezone.utc)

        with patch(
            "raven_metrics_history.collect_current_sample",
            side_effect=[
                _sample(offset_hours=0, load_1=1.0, now=now),
                _sample(offset_hours=0, load_1=2.0, now=now + timedelta(seconds=2)),
            ],
        ):
            assert record_sample_if_due(path=history_path, now=now) is True
            assert record_sample_if_due(path=history_path, now=now) is False
            assert record_sample_if_due(
                path=history_path,
                now=now + timedelta(seconds=2),
            ) is True

        lines = [line for line in history_path.read_text(encoding="utf-8").splitlines() if line]
        assert len(lines) == 2

    def test_sampler_thread_invokes_record_sample_if_due(self, monkeypatch):
        calls: list[bool] = []

        def _fake_record() -> bool:
            calls.append(True)
            return False

        monkeypatch.setattr("metrics_sampler.record_sample_if_due", _fake_record)
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
        sample = _sample(
            offset_hours=0.25,
            load_1=2.14,
            mem_pct=62.0,
            cpu_percent=85.0,
            cpu_temp_celsius=72.0,
            now=now,
        )
        history_path.write_text(sample.to_json_line() + "\n", encoding="utf-8")

        monkeypatch.setenv("DASHBOARD_METRICS_HISTORY_PATH", str(history_path))
        monkeypatch.setattr("raven_metrics_history.HISTORY_PATH", history_path)
        monkeypatch.setattr("raven_metrics_history.MIN_SAMPLE_INTERVAL_SECONDS", 9999)

        with patch("host_status.run_host_command", return_value=(False, "unavailable")):
            with patch("host_status.run_systemctl", return_value=(False, "unavailable")):
                with patch("raven_metrics_history.collect_current_sample", return_value=None):
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
        assert "Temp now" in response.text
        assert "Details" in response.text
        assert "Peak load avg 1h" in response.text
        assert "Load is runnable work" in response.text

    def test_sample_and_get_peaks_collecting_when_empty(self, tmp_path: Path, monkeypatch):
        history_path = tmp_path / "raven_metrics_history.jsonl"
        monkeypatch.setenv("DASHBOARD_METRICS_HISTORY_PATH", str(history_path))
        monkeypatch.setattr("raven_metrics_history.HISTORY_PATH", history_path)

        with patch("raven_metrics_history.collect_current_sample", return_value=None):
            peaks = sample_and_get_peaks(path=history_path)
        assert peaks["peak_memory_1h"] == COLLECTING_LABEL

    def test_legacy_samples_without_cpu_fields_still_parse(self):
        now = datetime(2026, 6, 12, 12, 0, tzinfo=timezone.utc)
        legacy = {
            "timestamp": now.isoformat(),
            "load_1": 1.5,
            "load_5": 1.2,
            "load_15": 1.0,
            "memory_used_percent": 55.0,
            "memory_used_bytes": 4_000_000_000,
            "memory_total_bytes": 8_000_000_000,
        }
        sample = MetricsSample.from_dict(legacy)
        assert sample is not None
        assert sample.cpu_percent is None
        assert sample.cpu_temp_celsius is None
