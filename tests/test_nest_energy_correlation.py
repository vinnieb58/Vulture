"""Tests for energy interval + Nest HVAC correlation helper."""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DASHBOARD_DIR = ROOT / "dashboard"
sys.path.insert(0, str(DASHBOARD_DIR))
sys.path.insert(0, str(ROOT))

from kestrel.config import PROVIDER_SMART_METER_TEXAS  # noqa: E402
from kestrel.models import EnergyInterval  # noqa: E402
from kestrel.nest_history import append_history_from_snapshot  # noqa: E402
from kestrel.storage import init_db, upsert_intervals  # noqa: E402
from nest_energy_correlation import (  # noqa: E402
    STATUS_AVAILABLE,
    STATUS_EMPTY_SMT,
    STATUS_NO_NEST_HISTORY,
    STATUS_NO_OVERLAP,
    STATUS_NO_ROWS_IN_WINDOW,
    STATUS_NO_SMT_DB,
    WARNING_NO_OVERLAP,
    _compute_correlation_window,
    correlate_energy_intervals,
    diagnose_energy_hvac_correlation,
    get_energy_hvac_correlation,
)
from nest_history import NestHistoryRecord  # noqa: E402


def _seed_energy(
    db_path: Path,
    *,
    start_ts: str = "2026-06-19T18:00:00+00:00",
) -> None:
    rows = [
        EnergyInterval(
            provider=PROVIDER_SMART_METER_TEXAS,
            start_ts=start_ts,
            end_ts="2026-06-19T18:15:00+00:00",
            kwh=1.2,
        ),
        EnergyInterval(
            provider=PROVIDER_SMART_METER_TEXAS,
            start_ts="2026-06-19T18:15:00+00:00",
            end_ts="2026-06-19T18:30:00+00:00",
            kwh=1.4,
        ),
        EnergyInterval(
            provider=PROVIDER_SMART_METER_TEXAS,
            start_ts="2026-06-19T18:30:00+00:00",
            end_ts="2026-06-19T18:45:00+00:00",
            kwh=0.4,
        ),
    ]
    init_db(db_path)
    upsert_intervals(db_path, rows)


def _append_nest_history(
    history_path: Path,
    *,
    updated_at: str = "2026-06-19T18:10:00+00:00",
    extra_samples: tuple[str, ...] = (
        "2026-06-19T18:20:00+00:00",
        "2026-06-19T18:40:00+00:00",
        "2026-06-19T18:45:00+00:00",
    ),
) -> None:
    timestamps = (updated_at, *extra_samples)
    for sample_at in timestamps:
        append_history_from_snapshot(
            {
                "updated_at": sample_at,
                "thermostats": {
                    "downstairs": {"action": "COOLING", "online": True},
                    "upstairs": {"action": "OFF", "online": True},
                },
            },
            path=history_path,
        )


class TestEnergyHvacCorrelation:
    def test_correlate_sample_interval_data(self) -> None:
        nest_records = [
            NestHistoryRecord(
                timestamp=datetime(2026, 6, 19, 18, 5, tzinfo=timezone.utc),
                thermostats={
                    "downstairs": {"action": "COOLING"},
                    "upstairs": {"action": "OFF"},
                },
            ),
            NestHistoryRecord(
                timestamp=datetime(2026, 6, 19, 18, 20, tzinfo=timezone.utc),
                thermostats={
                    "downstairs": {"action": "COOLING"},
                    "upstairs": {"action": "OFF"},
                },
            ),
            NestHistoryRecord(
                timestamp=datetime(2026, 6, 19, 18, 35, tzinfo=timezone.utc),
                thermostats={
                    "downstairs": {"action": "OFF"},
                    "upstairs": {"action": "OFF"},
                },
            ),
        ]
        energy_rows = [
            {
                "start_ts": "2026-06-19T18:00:00+00:00",
                "end_ts": "2026-06-19T18:15:00+00:00",
                "kwh": 1.2,
            },
            {
                "start_ts": "2026-06-19T18:15:00+00:00",
                "end_ts": "2026-06-19T18:30:00+00:00",
                "kwh": 1.4,
            },
            {
                "start_ts": "2026-06-19T18:30:00+00:00",
                "end_ts": "2026-06-19T18:45:00+00:00",
                "kwh": 0.4,
            },
        ]

        rows = correlate_energy_intervals(energy_rows, nest_records)
        assert len(rows) == 3
        assert rows[0]["cooling_display"] == "yes"
        assert rows[1]["cooling_display"] == "yes"
        assert rows[2]["cooling_display"] == "no"
        assert rows[0]["note"] is not None
        assert "High usage" in rows[0]["note"]
        assert rows[2]["note"] is None

    def test_no_smt_db_reports_missing_database(self, tmp_path: Path, monkeypatch) -> None:
        missing_db = tmp_path / "missing.db"
        history_path = tmp_path / "history.jsonl"
        monkeypatch.setattr("kestrel_metrics.KESTREL_DB_PATH", missing_db)
        monkeypatch.setattr("nest_energy_correlation.NEST_HISTORY_PATH", history_path)
        _append_nest_history(history_path)

        now = datetime(2026, 6, 19, 19, 0, tzinfo=timezone.utc)
        result = get_energy_hvac_correlation(history_path=history_path, now=now)
        assert result["status"] == STATUS_NO_SMT_DB
        assert result["available"] is False
        assert "database not found" in (result["warning"] or "").lower()

    def test_empty_smt_table_reports_no_interval_data(self, tmp_path: Path, monkeypatch) -> None:
        db_path = tmp_path / "kestrel.db"
        history_path = tmp_path / "history.jsonl"
        init_db(db_path)
        monkeypatch.setattr("kestrel_metrics.KESTREL_DB_PATH", db_path)
        monkeypatch.setattr("nest_energy_correlation.NEST_HISTORY_PATH", history_path)
        _append_nest_history(history_path)

        now = datetime(2026, 6, 19, 19, 0, tzinfo=timezone.utc)
        result = get_energy_hvac_correlation(history_path=history_path, now=now)
        assert result["status"] == STATUS_EMPTY_SMT
        assert result["available"] is False
        assert "no smart meter texas interval data" in (result["warning"] or "").lower()

    def test_smt_rows_outside_nest_history_window_reports_no_overlap(
        self,
        tmp_path: Path,
        monkeypatch,
    ) -> None:
        db_path = tmp_path / "kestrel.db"
        history_path = tmp_path / "history.jsonl"
        init_db(db_path)
        upsert_intervals(
            db_path,
            [
                EnergyInterval(
                    provider=PROVIDER_SMART_METER_TEXAS,
                    start_ts="2026-06-18T04:30:00+00:00",
                    end_ts="2026-06-18T04:45:00+00:00",
                    kwh=0.8,
                ),
            ],
        )
        monkeypatch.setattr("kestrel_metrics.KESTREL_DB_PATH", db_path)
        monkeypatch.setattr("nest_energy_correlation.NEST_HISTORY_PATH", history_path)
        _append_nest_history(history_path, updated_at="2026-06-19T16:22:03+00:00")

        now = datetime(2026, 6, 19, 19, 0, tzinfo=timezone.utc)
        result = get_energy_hvac_correlation(history_path=history_path, now=now, hours=24)

        assert result["status"] == STATUS_NO_OVERLAP
        assert result["available"] is False
        assert result["warning"] == WARNING_NO_OVERLAP
        assert "no smart meter texas interval data" not in (result["warning"] or "").lower()
        diagnostics = result["diagnostics"]
        assert diagnostics["smt_latest"] == "2026-06-18T04:45:00+00:00"
        assert diagnostics["nest_earliest"] == "2026-06-19T16:22:03+00:00"
        assert diagnostics["window_start"]
        assert diagnostics["window_end"]
        assert diagnostics["interval_count"] == 1

    def test_overlapping_smt_and_nest_rows_are_correlated(
        self,
        tmp_path: Path,
        monkeypatch,
    ) -> None:
        db_path = tmp_path / "kestrel.db"
        history_path = tmp_path / "history.jsonl"
        _seed_energy(db_path)
        monkeypatch.setattr("kestrel_metrics.KESTREL_DB_PATH", db_path)
        monkeypatch.setattr("nest_energy_correlation.NEST_HISTORY_PATH", history_path)
        _append_nest_history(history_path)

        now = datetime(2026, 6, 19, 19, 0, tzinfo=timezone.utc)
        result = get_energy_hvac_correlation(history_path=history_path, now=now, hours=24)

        assert result["status"] == STATUS_AVAILABLE
        assert result["available"] is True
        assert result["rows"]
        diagnostics = result["diagnostics"]
        assert diagnostics["smt_latest"] == "2026-06-19T18:45:00+00:00"
        assert diagnostics["nest_earliest"] == "2026-06-19T18:10:00+00:00"
        assert diagnostics["overlap_end"] == "2026-06-19T18:45:00+00:00"
        assert diagnostics["smt_rows_in_window"] == 2

    def test_missing_nest_history_reports_unavailable(self, tmp_path: Path, monkeypatch) -> None:
        db_path = tmp_path / "kestrel.db"
        _seed_energy(db_path)
        monkeypatch.setattr("kestrel_metrics.KESTREL_DB_PATH", db_path)
        missing = tmp_path / "missing.jsonl"
        now = datetime(2026, 6, 19, 19, 0, tzinfo=timezone.utc)
        result = get_energy_hvac_correlation(history_path=missing, now=now)
        assert result["status"] == STATUS_NO_NEST_HISTORY
        assert result["available"] is False
        assert result["warning"]
        assert result["diagnostics"]["interval_count"] == 3

    def test_latest_overlap_succeeds_when_strict_last_24h_would_fail(
        self,
        tmp_path: Path,
        monkeypatch,
    ) -> None:
        """SMT lags behind current time but overlaps Nest history."""
        db_path = tmp_path / "kestrel.db"
        history_path = tmp_path / "history.jsonl"
        _seed_energy(db_path, start_ts="2026-06-19T18:00:00+00:00")
        monkeypatch.setattr("kestrel_metrics.KESTREL_DB_PATH", db_path)
        monkeypatch.setattr("nest_energy_correlation.NEST_HISTORY_PATH", history_path)
        _append_nest_history(
            history_path,
            updated_at="2026-06-19T18:10:00+00:00",
            extra_samples=("2026-06-19T18:20:00+00:00", "2026-06-19T18:45:00+00:00", "2026-06-23T12:00:00+00:00"),
        )

        now = datetime(2026, 6, 23, 12, 0, tzinfo=timezone.utc)
        result = get_energy_hvac_correlation(history_path=history_path, now=now, hours=24)

        assert result["status"] == STATUS_AVAILABLE
        assert result["available"] is True
        assert result["rows"]
        diagnostics = result["diagnostics"]
        assert diagnostics["smt_latest"] == "2026-06-19T18:45:00+00:00"
        assert diagnostics["nest_latest"] == "2026-06-23T12:00:00+00:00"
        assert diagnostics["overlap_end"] == "2026-06-19T18:45:00+00:00"
        assert diagnostics["smt_rows_in_window"] == 2
        assert diagnostics["nest_samples_in_window"] >= 2

    def test_compute_correlation_window_anchors_on_latest_common_overlap(self) -> None:
        smt_start = datetime(2025, 6, 27, 5, 0, tzinfo=timezone.utc)
        smt_end = datetime(2026, 6, 21, 4, 45, tzinfo=timezone.utc)
        nest_start = datetime(2026, 6, 19, 0, 0, tzinfo=timezone.utc)
        nest_end = datetime(2026, 6, 23, 12, 0, tzinfo=timezone.utc)

        window = _compute_correlation_window(
            smt_start=smt_start,
            smt_end=smt_end,
            nest_start=nest_start,
            nest_end=nest_end,
            hours=24,
        )
        assert window is not None
        overlap_start, overlap_end, window_start, window_end = window
        assert window_end == datetime(2026, 6, 21, 4, 45, tzinfo=timezone.utc)
        assert window_start == datetime(2026, 6, 20, 4, 45, tzinfo=timezone.utc)
        assert overlap_start == window_start
        assert overlap_end == window_end

    def test_diagnose_energy_hvac_correlation_reports_window_counts(
        self,
        tmp_path: Path,
        monkeypatch,
    ) -> None:
        db_path = tmp_path / "kestrel.db"
        history_path = tmp_path / "history.jsonl"
        _seed_energy(db_path)
        monkeypatch.setattr("kestrel_metrics.KESTREL_DB_PATH", db_path)
        monkeypatch.setattr("nest_energy_correlation.NEST_HISTORY_PATH", history_path)
        _append_nest_history(history_path)

        now = datetime(2026, 6, 19, 19, 0, tzinfo=timezone.utc)
        payload = diagnose_energy_hvac_correlation(history_path=history_path, now=now)

        assert payload["status"] == STATUS_AVAILABLE
        assert payload["window_start"]
        assert payload["window_end"]
        assert payload["overlap_start"]
        assert payload["overlap_end"]
        assert payload["smt_rows_in_window"] == 2
        assert payload["nest_samples_in_window"] == 3
        assert payload["matched_correlation_rows"] == 2
        assert payload["correlation_rows"] == 2

    def test_no_rows_in_window_when_overlap_is_too_narrow(
        self,
        tmp_path: Path,
        monkeypatch,
    ) -> None:
        db_path = tmp_path / "kestrel.db"
        history_path = tmp_path / "history.jsonl"
        init_db(db_path)
        upsert_intervals(
            db_path,
            [
                EnergyInterval(
                    provider=PROVIDER_SMART_METER_TEXAS,
                    start_ts="2026-06-19T18:00:00+00:00",
                    end_ts="2026-06-19T18:15:00+00:00",
                    kwh=0.8,
                ),
            ],
        )
        monkeypatch.setattr("kestrel_metrics.KESTREL_DB_PATH", db_path)
        monkeypatch.setattr("nest_energy_correlation.NEST_HISTORY_PATH", history_path)
        _append_nest_history(
            history_path,
            updated_at="2026-06-19T18:10:00+00:00",
            extra_samples=(),
        )

        now = datetime(2026, 6, 19, 19, 0, tzinfo=timezone.utc)
        result = get_energy_hvac_correlation(history_path=history_path, now=now, hours=24)

        assert result["status"] == STATUS_NO_ROWS_IN_WINDOW
        assert result["available"] is False
        diagnostics = result["diagnostics"]
        assert diagnostics["overlap_start"] == "2026-06-19T18:10:00+00:00"
        assert diagnostics["overlap_end"] == "2026-06-19T18:10:00+00:00"
