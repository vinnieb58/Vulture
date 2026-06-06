"""Unit tests for dashboard host command helpers."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "dashboard"))

from host_commands import (  # noqa: E402
    _tool_missing,
    command_strategies,
    run_systemctl,
)


class TestHostCommands:
    def test_tool_missing_detects_systemctl_absence(self):
        assert _tool_missing("command not found: systemctl")
        assert not _tool_missing("active")

    def test_command_strategies_prefers_chroot_when_host_root_present(self, tmp_path):
        host_root = tmp_path / "host"
        (host_root / "usr/bin").mkdir(parents=True)
        (host_root / "usr/bin/systemctl").write_text("", encoding="utf-8")
        with patch("host_commands.HOST_ROOT", host_root):
            strategies = command_strategies(["systemctl", "is-active", "ssh.service"])
        assert strategies
        assert strategies[0][:2] == ["chroot", str(host_root)]

    def test_run_systemctl_parses_inactive_state(self):
        with patch("host_commands.command_strategies", return_value=[["systemctl", "is-active", "ssh"]]):
            with patch("host_commands._run_raw", return_value=(3, "inactive")):
                ok, state = run_systemctl(["is-active", "ssh"])
        assert ok is True
        assert state == "inactive"
