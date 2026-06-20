"""
Tests for Raven Health details page and compact home card.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

DASHBOARD_DIR = Path(__file__).resolve().parent.parent / "dashboard"
sys.path.insert(0, str(DASHBOARD_DIR))

import app as dashboard_app  # noqa: E402
from glances_client import GLANCES_UNAVAILABLE_LABEL  # noqa: E402
from raven_health_details import build_raven_health_details, normalize_glances_details  # noqa: E402

MOCK_GLANCES = {
    "available": True,
    "status_message": None,
    "cpu_total_percent": 42.5,
    "cpu_now": "42%",
    "cpu_breakdown": {
        "user": 20.0,
        "system": 10.0,
        "idle": 57.5,
        "user_display": "20%",
        "system_display": "10%",
        "idle_display": "58%",
    },
    "cpu_per_core": [
        {"core": 0, "cpu_percent": 10.0, "cpu_percent_display": "10%"},
        {"core": 1, "cpu_percent": 20.0, "cpu_percent_display": "20%"},
    ],
    "load_1": 1.23,
    "load_5": 0.98,
    "load_15": 0.75,
    "load_average": "1.23 / 0.98 / 0.75",
    "cpu_threads": 4,
    "memory_percent": 63.0,
    "memory_used_bytes": 5_000_000_000,
    "memory_total_bytes": 8_000_000_000,
    "memory_free_bytes": 2_000_000_000,
    "memory_cached_bytes": 1_000_000_000,
    "memory": "63% · 4.7 GB / 7.5 GB",
    "swap_percent": 12.0,
    "swap_used_bytes": 480_000_000,
    "swap_total_bytes": 4_000_000_000,
    "swap_free_bytes": 3_520_000_000,
    "swap": "12% · 0.4 GB / 3.7 GB",
    "cpu_temp_celsius": 61.0,
    "temp_now": "61°C",
    "sensors": [
        {
            "label": "Package id 0",
            "value_celsius": 61.0,
            "value_display": "61°C",
            "is_highest": True,
        }
    ],
    "top_processes": [
        {
            "name": "python3",
            "cpu_percent": 12.5,
            "cpu_percent_display": "12%",
            "memory_percent": 2.0,
            "memory_percent_display": "2.0%",
        }
    ],
    "processes": [
        {
            "name": "python3",
            "cpu_percent": 12.5,
            "cpu_percent_display": "12%",
            "memory_percent": 2.0,
            "memory_percent_display": "2.0%",
        }
    ],
    "filesystems": [
        {
            "device": "/dev/sda1",
            "mount": "/",
            "percent": 50.0,
            "percent_display": "50%",
            "used_display": "10.0 GB",
            "total_display": "20.0 GB",
            "free_display": "10.0 GB",
        }
    ],
    "network": [
        {
            "name": "eth0",
            "bytes_recv_display": "1.0 GB",
            "bytes_sent_display": "0.5 GB",
            "speed_mbps": 1000,
        }
    ],
    "uptime_seconds": 86400.0,
    "system_info": {"hostname": "raven", "os": "Ubuntu", "kernel": "6.1.0"},
    "docker_containers": [{"name": "vulture-dashboard", "status": "running"}],
}


@pytest.fixture
def client(tmp_path, monkeypatch):
    db_path = tmp_path / "missing.db"
    log_path = tmp_path / "missing.log"
    monkeypatch.setenv("DASHBOARD_METRICS_SAMPLER_ENABLED", "0")
    monkeypatch.setenv("DASHBOARD_USE_GLANCES", "0")
    monkeypatch.setattr(dashboard_app, "DB_PATH", db_path)
    monkeypatch.setattr(dashboard_app, "LOG_PATH", log_path)
    monkeypatch.setattr("db_readers.DB_PATH", db_path)
    monkeypatch.setattr("log_readers.LOG_PATH", log_path)
    monkeypatch.setattr("vulture_runtime.LOG_PATH", log_path)
    return TestClient(dashboard_app.app)


class TestCompactRavenHomeCard:
    COMPACT_LABELS = (
        "Uptime",
        "CPU now",
        "Peak CPU 1h",
        "Peak CPU 24h",
        "CPU &gt;90% last hour",
        "Temp now",
        "Peak Temp 24h",
        "Peak Memory 24h",
        "Load 1/5/15",
        "Containers",
    )

    REMOVED_LABELS = (
        "CPU per core",
        "Swap",
        "Top CPU processes",
        "Temp avg 1h",
        "Temp high today",
        "Peak memory 1h",
        "Load pressure",
        "CPU threads",
    )

    def _home(self, client):
        with patch("host_status.run_host_command", return_value=(False, "unavailable")):
            with patch("host_status.run_systemctl", return_value=(False, "unavailable")):
                return client.get("/")

    def test_home_card_links_to_details_page(self, client):
        response = self._home(client)
        assert response.status_code == 200
        assert 'href="/raven/health"' in response.text
        assert "Details →" in response.text

    def test_home_card_excludes_verbose_fields(self, client):
        response = self._home(client)
        text = response.text
        raven_start = text.index("Raven Health")
        raven_end = text.index("Storage / Roost", raven_start)
        card_html = text[raven_start:raven_end]
        for label in self.REMOVED_LABELS:
            assert label not in card_html

    def test_home_card_includes_compact_fields(self, client, monkeypatch):
        monkeypatch.setattr(
            dashboard_app,
            "get_metrics_summary",
            lambda **kwargs: {
                "cpu_now": "42%",
                "cpu_above_90_minutes_1h": "0 min",
                "temp_now": "61°C",
                "load_average": "1.23 / 0.98 / 0.75",
                "peak_cpu_1h": "88%",
                "peak_cpu_24h": "92%",
                "temp_high_24h": "74°C",
                "peak_memory_24h": "70%",
            },
        )
        response = self._home(client)
        text = response.text
        for label in self.COMPACT_LABELS:
            assert label in text


class TestRavenHealthDetailsPage:
    def test_details_page_renders_with_mocked_glances(self, client, monkeypatch):
        monkeypatch.setenv("DASHBOARD_USE_GLANCES", "true")
        with patch("raven_health_details.fetch_glances_details_snapshot", return_value=MOCK_GLANCES):
            with patch("host_status.run_host_command", return_value=(False, "unavailable")):
                with patch("host_status.run_systemctl", return_value=(False, "unavailable")):
                    response = client.get("/raven/health")
        assert response.status_code == 200
        text = response.text
        assert "Raven Health Details (Glances)" in text
        assert "Top CPU Processes" in text
        assert "python3" in text
        for marker in (
            "data-gauge",
            "data-chart",
            "donut-gauge",
            "progress-bar",
            "CPU Usage",
            "Load Average",
            "Memory Usage",
            "Disk Usage",
            "Network",
        ):
            assert marker in text

    def test_details_page_contains_chart_and_gauge_containers(self, client, monkeypatch):
        monkeypatch.setenv("DASHBOARD_USE_GLANCES", "true")
        with patch("raven_health_details.fetch_glances_details_snapshot", return_value=MOCK_GLANCES):
            with patch("host_status.run_host_command", return_value=(False, "unavailable")):
                with patch("host_status.run_systemctl", return_value=(False, "unavailable")):
                    response = client.get("/raven/health")
        text = response.text
        assert 'id="gauge-cpu"' in text
        assert 'id="chart-cpu-1h"' in text
        assert 'class="progress-list"' in text
        assert 'data-chart="cpu-history"' in text

    def test_details_page_shows_unavailable_banner(self, client, monkeypatch):
        monkeypatch.setenv("DASHBOARD_USE_GLANCES", "true")
        unavailable = dict(MOCK_GLANCES)
        unavailable["available"] = False
        unavailable["status_message"] = GLANCES_UNAVAILABLE_LABEL
        with patch("raven_health_details.fetch_glances_details_snapshot", return_value=unavailable):
            with patch("host_status.run_host_command", return_value=(False, "unavailable")):
                with patch("host_status.run_systemctl", return_value=(False, "unavailable")):
                    response = client.get("/raven/health")
        assert response.status_code == 200
        assert "Glances unavailable" in response.text
        assert 'id="gauge-cpu"' in response.text
        assert 'id="chart-cpu-1h"' in response.text


class TestRavenHealthGlancesAPI:
    def test_api_returns_normalized_data(self, client, monkeypatch):
        monkeypatch.setenv("DASHBOARD_USE_GLANCES", "true")
        with patch("raven_health_details.fetch_glances_details_snapshot", return_value=MOCK_GLANCES):
            with patch("host_status.run_host_command", return_value=(False, "unavailable")):
                with patch("host_status.run_systemctl", return_value=(False, "unavailable")):
                    response = client.get("/api/raven/health/glances")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "live"
        assert data["overview"]["cpu"]["total_display"] == "42%"
        assert data["processes"][0]["name"] == "python3"
        assert data["disks"][0]["mount"] == "/"
        assert "cpu_history_1h" in data["history"]
        assert "load_history_1h" in data["history"]
        assert "memory_history_1h" in data["history"]
        assert "network_history_1h" in data["history"]
        assert "containers" in data

    def test_api_unavailable_status(self, client, monkeypatch):
        monkeypatch.setenv("DASHBOARD_USE_GLANCES", "true")
        unavailable = {"available": False, "status_message": GLANCES_UNAVAILABLE_LABEL}
        with patch("raven_health_details.fetch_glances_details_snapshot", return_value=unavailable):
            with patch("host_status.run_host_command", return_value=(False, "unavailable")):
                with patch("host_status.run_systemctl", return_value=(False, "unavailable")):
                    response = client.get("/api/raven/health/glances")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "unavailable"
        assert data["glances_available"] is False

    def test_api_includes_history_arrays_when_samples_exist(self, client, tmp_path, monkeypatch):
        from datetime import datetime, timedelta, timezone

        from raven_metrics_history import MetricsSample

        monkeypatch.setenv("DASHBOARD_USE_GLANCES", "true")
        history_path = tmp_path / "history.jsonl"
        now = datetime.now(timezone.utc)
        sample = MetricsSample(
            timestamp=now - timedelta(minutes=10),
            load_1=1.2,
            load_5=1.0,
            load_15=0.8,
            memory_used_percent=55.0,
            memory_used_bytes=4_000_000_000,
            memory_total_bytes=8_000_000_000,
            cpu_percent=33.0,
            cpu_temp_celsius=60.0,
            cpu_total_jiffies=1000,
            cpu_idle_jiffies=700,
            cpu_threads=4,
        )
        history_path.write_text(sample.to_json_line() + "\n", encoding="utf-8")
        monkeypatch.setenv("DASHBOARD_METRICS_HISTORY_PATH", str(history_path))
        monkeypatch.setattr("raven_metrics_history.HISTORY_PATH", history_path)

        with patch("raven_health_details.fetch_glances_details_snapshot", return_value=MOCK_GLANCES):
            with patch("host_status.run_host_command", return_value=(False, "unavailable")):
                with patch("host_status.run_systemctl", return_value=(False, "unavailable")):
                    response = client.get("/api/raven/health/glances")
        data = response.json()
        assert len(data["history"]["cpu_history_1h"]) == 1
        assert len(data["history"]["load_history_1h"]) == 1
        assert len(data["history"]["memory_history_1h"]) == 1


class TestNormalizeGlancesDetails:
    def test_normalize_includes_sections(self):
        payload = normalize_glances_details(
            MOCK_GLANCES,
            raven={"hostname": "raven", "uptime": "1 day"},
            docker_running=3,
            metrics={"peak_cpu_1h": "88%"},
            history={"cpu_1h": [], "load_1h": [], "memory_1h": []},
            updated_at="2026-06-20 12:00:00 UTC",
        )
        assert payload["status"] == "live"
        assert payload["overview"]["load"]["average_display"] == "1.23 / 0.98 / 0.75"
        assert payload["system"]["containers_running"] == 3


class TestHealthEndpointUnchanged:
    def test_health_does_not_call_glances(self, client, monkeypatch):
        monkeypatch.setenv("DASHBOARD_USE_GLANCES", "true")

        def boom():
            raise AssertionError("Glances must not be called from /health")

        with patch("glances_client.fetch_glances_details_snapshot", side_effect=boom):
            with patch("glances_client.fetch_glances_snapshot", side_effect=boom):
                response = client.get("/health")
        assert response.status_code == 200
        assert response.json()["status"] == "ok"

    def test_healthcheck_does_not_require_glances(self):
        dockerfile = (DASHBOARD_DIR / "Dockerfile").read_text(encoding="utf-8")
        assert "curl -sf http://localhost:8088/health" in dockerfile
        assert "glances" not in dockerfile.lower() or "DASHBOARD_GLANCES" not in dockerfile
