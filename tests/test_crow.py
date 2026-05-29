"""
tests/test_crow.py

Unit tests for Crow v0.1 check helpers (no Discord, Raven, or tmux required).
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from crow.checks import services, system, vulture
from crow.checks.services import ServiceStatus
from crow.formatting import disk_level, format_bytes, join_lines, truncate
from crow.commands.help import crow_help_text


class TestDiskParsing:
    SAMPLE_DF = """Filesystem     1B-blocks       Used  Available Use% Mounted on
/dev/sda1     100000000000  50000000000  45000000000  50% /
/dev/sdb1     200000000000 180000000000  10000000000  95% /mnt/data
"""

    def test_parse_df_output_two_mounts(self):
        entries = system.parse_df_output(self.SAMPLE_DF)
        assert len(entries) == 2
        root = entries[0]
        assert root.mount == "/"
        assert root.percent_used == 50.0
        assert root.level == "ok"
        data = entries[1]
        assert data.mount == "/mnt/data"
        assert data.percent_used == 95.0
        assert data.level == "critical"

    def test_disk_level_thresholds(self):
        assert disk_level(79) == "ok"
        assert disk_level(80) == "warn"
        assert disk_level(90) == "critical"
        assert disk_level(None) == "unknown"

    def test_format_disk_check_empty(self):
        msg = system.format_disk_check_message([])
        assert "No filesystem" in msg


class TestMemoryParsing:
    SAMPLE_FREE = """              total        used        free      shared  buff/cache   available
Mem:     16000000000  8000000000  2000000000     1000000  6000000000  7000000000
Swap:     4000000000           0   4000000000
"""

    SAMPLE_MEMINFO = """MemTotal:       16384000 kB
MemFree:         2048000 kB
MemAvailable:    7000000 kB
"""

    def test_parse_free_output(self):
        mem = system.parse_free_output(self.SAMPLE_FREE)
        assert mem.total_bytes == 16_000_000_000
        assert mem.used_bytes == 8_000_000_000
        assert mem.available_bytes == 7_000_000_000
        assert mem.percent_used == pytest.approx(50.0)

    def test_parse_meminfo(self):
        mem = system.parse_meminfo(self.SAMPLE_MEMINFO)
        assert mem.total_bytes == 16384000 * 1024
        assert mem.available_bytes == 7000000 * 1024
        assert mem.percent_used is not None

    def test_format_memory_check_message(self):
        mem = system.MemoryInfo(1000, 500, 500, 50.0)
        msg = system.format_memory_check_message(mem)
        assert "Memory check" in msg
        assert "50.0%" in msg


class TestServiceDetection:
    def test_pgrep_running(self):
        with patch("crow.checks.services.subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = "123 python discord_bot.py\n"
            assert services._pgrep_matches("discord_bot.py") == "running"

    def test_pgrep_not_detected(self):
        with patch("crow.checks.services.subprocess.run") as mock_run:
            mock_run.return_value.returncode = 1
            mock_run.return_value.stdout = ""
            assert services._pgrep_matches("discord_bot.py") == "not detected"

    def test_tmux_session_running(self):
        with patch("crow.checks.services.run_command", return_value=(True, "bot: 1 windows\nscheduler: 1 windows")):
            assert services._tmux_session_exists("bot") == "running"
            assert services._tmux_session_exists("missing") == "not detected"

    def test_format_services_message(self):
        msg = services.format_services_message(
            [ServiceStatus("Test", "running", "detail")]
        )
        assert "Test" in msg
        assert "running" in msg


class TestVultureHealth:
    def test_health_when_files_missing(self, tmp_path):
        db = tmp_path / "data" / "vulture.db"
        logs = tmp_path / "logs"
        with patch.object(vulture, "check_scheduler_process") as mock_main:
            with patch.object(vulture, "check_scheduler_tmux") as mock_tmux:
                mock_main.return_value = ServiceStatus("Vulture scheduler (main.py)", "not detected")
                mock_tmux.return_value = ServiceStatus("Scheduler tmux session", "not detected")
                health = vulture.get_vulture_health(db_path=db, logs_dir=logs)
        assert health.db_exists is False
        assert health.logs_dir_exists is False
        msg = vulture.format_vulture_health_message(health)
        assert "missing" in msg

    def test_health_when_files_present(self, tmp_path):
        db = tmp_path / "vulture.db"
        db.write_text("sqlite", encoding="utf-8")
        logs = tmp_path / "logs"
        logs.mkdir()
        log_file = logs / "vulture.log"
        log_file.write_text("run\n", encoding="utf-8")

        with patch.object(vulture, "check_scheduler_process") as mock_main:
            with patch.object(vulture, "check_scheduler_tmux") as mock_tmux:
                mock_main.return_value = ServiceStatus("Vulture scheduler (main.py)", "running")
                mock_tmux.return_value = ServiceStatus("Scheduler tmux session", "not detected")
                health = vulture.get_vulture_health(db_path=db, logs_dir=logs)
        assert health.db_exists is True
        assert health.logs_dir_exists is True
        assert health.main_log_mtime is not None
        assert vulture.scheduler_summary(health) == "running"


class TestFormatting:
    def test_truncate_adds_notice(self):
        long = "x" * 2000
        out = truncate(long, 100)
        assert len(out) <= 120
        assert "truncated" in out

    def test_format_bytes_none(self):
        assert format_bytes(None) == "n/a"

    def test_format_raven_status_missing_fields(self):
        msg = system.format_raven_status_message(
            {
                "hostname": "raven",
                "uptime": "up 1d",
                "memory": "n/a",
                "disk_root": "n/a",
                "load_average": "n/a",
                "timestamp": "2026-01-01 00:00:00 UTC",
            }
        )
        assert "raven" in msg
        assert join_lines([]) == ""

    def test_crow_help_text_mentions_read_only(self):
        text = crow_help_text()
        assert "read-only" in text.lower()
        assert "/raven_status" in text
