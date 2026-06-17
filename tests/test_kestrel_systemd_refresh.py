"""Tests for Kestrel systemd timer and --no-browser-fallback."""

from __future__ import annotations

import importlib.util
import sys
from datetime import date
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PROBE_FILE = ROOT / "experiments" / "kestrel" / "smart_meter_texas_probe.py"
_spec = importlib.util.spec_from_file_location("smart_meter_texas_probe", PROBE_FILE)
assert _spec and _spec.loader
probe_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(probe_module)
from kestrel.config import KestrelConfig, PROVIDER_SMART_METER_TEXAS
from kestrel.live_refresh import RefreshMetadata, run_live_refresh
from kestrel.models import EnergyInterval, hash_identifier
from kestrel.smart_meter_texas import FetchIntervalsResult, SmartMeterTexasError

DEPLOY_DIR = ROOT / "deploy" / "systemd"
SERVICE_FILE = DEPLOY_DIR / "kestrel-smt-refresh.service"
TIMER_FILE = DEPLOY_DIR / "kestrel-smt-refresh.timer"
DOCS_FILE = ROOT / "docs" / "current" / "KESTREL_OPERATIONS.md"
INSTALL_SCRIPT = ROOT / "scripts" / "install_kestrel_timer.sh"


def _config(tmp_path: Path) -> KestrelConfig:
    return KestrelConfig(
        enabled=True,
        smt_username="test-user",
        smt_password="secret-password",
        smt_account_id="1000000000000000000001",
        data_dir=tmp_path,
        db_path=tmp_path / "kestrel.db",
        lookback_days=7,
        headless=True,
        log_level="WARNING",
        timezone="America/Chicago",
    )


def _intervals() -> list[EnergyInterval]:
    return [
        EnergyInterval(
            provider=PROVIDER_SMART_METER_TEXAS,
            start_ts="2026-06-15T05:00:00+00:00",
            end_ts="2026-06-15T05:15:00+00:00",
            kwh=0.42,
            account_id_hash=hash_identifier("1000000000000000000001"),
            raw_source="smt_portal_api",
        )
    ]


def _fetch_result(
    intervals: list[EnergyInterval],
    *,
    failed_days: list[date] | None = None,
    failed_messages: list[str] | None = None,
) -> FetchIntervalsResult:
    return FetchIntervalsResult(
        intervals=intervals,
        data_lag_days=[],
        failed_days=failed_days or [],
        failed_messages=failed_messages or [],
    )


class TestNoBrowserFallbackCLI:
    def test_parse_args_supports_no_browser_fallback(self) -> None:
        with patch.object(sys, "argv", ["probe", "--live-refresh", "--no-browser-fallback"]):
            args = probe_module.parse_args()
        assert args.live_refresh is True
        assert args.no_browser_fallback is True

    def test_no_browser_fallback_requires_live_refresh(self, capsys) -> None:
        with patch.object(sys, "argv", ["probe", "--no-browser-fallback"]):
            code = probe_module.main()
        assert code == 1
        assert "--no-browser-fallback requires --live-refresh" in capsys.readouterr().err


class TestNoBrowserFallbackBehavior:
    def test_browser_not_invoked_when_flag_set(self, tmp_path: Path) -> None:
        config = _config(tmp_path)
        with (
            patch(
                "kestrel.live_refresh.fetch_intervals_by_day",
                return_value=_fetch_result(
                    [],
                    failed_days=[date(2026, 6, 15)],
                    failed_messages=["API unavailable"],
                ),
            ),
            patch("kestrel.live_refresh.fetch_intervals_via_browser") as browser_mock,
        ):
            result = run_live_refresh(
                config,
                start=date(2026, 6, 15),
                end=date(2026, 6, 15),
                no_browser_fallback=True,
            )
        browser_mock.assert_not_called()
        assert result.metadata.status == "failed"
        assert "Browser fallback disabled" in (result.metadata.message or "")

    def test_browser_still_used_without_flag(self, tmp_path: Path) -> None:
        config = _config(tmp_path)
        intervals = _intervals()
        with (
            patch(
                "kestrel.live_refresh.fetch_intervals_by_day",
                return_value=_fetch_result(
                    [],
                    failed_days=[date(2026, 6, 15)],
                    failed_messages=["API unavailable"],
                ),
            ),
            patch(
                "kestrel.live_refresh.fetch_intervals_via_browser",
                return_value=intervals,
            ) as browser_mock,
        ):
            result = run_live_refresh(
                config,
                start=date(2026, 6, 15),
                end=date(2026, 6, 15),
            )
        browser_mock.assert_called_once()
        assert result.metadata.source == "live_browser"


class TestProbeExitCodes:
    def _run_live_main(self, metadata: RefreshMetadata) -> int:
        intervals = _intervals()
        result = type(
            "Result",
            (),
            {
                "metadata": metadata,
                "intervals": intervals,
                "inserted": 1,
                "skipped": 0,
                "attempted_start": date(2026, 6, 15),
                "attempted_end": date(2026, 6, 16),
                "min_interval_ts": intervals[0].start_ts,
                "max_interval_ts": intervals[0].end_ts,
                "fetched_total_kwh": 0.42,
                "fetched_peak": None,
            },
        )()
        with (
            patch.object(sys, "argv", ["probe", "--live-refresh", "--days", "2", "--no-browser-fallback"]),
            patch.object(probe_module, "load_config", return_value=_config(Path("/tmp/kestrel-test"))),
            patch.object(probe_module, "setup_logging"),
            patch.object(probe_module, "run_live_refresh", return_value=result),
            patch.object(probe_module, "load_stored_intervals", return_value=intervals),
            patch.object(probe_module, "summarize_intervals"),
            patch.object(probe_module, "top_intervals", return_value=[]),
            patch.object(probe_module, "build_status_snapshot", return_value={}),
            patch.object(probe_module.Path, "write_text"),
            patch.object(probe_module.Path, "mkdir"),
            patch.object(probe_module, "_print_summary"),
            patch.object(probe_module, "load_refresh_metadata_from_status", return_value=None),
        ):
            return probe_module.main()

    def test_ok_status_exits_zero(self, tmp_path: Path) -> None:
        meta = RefreshMetadata(
            attempt_at="2026-06-17T12:00:00+00:00",
            success_at="2026-06-17T12:00:01+00:00",
            source="live_api",
            status="ok",
            message="Fetched 96 intervals",
        )
        assert self._run_live_main(meta) == 0

    def test_partial_status_exits_zero(self, tmp_path: Path) -> None:
        meta = RefreshMetadata(
            attempt_at="2026-06-17T12:00:00+00:00",
            success_at="2026-06-17T12:00:01+00:00",
            source="live_api",
            status="partial",
            message="Latest day unavailable (likely TDSP lag)",
        )
        assert self._run_live_main(meta) == 0

    def test_failed_status_exits_nonzero(self, tmp_path: Path) -> None:
        meta = RefreshMetadata(
            attempt_at="2026-06-17T12:00:00+00:00",
            success_at=None,
            source=None,
            status="failed",
            message="API setup failed",
        )
        assert self._run_live_main(meta) == 1


class TestSystemdUnits:
    def test_service_unit_has_required_fields(self) -> None:
        text = SERVICE_FILE.read_text(encoding="utf-8")
        assert "Type=oneshot" in text
        assert "WorkingDirectory=/home/vinnieb58/projects/vulture" in text
        assert "EnvironmentFile=/home/vinnieb58/projects/vulture/.env" in text
        assert "--live-refresh --days 3 --no-browser-fallback" in text
        assert "User=vinnieb58" in text
        assert "Group=vinnieb58" in text
        assert "NoNewPrivileges=true" in text
        assert "TimeoutStartSec=180" in text
        assert "Restart=no" in text

    def test_timer_unit_has_persistent_schedule(self) -> None:
        text = TIMER_FILE.read_text(encoding="utf-8")
        assert "OnCalendar=*-*-* 08:30:00" in text
        assert "Persistent=true" in text
        assert "RandomizedDelaySec=15m" in text
        assert "Unit=kestrel-smt-refresh.service" in text
        assert "WantedBy=timers.target" in text

    def test_units_do_not_reference_secrets(self) -> None:
        combined = SERVICE_FILE.read_text(encoding="utf-8") + TIMER_FILE.read_text(encoding="utf-8")
        for forbidden in ("PASSWORD=", "USERNAME=", "KESTREL_SMT_PASSWORD", "token", "esiid"):
            assert forbidden.lower() not in combined.lower()


class TestOperatorDocs:
    def test_operations_doc_has_install_and_rollback(self) -> None:
        text = DOCS_FILE.read_text(encoding="utf-8")
        assert "kestrel-smt-refresh.service" in text
        assert "systemctl enable --now kestrel-smt-refresh.timer" in text
        assert "journalctl -u kestrel-smt-refresh.service" in text
        assert "systemctl disable --now kestrel-smt-refresh.timer" in text
        for forbidden in ("KESTREL_SMT_PASSWORD=", "password123", "hunter2"):
            assert forbidden not in text

    def test_install_script_references_unit_files(self) -> None:
        text = INSTALL_SCRIPT.read_text(encoding="utf-8")
        assert "cat .env" not in text
        assert "KESTREL_SMT_PASSWORD" not in text
        assert "kestrel-smt-refresh.service" in text
        assert "kestrel-smt-refresh.timer" in text

    def test_install_script_is_executable(self) -> None:
        assert INSTALL_SCRIPT.exists()
