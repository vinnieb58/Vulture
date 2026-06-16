"""Unit tests for Kestrel SQLite storage."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kestrel.config import PROVIDER_SMART_METER_TEXAS
from kestrel.models import EnergyInterval, hash_identifier, normalize_account_identifier
from kestrel.smart_meter_texas import import_csv_file, parse_csv_content
from kestrel.storage import fetch_intervals, upsert_intervals


FIXTURE_CSV = Path(__file__).resolve().parent / "fixtures" / "kestrel_smt_intervals.csv"
SMT_PORTAL_FIXTURE_CSV = Path(__file__).resolve().parent / "fixtures" / "kestrel_smt_portal_export.csv"


def _sample_interval(start: str, end: str, kwh: float) -> EnergyInterval:
    return EnergyInterval(
        provider=PROVIDER_SMART_METER_TEXAS,
        start_ts=start,
        end_ts=end,
        kwh=kwh,
        account_id_hash=hash_identifier("sample-esiid-0001"),
        raw_source="test",
    )


class TestKestrelStorage:
    def test_upsert_and_fetch(self, tmp_path: Path) -> None:
        db_path = tmp_path / "kestrel.db"
        rows = [
            _sample_interval("2026-06-01T05:00:00+00:00", "2026-06-01T05:15:00+00:00", 0.42),
            _sample_interval("2026-06-01T05:15:00+00:00", "2026-06-01T05:30:00+00:00", 0.38),
        ]
        inserted, skipped = upsert_intervals(db_path, rows)
        assert inserted == 2
        assert skipped == 0

        stored = fetch_intervals(db_path, provider=PROVIDER_SMART_METER_TEXAS)
        assert len(stored) == 2
        assert stored[0].kwh == 0.42

    def test_upsert_dedupes_on_unique_key(self, tmp_path: Path) -> None:
        db_path = tmp_path / "kestrel.db"
        row = _sample_interval("2026-06-01T05:00:00+00:00", "2026-06-01T05:15:00+00:00", 0.42)
        duplicate = _sample_interval("2026-06-01T05:00:00+00:00", "2026-06-01T05:15:00+00:00", 9.99)

        inserted, skipped = upsert_intervals(db_path, [row])
        assert inserted == 1
        assert skipped == 0

        inserted, skipped = upsert_intervals(db_path, [duplicate])
        assert inserted == 0
        assert skipped == 1

        stored = fetch_intervals(db_path)
        assert len(stored) == 1
        assert stored[0].kwh == 0.42

    def test_csv_fixture_import(self, tmp_path: Path) -> None:
        intervals = import_csv_file(FIXTURE_CSV, account_id="sample-esiid-0001")
        assert len(intervals) == 8
        assert intervals[0].kwh == pytest.approx(0.42)
        assert intervals[0].account_id_hash == hash_identifier("sample-esiid-0001")
        assert "esiid" not in (intervals[0].raw_source or "").lower()

        db_path = tmp_path / "kestrel.db"
        inserted, skipped = upsert_intervals(db_path, intervals)
        assert inserted == 8
        assert skipped == 0

    def test_csv_date_time_split_columns(self) -> None:
        content = "Date,Time,kWh\n06/01/2026,01:00,0.55\n"
        rows = parse_csv_content(content, account_id="acct-1")
        assert len(rows) == 1
        assert rows[0].kwh == pytest.approx(0.55)

    def test_hash_identifier_never_returns_raw(self) -> None:
        raw = "1234567890123456789012345678901234567890"
        hashed = hash_identifier(raw)
        assert hashed is not None
        assert raw not in hashed
        assert len(hashed) == 16

    def test_smt_portal_export_fixture(self, tmp_path: Path) -> None:
        intervals = import_csv_file(SMT_PORTAL_FIXTURE_CSV)
        assert len(intervals) == 3
        assert intervals[0].kwh == pytest.approx(0.131)
        assert intervals[0].start_ts == "2026-06-15T05:00:00+00:00"
        assert intervals[0].end_ts == "2026-06-15T05:15:00+00:00"
        assert intervals[0].account_id_hash == hash_identifier("1000000000000000000001")
        assert "1000000000000000000001" not in (intervals[0].raw_source or "")
        assert "est=A" in (intervals[0].raw_source or "")
        assert "type=Consumption" in (intervals[0].raw_source or "")

        db_path = tmp_path / "kestrel.db"
        inserted, skipped = upsert_intervals(db_path, intervals)
        assert inserted == 3
        assert skipped == 0

    def test_smt_esiid_apostrophe_is_hashed_not_stored_raw(self) -> None:
        content = (
            "ESIID,USAGE_DATE,REVISION_DATE,USAGE_START_TIME,USAGE_END_TIME,"
            "USAGE_KWH,ESTIMATED_ACTUAL,CONSUMPTION_SURPLUSGENERATION\n"
            "'1000000000000000000001,06/15/2026,06/15/2026 11:18:06,00:00,00:15,0.131,A,Consumption\n"
        )
        rows = parse_csv_content(content)
        assert len(rows) == 1
        expected_hash = hash_identifier("1000000000000000000001")
        assert rows[0].account_id_hash == expected_hash
        assert hash_identifier("'1000000000000000000001") == expected_hash
        assert normalize_account_identifier("'1000000000000000000001") == "1000000000000000000001"

    def test_smt_midnight_rollover(self) -> None:
        content = (
            "ESIID,USAGE_DATE,REVISION_DATE,USAGE_START_TIME,USAGE_END_TIME,"
            "USAGE_KWH,ESTIMATED_ACTUAL,CONSUMPTION_SURPLUSGENERATION\n"
            "'1000000000000000000001,06/15/2026,06/15/2026 11:18:06,23:45,00:00,0.250,A,Consumption\n"
        )
        rows = parse_csv_content(content)
        assert len(rows) == 1
        assert rows[0].start_ts == "2026-06-16T04:45:00+00:00"
        assert rows[0].end_ts == "2026-06-16T05:00:00+00:00"
        assert rows[0].kwh == pytest.approx(0.25)

    def test_smt_usage_kwh_column_maps_correctly(self) -> None:
        content = (
            "ESIID,USAGE_DATE,REVISION_DATE,USAGE_START_TIME,USAGE_END_TIME,"
            "USAGE_KWH,ESTIMATED_ACTUAL,CONSUMPTION_SURPLUSGENERATION\n"
            "'1000000000000000000001,06/15/2026,06/15/2026 11:18:06,01:00,01:15,1.234,A,Consumption\n"
        )
        rows = parse_csv_content(content)
        assert rows[0].kwh == pytest.approx(1.234)
