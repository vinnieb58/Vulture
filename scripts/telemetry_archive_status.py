#!/usr/bin/env python3
"""Report oldest records in long-term telemetry archives."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from kestrel.telemetry_retention import (  # noqa: E402
    archive_record_count,
    nest_archive_path,
    oldest_archive_timestamp,
    raven_metrics_archive_path,
    tuya_archive_path,
)


def _report(label: str, path: Path) -> None:
    oldest = oldest_archive_timestamp(path)
    count = archive_record_count(path)
    if oldest is None:
        print(f"{label}: {path} — (empty or missing), records=0")
        return
    print(
        f"{label}: {path} — oldest={oldest.isoformat()}, records={count}"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=ROOT,
        help="Repository root for resolving default archive paths",
    )
    args = parser.parse_args(argv)
    repo_root = args.repo_root.resolve()

    def resolve(path: Path) -> Path:
        if path.is_absolute():
            return path
        return (repo_root / path).resolve()

    print("Long-term telemetry archives:")
    _report("Nest", resolve(nest_archive_path()))
    _report("Tuya power", resolve(tuya_archive_path()))
    _report("Raven metrics", resolve(raven_metrics_archive_path()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
