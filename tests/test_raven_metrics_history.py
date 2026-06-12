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

from raven_metrics_history import (  # noqa: E402
    COLLECTING_LABEL,
    MetricsSample,
    append_sample,
    compute_peaks,
    parse_history_lines,
    prune_samples,
    read_history,
    sample_and_get_peaks,
)


def _sample(
    *,
    offset_hours: float = 0,
    load_1: float = 1.0,
    mem_pct: float = 50.0,
    mem_used: int = 5_000_000_000,
    mem_total: int = 8_000_000_000,
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
    )


class TestMetricsHistoryBasics:
    def test_empty_no_history_file(self, tmp_path: Path):
        path = tmp_path / "missing.jsonl"
        peaks = compute_peaks(read_history(path))
        assert peaks["peak_memory_1h"] == COLLECTING_LABEL
        assert peaks["peak_memory_24h"] == COLLECTING_LABEL
        assert peaks["peak_load_1h"] == COLLECTING_LABEL
        assert peaks["peak_load_24h"] == COLLECTING_LABEL

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
        assert peaks["peak_load_1h"] == "2.00"
        assert "60%" in peaks["peak_memory_1h"]

    def test_prune_older_than_24h(self):
        now = datetime(2026, 6, 12, 12, 0, tzinfo=timezone.utc)
        samples = [
            _sample(offset_hours=25, load_1=9.0, mem_pct=90.0, now=now),
            _sample(offset_hours=23, load_1=3.0, mem_pct=70.0, now=now),
            _sample(offset_hours=1, load_1=2.0, mem_pct=60.0, now=now),
        ]
        pruned = prune_samples(samples, now=now)
        assert len(pruned) == 2
        assert all(s.load_1 != 9.0 for s in pruned)

    def test_peak_1h_calculation(self):
        now = datetime(2026, 6, 12, 12, 0, tzinfo=timezone.utc)
        samples = [
            _sample(offset_hours=0.5, load_1=2.14, mem_pct=62.0, mem_used=5_100_000_000, now=now),
            _sample(offset_hours=0.25, load_1=1.50, mem_pct=55.0, mem_used=4_500_000_000, now=now),
            _sample(offset_hours=2.0, load_1=3.02, mem_pct=71.0, mem_used=5_800_000_000, now=now),
        ]
        peaks = compute_peaks(samples, now=now)
        assert peaks["peak_load_1h"] == "2.14"
        assert peaks["peak_memory_1h"] == "62% / 4.7 GB"
        assert peaks["peak_load_24h"] == "3.02"
        assert peaks["peak_memory_24h"] == "71% / 5.4 GB"

    def test_peak_24h_calculation(self):
        now = datetime(2026, 6, 12, 12, 0, tzinfo=timezone.utc)
        samples = [
            _sample(offset_hours=20, load_1=1.0, mem_pct=40.0, now=now),
            _sample(offset_hours=10, load_1=3.02, mem_pct=71.0, mem_used=5_800_000_000, now=now),
            _sample(offset_hours=30, load_1=9.99, mem_pct=99.0, now=now),
        ]
        pruned = prune_samples(samples, now=now)
        peaks = compute_peaks(pruned, now=now)
        assert peaks["peak_load_24h"] == "3.02"
        assert peaks["peak_memory_24h"] == "71% / 5.4 GB"

    def test_append_sample_persists_and_prunes(self, tmp_path: Path):
        now = datetime(2026, 6, 12, 12, 0, tzinfo=timezone.utc)
        path = tmp_path / "history.jsonl"
        old = _sample(offset_hours=30, load_1=9.0, mem_pct=99.0, now=now)
        append_sample(old, path=path, now=now)
        new = _sample(offset_hours=0, load_1=1.2, mem_pct=45.0, now=now)
        samples = append_sample(new, path=path, now=now)
        assert len(samples) == 1
        assert samples[0].load_1 == 1.2
        assert path.is_file()


class TestDashboardPeaksIntegration:
    def test_dashboard_renders_peak_values_without_crashing(self, tmp_path: Path, monkeypatch):
        from fastapi.testclient import TestClient

        import app as dashboard_app

        history_path = tmp_path / "raven_metrics_history.jsonl"
        now = datetime(2026, 6, 12, 12, 0, tzinfo=timezone.utc)
        sample = _sample(offset_hours=0.25, load_1=2.14, mem_pct=62.0, now=now)
        history_path.write_text(sample.to_json_line() + "\n", encoding="utf-8")

        monkeypatch.setenv("DASHBOARD_METRICS_HISTORY_PATH", str(history_path))
        monkeypatch.setattr(
            "raven_metrics_history.HISTORY_PATH",
            history_path,
        )
        monkeypatch.setattr(
            "raven_metrics_history.MIN_SAMPLE_INTERVAL_SECONDS",
            9999,
        )

        with patch("host_status.run_host_command", return_value=(False, "unavailable")):
            with patch("host_status.run_systemctl", return_value=(False, "unavailable")):
                with patch(
                    "raven_metrics_history.collect_current_sample",
                    return_value=None,
                ):
                    client = TestClient(dashboard_app.app)
                    response = client.get("/")

        assert response.status_code == 200
        assert "Peaks" in response.text
        assert "Peak memory 1h" in response.text
        assert "Peak load 1h" in response.text
        assert "2.14" in response.text

    def test_sample_and_get_peaks_collecting_when_empty(self, tmp_path: Path, monkeypatch):
        history_path = tmp_path / "raven_metrics_history.jsonl"
        monkeypatch.setenv("DASHBOARD_METRICS_HISTORY_PATH", str(history_path))
        monkeypatch.setattr("raven_metrics_history.HISTORY_PATH", history_path)

        with patch(
            "raven_metrics_history.collect_current_sample",
            return_value=None,
        ):
            peaks = sample_and_get_peaks(path=history_path)
        assert peaks["peak_memory_1h"] == COLLECTING_LABEL
