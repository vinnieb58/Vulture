"""Tests for Pelican monitor telemetry coverage manifest parsing."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pelican_monitor.telemetry_coverage  # noqa: E402 — regression: must import from repo root
from pelican_monitor.telemetry_coverage import (  # noqa: E402
    evaluate_latest_archive_telemetry,
    evaluate_manifest_telemetry_coverage,
)


SAMPLE_MANIFEST = """\
Pelican Raven Recovery Bundle Manifest
======================================
backup_timestamp: 2026-06-29T00:00:00Z
hostname: raven

telemetry_coverage:
  Aviary long-term telemetry/history files included in this bundle.

long_term_data_catalog:
  - [sqlite/required] data/vulture.db — SQLite

sqlite_databases:
  - /repo/data/vulture.db (integrity=ok)
  - /repo/data/kestrel/kestrel.db (integrity=ok)

jsonl_history:
  - /repo/data/kestrel_nest_history.jsonl

telemetry_snapshots:
  - /repo/data/kestrel/kestrel_status.json

telemetry_config:
  - /repo/devices.json
"""


class TestTelemetryCoverageImport:
    """Regression: pelican_monitor.telemetry_coverage must import cleanly from repo root."""

    def test_module_importable(self) -> None:
        # The import at the top of this file already exercises this path.  This
        # test makes the regression explicit so pytest names it in the report.
        assert pelican_monitor.telemetry_coverage is not None

    def test_function_callable(self) -> None:
        # Verify the imported function from scripts.pelican.telemetry_data is
        # accessible via the module's public surface.
        result = pelican_monitor.telemetry_coverage.evaluate_manifest_telemetry_coverage("")
        assert result is not None


class TestTelemetryCoverageParsing:
    def test_recognizes_covered_manifest(self) -> None:
        status = evaluate_manifest_telemetry_coverage(SAMPLE_MANIFEST)
        assert status.covered
        assert status.sqlite_count == 2
        assert status.jsonl_count == 1
        assert "Telemetry/history backup covered" in status.message

    def test_flags_missing_markers(self) -> None:
        status = evaluate_manifest_telemetry_coverage("backup_timestamp: x\n")
        assert not status.covered
        assert status.missing_markers

    def test_latest_archive_without_manifest(self, tmp_path: Path) -> None:
        status = evaluate_latest_archive_telemetry(tmp_path, "raven-recovery-20260629T000000Z.tar.gz")
        assert not status.covered
        assert "manifest missing" in status.message.lower()
