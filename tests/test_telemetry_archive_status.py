"""Tests for telemetry archive status script."""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from telemetry_archive_status import main  # noqa: E402


def test_status_reports_oldest_archive_record(tmp_path: Path, capsys) -> None:
    repo = tmp_path / "repo"
    archive = repo / "data" / "telemetry" / "nest_history_archive.jsonl"
    archive.parent.mkdir(parents=True)
    archive.write_text(
        "\n".join(
            [
                '{"timestamp":"2024-01-01T00:00:00+00:00","thermostats":{}}',
                '{"timestamp":"2026-01-01T00:00:00+00:00","thermostats":{}}',
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    rc = main(["--repo-root", str(repo)])
    captured = capsys.readouterr().out
    assert rc == 0
    assert "oldest=2024-01-01T00:00:00+00:00" in captured
    assert "records=2" in captured
