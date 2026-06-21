"""
Unit tests for Glances-backed Raven Health telemetry.
"""

from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

DASHBOARD_DIR = Path(__file__).resolve().parent.parent / "dashboard"
sys.path.insert(0, str(DASHBOARD_DIR))

from glances_client import (  # noqa: E402
    GLANCES_UNAVAILABLE_LABEL,
    _fetch_all_json,
    fetch_glances_snapshot,
)
from metrics_sampler import (  # noqa: E402
    MetricsSampler,
    is_sampler_enabled,
    start_metrics_sampler,
    stop_metrics_sampler,
)
from raven_metrics_history import (  # noqa: E402
    MetricsSample,
    get_metrics_summary,
)


MOCK_CPU = {
    "total": 42.5,
    "idle": 57.5,
    "cpucore": 4,
}

MOCK_LOAD = {
    "min1": 1.23,
    "min5": 0.98,
    "min15": 0.75,
    "cpucore": 4,
}

MOCK_MEM = {
    "percent": 63.2,
    "total": 8_000_000_000,
    "available": 2_944_000_000,
}

MOCK_SWAP = {
    "percent": 12.0,
    "total": 4_000_000_000,
    "used": 480_000_000,
}

MOCK_SENSORS = [
    {
        "label": "Ambient",
        "type": "temperature_core",
        "unit": "C",
        "value": 33,
    },
    {
        "label": "Package id 0",
        "type": "temperature_core",
        "unit": "C",
        "value": 61,
    },
]

MOCK_PERCPU = [
    {"cpu_number": 0, "total": 10.0, "idle": 90.0},
    {"cpu_number": 1, "total": 20.0, "idle": 80.0},
]

MOCK_PROCESSLIST = [
    {"name": "python3", "cpu_percent": 12.5},
    {"name": "chrome", "cpu_percent": 8.0},
    {"name": "systemd", "cpu_percent": 1.0},
]


def _mock_fetch(path: str, *, timeout: float | None = None):
    payloads = {
        "/api/4/cpu": MOCK_CPU,
        "/api/4/load": MOCK_LOAD,
        "/api/4/mem": MOCK_MEM,
        "/api/4/memswap": MOCK_SWAP,
        "/api/4/sensors": MOCK_SENSORS,
        "/api/4/percpu": MOCK_PERCPU,
        "/api/4/processlist": MOCK_PROCESSLIST,
    }
    return payloads.get(path)


class TestGlancesClient:
    def test_fetch_glances_snapshot_parses_live_metrics(self, monkeypatch):
        monkeypatch.setenv("DASHBOARD_USE_GLANCES", "true")
        with patch("glances_client._fetch_json", side_effect=_mock_fetch):
            snapshot = fetch_glances_snapshot()

        assert snapshot["available"] is True
        assert snapshot["cpu_now"] == "42%"
        assert snapshot["load_average"] == "1.23 / 0.98 / 0.75"
        assert snapshot["memory"] == "63% · 4.7 GB / 7.5 GB"
        assert snapshot["swap"] == "12% · 0.4 GB / 3.7 GB"
        assert snapshot["temp_now"] == "61°C"
        assert snapshot["cpu_per_core_summary"] == "C0 10%, C1 20%"
        assert snapshot["top_processes_summary"] == "python3 12%, chrome 8%, systemd 1%"

    def test_fetch_glances_snapshot_unavailable_when_api_empty(self, monkeypatch):
        monkeypatch.setenv("DASHBOARD_USE_GLANCES", "true")
        with patch("glances_client._fetch_json", return_value=None):
            snapshot = fetch_glances_snapshot()

        assert snapshot["available"] is False
        assert snapshot["status_message"] == GLANCES_UNAVAILABLE_LABEL

    def test_fetch_glances_snapshot_aborts_slow_requests_within_budget(self, monkeypatch):
        monkeypatch.setenv("DASHBOARD_GLANCES_FETCH_BUDGET_SECONDS", "0.2")
        monkeypatch.setenv("DASHBOARD_GLANCES_REQUEST_TIMEOUT_SECONDS", "5.0")

        def slow_fetch(path: str, *, timeout: float | None = None):
            time.sleep(1.0)
            return _mock_fetch(path)

        started = time.monotonic()
        with patch("glances_client._fetch_json", side_effect=slow_fetch):
            snapshot = fetch_glances_snapshot()
        elapsed = time.monotonic() - started

        assert elapsed < 0.8
        assert snapshot["available"] is False
        assert snapshot["status_message"] == GLANCES_UNAVAILABLE_LABEL

    def test_fetch_all_json_parallel_returns_within_budget(self, monkeypatch):
        monkeypatch.setenv("DASHBOARD_GLANCES_FETCH_BUDGET_SECONDS", "0.5")
        monkeypatch.setenv("DASHBOARD_GLANCES_REQUEST_TIMEOUT_SECONDS", "1.0")

        def quick_fetch(path: str, *, timeout: float | None = None):
            return _mock_fetch(path)

        started = time.monotonic()
        with patch("glances_client._fetch_json", side_effect=quick_fetch):
            payload = _fetch_all_json()
        elapsed = time.monotonic() - started

        assert elapsed < 0.8
        assert payload["/api/4/cpu"] == MOCK_CPU
        assert payload["/api/4/load"] == MOCK_LOAD


class TestMetricsSummaryWithGlances:
    def test_get_metrics_summary_uses_glances_live_values(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DASHBOARD_USE_GLANCES", "true")
        history_path = tmp_path / "history.jsonl"
        now = datetime(2026, 6, 19, 12, 0, tzinfo=timezone.utc)
        sample = MetricsSample(
            timestamp=now - timedelta(minutes=30),
            load_1=2.0,
            load_5=1.5,
            load_15=1.0,
            memory_used_percent=70.0,
            memory_used_bytes=5_000_000_000,
            memory_total_bytes=8_000_000_000,
            cpu_percent=88.0,
            cpu_temp_celsius=72.0,
            cpu_total_jiffies=1000,
            cpu_idle_jiffies=500,
            cpu_threads=4,
        )
        history_path.write_text(sample.to_json_line() + "\n", encoding="utf-8")

        with patch("glances_client._fetch_json", side_effect=_mock_fetch):
            summary = get_metrics_summary(path=history_path, now=now)

        assert summary["metrics_source"] == "glances"
        assert summary["glances_available"] is True
        assert summary["cpu_now"] == "42%"
        assert summary["temp_now"] == "61°C"
        assert summary["load_average"] == "1.23 / 0.98 / 0.75"
        assert summary["memory_live"] == "63% · 4.7 GB / 7.5 GB"
        assert summary["swap"] == "12% · 0.4 GB / 3.7 GB"
        assert summary["peak_cpu_1h"] == "88%"
        assert summary["top_processes_summary"] == "python3 12%, chrome 8%, systemd 1%"

    def test_get_metrics_summary_falls_back_when_glances_unavailable(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("DASHBOARD_USE_GLANCES", "true")
        history_path = tmp_path / "history.jsonl"
        now = datetime(2026, 6, 19, 12, 0, tzinfo=timezone.utc)

        with patch("glances_client._fetch_json", return_value=None):
            with patch(
                "raven_metrics_history._collect_live_readings",
                return_value={
                    "live_cpu_percent": 15.0,
                    "live_cpu_temp": 55.0,
                    "live_cpu_threads": 4,
                    "live_load_1": 0.5,
                },
            ):
                summary = get_metrics_summary(path=history_path, now=now)

        assert summary["metrics_source"] == "fallback"
        assert summary["glances_available"] is False
        assert summary["glances_status"] == GLANCES_UNAVAILABLE_LABEL
        assert summary["cpu_now"] == "15%"

    def test_sampler_enabled_by_default_when_glances_enabled(self, monkeypatch):
        monkeypatch.delenv("DASHBOARD_METRICS_SAMPLER_ENABLED", raising=False)
        monkeypatch.setenv("DASHBOARD_USE_GLANCES", "true")
        assert is_sampler_enabled() is True

    def test_sampler_disabled_when_explicitly_off(self, monkeypatch):
        monkeypatch.setenv("DASHBOARD_METRICS_SAMPLER_ENABLED", "0")
        monkeypatch.setenv("DASHBOARD_USE_GLANCES", "true")
        assert is_sampler_enabled() is False

    def test_sampler_enabled_by_default_without_glances(self, monkeypatch):
        monkeypatch.delenv("DASHBOARD_METRICS_SAMPLER_ENABLED", raising=False)
        monkeypatch.setenv("DASHBOARD_USE_GLANCES", "false")
        assert is_sampler_enabled() is True


class TestGlancesHistorySampler:
    def test_collect_glances_sample_writes_compact_fields(self, monkeypatch):
        monkeypatch.setenv("DASHBOARD_USE_GLANCES", "true")
        glances_metrics = {
            "available": True,
            "cpu_total_percent": 42.5,
            "load_1": 1.2,
            "load_5": 1.0,
            "load_15": 0.8,
            "cpu_threads": 4,
            "memory_used_percent": 55.0,
            "memory_used_bytes": 4_000_000_000,
            "memory_total_bytes": 8_000_000_000,
            "network_rx_bps": 1024.0,
            "network_tx_bps": 512.0,
        }
        with patch(
            "raven_metrics_history.fetch_glances_history_metrics",
            return_value=glances_metrics,
        ):
            from raven_metrics_history import collect_glances_sample

            sample = collect_glances_sample(
                now=datetime(2026, 6, 20, 12, 0, tzinfo=timezone.utc)
            )
        assert sample is not None
        assert sample.cpu_percent == 42.5
        assert sample.network_rx_bps == 1024.0
        assert sample.network_tx_bps == 512.0
        assert sample.cpu_total_jiffies is None
        parsed = json.loads(sample.to_json_line())
        assert "network_rx_bps" in parsed
        assert "network_tx_bps" in parsed

    def test_record_sample_if_due_uses_glances_when_enabled(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("DASHBOARD_USE_GLANCES", "true")
        monkeypatch.setattr("raven_metrics_history.GLANCES_HISTORY_INTERVAL_SECONDS", 1)
        history_path = tmp_path / "history.jsonl"
        now = datetime(2026, 6, 20, 12, 0, tzinfo=timezone.utc)
        glances_metrics = {
            "available": True,
            "cpu_total_percent": 10.0,
            "load_1": 0.5,
            "load_5": 0.4,
            "load_15": 0.3,
            "cpu_threads": 4,
            "memory_used_percent": 40.0,
            "memory_used_bytes": 3_000_000_000,
            "memory_total_bytes": 8_000_000_000,
            "network_rx_bps": 100.0,
            "network_tx_bps": 50.0,
        }
        with patch(
            "raven_metrics_history.fetch_glances_history_metrics",
            return_value=glances_metrics,
        ):
            from raven_metrics_history import record_sample_if_due

            assert record_sample_if_due(path=history_path, now=now) is True
            lines = history_path.read_text(encoding="utf-8").splitlines()
            assert len(lines) == 1
            row = json.loads(lines[0])
            assert row["cpu_percent"] == 10.0
            assert row["network_rx_bps"] == 100.0
