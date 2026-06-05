"""
tests/test_crow_logs.py

Unit tests for Crow /check logs helpers (no Discord or live journald).
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from crow.commands.help import CHECK_SUBCOMMANDS, crow_help_text
from crow.embeds import build_logs_embed
from crow.system.logs import (
    get_logs_summary,
    sanitize_log_line,
    sanitize_log_text,
)


class TestCrowHelpCheckSubcommands:
    @pytest.mark.parametrize("subcommand", CHECK_SUBCOMMANDS)
    def test_crow_help_lists_check_subcommand(self, subcommand: str) -> None:
        text = crow_help_text()
        assert f"/check {subcommand}" in text

    def test_crow_help_has_system_checks_section(self) -> None:
        text = crow_help_text()
        assert "Raven / system checks" in text
        assert "Legacy v0.1 commands" in text


class TestLogSanitizer:
    def test_redacts_discord_token(self) -> None:
        token = f"{'A' * 24}.{'B' * 6}.{'C' * 27}"
        line = f"auth failed with token {token}"
        sanitized = sanitize_log_line(line)
        assert token not in sanitized
        assert "[REDACTED_TOKEN]" in sanitized

    def test_redacts_env_style_secrets(self) -> None:
        line = "config DISCORD_BOT_TOKEN=supersecret API_KEY=abc123 PASSWORD=hunter2"
        sanitized = sanitize_log_line(line)
        assert "supersecret" not in sanitized
        assert "abc123" not in sanitized
        assert "hunter2" not in sanitized
        assert "DISCORD_BOT_TOKEN=[REDACTED]" in sanitized

    def test_redacts_bearer_and_authorization(self) -> None:
        line = "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.payload.sig"
        sanitized = sanitize_log_line(line)
        assert "eyJhbGciOiJIUzI1NiJ9" not in sanitized
        assert "[REDACTED]" in sanitized

    def test_redacts_webhook_urls(self) -> None:
        url = "https://discord.com/api/webhooks/123456789/abcdefghijklmnop"
        sanitized = sanitize_log_line(f"posting to {url}")
        assert url not in sanitized
        assert "[REDACTED_WEBHOOK]" in sanitized

    def test_redacts_url_query_secrets(self) -> None:
        line = "GET https://example.com/api?token=secret123&other=1"
        sanitized = sanitize_log_line(line)
        assert "secret123" not in sanitized
        assert "token=[REDACTED]" in sanitized

    def test_sanitize_log_text_multiline(self) -> None:
        text = "TOKEN=abc\nnormal line\n"
        sanitized = sanitize_log_text(text)
        assert "abc" not in sanitized
        assert "normal line" in sanitized


class TestLogsSummary:
    SAMPLE_LOG = """2026-06-01 10:00:00,000 [INFO] __main__: Starting Vulture Discord bot
2026-06-01 10:00:01,000 [INFO] __main__: Vulture bot ready — logged in as CrowBot
2026-06-01 10:05:00,000 [INFO] main: Starting Vulture hunt cycle
2026-06-01 10:05:30,000 [WARNING] adapters.ebay: rate limited
2026-06-01 10:05:45,000 [ERROR] engine.hunt: hunt failed DISCORD_BOT_TOKEN=leak
2026-06-01 10:06:00,000 [INFO] main: Hunt cycle completed
"""

    def test_missing_log_files_do_not_crash(self, tmp_path: Path) -> None:
        logs_dir = tmp_path / "logs"
        main_log = logs_dir / "vulture.log"
        with patch("crow.system.logs.get_journal_excerpt", return_value=None):
            summary = get_logs_summary(main_log=main_log, logs_dir=logs_dir)
        assert summary.warning_count == 0
        assert summary.error_count == 0
        assert any(source.status == "missing" for source in summary.sources)

    def test_normal_log_summary_counts_and_detects_lines(self, tmp_path: Path) -> None:
        logs_dir = tmp_path / "logs"
        logs_dir.mkdir()
        main_log = logs_dir / "vulture.log"
        main_log.write_text(self.SAMPLE_LOG, encoding="utf-8")

        with patch("crow.system.logs.get_journal_excerpt", return_value=None):
            summary = get_logs_summary(main_log=main_log, logs_dir=logs_dir)

        assert summary.warning_count == 1
        assert summary.error_count == 1
        assert summary.last_cycle_line is not None
        assert "Hunt cycle completed" in summary.last_cycle_line
        assert summary.last_bot_startup_line is not None
        assert "Starting Vulture Discord bot" in summary.last_bot_startup_line
        assert all("leak" not in line for line in summary.recent_issues)
        assert any(source.status == "ok" for source in summary.sources)

    def test_logs_embed_is_stable_and_safe(self, tmp_path: Path) -> None:
        logs_dir = tmp_path / "logs"
        logs_dir.mkdir()
        main_log = logs_dir / "vulture.log"
        main_log.write_text(self.SAMPLE_LOG, encoding="utf-8")

        with patch("crow.system.logs.get_journal_excerpt", return_value=None):
            summary = get_logs_summary(main_log=main_log, logs_dir=logs_dir)

        embed = build_logs_embed(summary)
        payload = embed.to_dict()
        serialized = str(payload)
        assert "leak" not in serialized
        assert embed.title == "Logs"
        assert any(field.name == "Log sources" for field in embed.fields)
