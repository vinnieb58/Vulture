"""Tests for Kestrel live refresh, redaction, and API normalization."""

from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kestrel.config import KestrelConfig, KestrelConfigError, PROVIDER_SMART_METER_TEXAS, load_config
from kestrel.live_refresh import (
    RefreshMetadata,
    build_csv_import_metadata,
    load_refresh_metadata_from_status,
    run_live_refresh,
)
from kestrel.models import EnergyInterval, hash_identifier
from kestrel.redact import describe_payload_shape, redact_text
from kestrel.smart_meter_texas import parse_interval_synch_payload
from kestrel.status_snapshot import build_status_snapshot
from kestrel.storage import upsert_intervals
from kestrel.summarize import summarize_intervals, top_intervals

FIXTURE_JSON = Path(__file__).resolve().parent / "fixtures" / "kestrel_smt_interval_synch.json"
FIXTURE_CSV = Path(__file__).resolve().parent / "fixtures" / "kestrel_smt_intervals.csv"


def _config(tmp_path: Path, *, enabled: bool = True, with_credentials: bool = True) -> KestrelConfig:
    return KestrelConfig(
        enabled=enabled,
        smt_username="test-user" if with_credentials else None,
        smt_password="secret-password" if with_credentials else None,
        smt_account_id="1000000000000000000001" if with_credentials else None,
        data_dir=tmp_path,
        db_path=tmp_path / "kestrel.db",
        lookback_days=7,
        headless=True,
        log_level="WARNING",
        timezone="America/Chicago",
    )


class TestRedaction:
    def test_redacts_bearer_tokens(self) -> None:
        token = "Bearer eyJhbGciOiJIUzI1NiJ9.payload.signature"
        redacted = redact_text(f"Authorization: {token}")
        assert "eyJhbGciOiJIUzI1NiJ9" not in redacted
        assert "[REDACTED]" in redacted

    def test_redacts_esiid_and_paths(self) -> None:
        esiid = "1000000000000000000001"
        redacted = redact_text(f"account {esiid} saved to /home/user/secret/export.csv")
        assert esiid not in redacted
        assert "/home/user" not in redacted
        assert "[REDACTED_ESIID]" in redacted
        assert "[REDACTED_PATH]" in redacted

    def test_describe_payload_shape_is_safe(self) -> None:
        payload = {
            "token": "eyJhbGciOiJIUzI1NiJ9.payload.signature",
            "data": {
                "ESIID": "1000000000000000000001",
                "energyData": [{"RT": "C", "RD": "0.1,0.2"}],
            },
        }
        shape = describe_payload_shape(payload)
        assert "1000000000000000000001" not in shape
        assert "eyJhbGciOiJIUzI1NiJ9" not in shape


class TestIntervalSynchNormalization:
    def test_fixture_parses_to_energy_intervals(self) -> None:
        payload = json.loads(FIXTURE_JSON.read_text(encoding="utf-8"))
        day = date(2026, 6, 15)
        intervals = parse_interval_synch_payload(
            payload,
            day=day,
            tz_name="America/Chicago",
            account_id="1000000000000000000001",
        )
        assert len(intervals) == 8
        assert intervals[0].kwh == pytest.approx(0.131)
        assert intervals[0].start_ts == "2026-06-15T05:00:00+00:00"
        assert intervals[0].provider == PROVIDER_SMART_METER_TEXAS
        assert intervals[0].account_id_hash == hash_identifier("1000000000000000000001")
        assert intervals[0].raw_source == "smt_portal_api"
        assert "1000000000000000000001" not in json.dumps(
            {k: getattr(intervals[0], k) for k in intervals[0].__dataclass_fields__}
        )


class TestLiveRefresh:
    def _sample_intervals(self) -> list[EnergyInterval]:
        return [
            EnergyInterval(
                provider=PROVIDER_SMART_METER_TEXAS,
                start_ts="2026-06-15T05:00:00+00:00",
                end_ts="2026-06-15T05:15:00+00:00",
                kwh=0.42,
                account_id_hash=hash_identifier("1000000000000000000001"),
                raw_source="smt_portal_api",
                created_at="2026-06-17T12:00:00+00:00",
            ),
            EnergyInterval(
                provider=PROVIDER_SMART_METER_TEXAS,
                start_ts="2026-06-15T05:15:00+00:00",
                end_ts="2026-06-15T05:30:00+00:00",
                kwh=0.38,
                account_id_hash=hash_identifier("1000000000000000000001"),
                raw_source="smt_portal_api",
                created_at="2026-06-17T12:00:00+00:00",
            ),
        ]

    def test_live_refresh_upserts_and_dedupes(self, tmp_path: Path) -> None:
        config = _config(tmp_path)
        intervals = self._sample_intervals()

        with patch("kestrel.live_refresh.fetch_intervals", return_value=intervals):
            first = run_live_refresh(config, start=date(2026, 6, 15), end=date(2026, 6, 15))
        assert first.metadata.status == "ok"
        assert first.metadata.source == "live_api"
        assert first.inserted == 2
        assert first.skipped == 0

        with patch("kestrel.live_refresh.fetch_intervals", return_value=intervals):
            second = run_live_refresh(config, start=date(2026, 6, 15), end=date(2026, 6, 15))
        assert second.inserted == 0
        assert second.skipped == 2

    def test_dry_run_skips_db_write(self, tmp_path: Path) -> None:
        config = _config(tmp_path)
        intervals = self._sample_intervals()
        with patch("kestrel.live_refresh.fetch_intervals", return_value=intervals):
            result = run_live_refresh(
                config,
                start=date(2026, 6, 15),
                end=date(2026, 6, 15),
                dry_run=True,
            )
        assert result.metadata.status == "ok"
        assert "Dry run" in (result.metadata.message or "")
        assert not config.db_path.exists()

    def test_api_failure_falls_back_to_browser(self, tmp_path: Path) -> None:
        config = _config(tmp_path)
        intervals = self._sample_intervals()
        from kestrel.smart_meter_texas import SmartMeterTexasError

        with (
            patch(
                "kestrel.live_refresh.fetch_intervals",
                side_effect=SmartMeterTexasError("API unavailable"),
            ),
            patch(
                "kestrel.live_refresh.fetch_intervals_via_browser",
                return_value=intervals,
            ) as browser_mock,
        ):
            result = run_live_refresh(config, start=date(2026, 6, 15), end=date(2026, 6, 15))
        browser_mock.assert_called_once()
        assert result.metadata.source == "live_browser"
        assert result.metadata.status == "ok"

    def test_both_paths_fail_returns_unsupported(self, tmp_path: Path) -> None:
        config = _config(tmp_path)
        from kestrel.smart_meter_texas import SmartMeterTexasError

        with (
            patch(
                "kestrel.live_refresh.fetch_intervals",
                side_effect=SmartMeterTexasError("API unavailable"),
            ),
            patch(
                "kestrel.live_refresh.fetch_intervals_via_browser",
                side_effect=SmartMeterTexasError("Browser blocked: CAPTCHA"),
            ),
        ):
            result = run_live_refresh(config, start=date(2026, 6, 15), end=date(2026, 6, 15))
        assert result.metadata.status == "failed"
        assert result.inserted == 0
        assert "CAPTCHA" in (result.metadata.message or "")
        assert "secret-password" not in (result.metadata.message or "")

    def test_missing_credentials_returns_failed_without_db_write(self, tmp_path: Path) -> None:
        config = _config(tmp_path, with_credentials=False)
        result = run_live_refresh(config, start=date(2026, 6, 15), end=date(2026, 6, 15))
        assert result.metadata.status in ("failed", "unsupported")
        assert result.inserted == 0
        assert not config.db_path.exists()


class TestConfigGating:
    def test_enabled_false_blocks_live_refresh_config(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("KESTREL_ENABLED", "false")
        monkeypatch.setenv("KESTREL_SMT_USERNAME", "user")
        monkeypatch.setenv("KESTREL_SMT_PASSWORD", "pass")
        with pytest.raises(KestrelConfigError, match="disabled"):
            load_config(require_enabled=True, require_credentials=True)

    def test_csv_import_does_not_require_enabled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("KESTREL_ENABLED", "false")
        monkeypatch.delenv("KESTREL_SMT_USERNAME", raising=False)
        monkeypatch.delenv("KESTREL_SMT_PASSWORD", raising=False)
        config = load_config(require_enabled=False, require_credentials=False)
        assert config.enabled is False


class TestRefreshStatusSnapshot:
    def test_status_includes_safe_refresh_fields(self, tmp_path: Path) -> None:
        rows = [
            EnergyInterval(
                provider=PROVIDER_SMART_METER_TEXAS,
                start_ts="2026-06-01T05:00:00+00:00",
                end_ts="2026-06-01T05:15:00+00:00",
                kwh=1.0,
            )
        ]
        summary = summarize_intervals(rows)
        refresh = RefreshMetadata(
            attempt_at="2026-06-17T12:00:00+00:00",
            success_at="2026-06-17T12:00:01+00:00",
            source="live_api",
            status="ok",
            message="Fetched 96 intervals for 2026-06-10..2026-06-17",
        )
        snapshot = build_status_snapshot(
            summary,
            top_intervals(rows, 1),
            provider=PROVIDER_SMART_METER_TEXAS,
            refresh=refresh,
        )
        assert snapshot["last_refresh_status"] == "ok"
        assert snapshot["last_refresh_source"] == "live_api"
        assert snapshot["last_refresh_attempt_at"] == "2026-06-17T12:00:00+00:00"
        text = json.dumps(snapshot)
        for forbidden in ("password", "username", "esiid", "1000000000000000000001", "token"):
            assert forbidden not in text.lower()

    def test_csv_import_metadata(self) -> None:
        meta = build_csv_import_metadata(imported=10, inserted=8, skipped=2)
        assert meta.source == "csv_import"
        assert meta.status == "ok"
        assert "8 inserted" in (meta.message or "")

    def test_load_refresh_metadata_round_trip(self, tmp_path: Path) -> None:
        status_path = tmp_path / "kestrel_status.json"
        status_path.write_text(
            json.dumps(
                {
                    "last_refresh_attempt_at": "2026-06-17T12:00:00+00:00",
                    "last_refresh_success_at": "2026-06-17T12:00:01+00:00",
                    "last_refresh_source": "live_api",
                    "last_refresh_status": "ok",
                    "last_refresh_message": "Fetched 10 intervals",
                }
            ),
            encoding="utf-8",
        )
        loaded = load_refresh_metadata_from_status(status_path)
        assert loaded is not None
        assert loaded.source == "live_api"
        assert loaded.status == "ok"


class TestCsvImportStillWorks:
    def test_existing_csv_fixture_import(self, tmp_path: Path) -> None:
        from kestrel.smart_meter_texas import import_csv_file

        intervals = import_csv_file(FIXTURE_CSV, account_id="sample-esiid-0001")
        inserted, skipped = upsert_intervals(tmp_path / "kestrel.db", intervals)
        assert len(intervals) == 8
        assert inserted == 8
        assert skipped == 0


class TestDashboardRefreshStatus:
    def test_dashboard_strips_sensitive_refresh_message(self, tmp_path: Path) -> None:
        from dashboard.kestrel_status import read_kestrel_status

        status_path = tmp_path / "kestrel_status.json"
        status_path.write_text(
            json.dumps(
                {
                    "status": "available",
                    "interval_count": 10,
                    "total_kwh": 5.0,
                    "last_refresh_status": "failed",
                    "last_refresh_message": "API: failed for ESIID 1000000000000000000001",
                    "password": "hunter2",
                    "esiid": "1000000000000000000001",
                    "account_id_hash": "abc123deadbeef",
                }
            ),
            encoding="utf-8",
        )
        with patch("dashboard.kestrel_status.KESTREL_STATUS_PATH", status_path):
            result = read_kestrel_status()
        text = json.dumps(result)
        assert "hunter2" not in text
        assert "1000000000000000000001" not in text
        assert result["last_refresh_status"] == "failed"

    def test_dashboard_page_hides_sensitive_refresh_fields(self, tmp_path: Path) -> None:
        ROOT = Path(__file__).resolve().parent.parent
        DASHBOARD_DIR = ROOT / "dashboard"
        sys.path.insert(0, str(DASHBOARD_DIR))
        import app as dashboard_app  # noqa: E402
        from fastapi.testclient import TestClient

        status_path = tmp_path / "kestrel_status.json"
        status_path.write_text(
            json.dumps(
                {
                    "status": "available",
                    "interval_count": 1,
                    "total_kwh": 1.0,
                    "last_refresh_status": "failed",
                    "last_refresh_message": "Browser blocked",
                    "esiid": "1000000000000000000001",
                    "token": "eyJhbGciOiJIUzI1NiJ9.payload.signature",
                }
            ),
            encoding="utf-8",
        )
        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setattr(dashboard_app, "DB_PATH", tmp_path / "missing.db")
        monkeypatch.setattr(dashboard_app, "LOG_PATH", tmp_path / "missing.log")
        monkeypatch.setattr("db_readers.DB_PATH", tmp_path / "missing.db")
        monkeypatch.setattr("log_readers.LOG_PATH", tmp_path / "missing.log")
        monkeypatch.setattr("vulture_runtime.LOG_PATH", tmp_path / "missing.log")
        monkeypatch.setattr("kestrel_status.KESTREL_STATUS_PATH", status_path)
        monkeypatch.setattr("kestrel_metrics.KESTREL_DB_PATH", tmp_path / "missing.db")
        try:
            client = TestClient(dashboard_app.app)
            with patch("host_status.run_host_command", return_value=(False, "unavailable")):
                with patch("host_status.run_systemctl", return_value=(False, "unavailable")):
                    response = client.get("/kestrel")
            text = response.text
            assert response.status_code == 200
            for forbidden in (
                "1000000000000000000001",
                "eyJhbGciOiJIUzI1NiJ9",
                "payload.signature",
            ):
                assert forbidden not in text
        finally:
            monkeypatch.undo()
