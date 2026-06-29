#!/usr/bin/env python3
"""Dry-run validation for Pelican long-term telemetry backup coverage."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from pelican.config import DEFAULT_DB_PATH, DEFAULT_REPO_ROOT  # noqa: E402
from pelican.telemetry_data import (  # noqa: E402
    discover_long_term_data,
    render_telemetry_catalog,
)


def log_info(message: str) -> None:
    print(f"pelican-backup-verify: INFO: {message}")


def log_warn(message: str) -> None:
    print(f"pelican-backup-verify: WARN: {message}", file=sys.stderr)


def run_verify(repo_root: Path, *, db_path: Path) -> int:
    repo_root = repo_root.resolve()
    if not repo_root.is_dir():
        log_warn(f"Repository root missing: {repo_root}")
        return 1

    inventory = discover_long_term_data(repo_root, primary_db=db_path)
    log_info(f"Repository: {repo_root}")
    log_info(
        "Discovered long-term data: "
        f"{len(inventory.sqlite_files)} SQLite, "
        f"{len(inventory.jsonl_files)} JSONL, "
        f"{len(inventory.snapshot_files)} snapshots, "
        f"{len(inventory.config_files)} config"
    )

    print("\nLong-term data catalog:")
    for line in render_telemetry_catalog(inventory):
        print(line)

    if inventory.missing_optional:
        print("\nOptional sources absent (backup continues without them):")
        for path in inventory.missing_optional:
            print(f"  - {path}")

    required_missing = [
        entry.rel_path
        for entry in inventory.catalog
        if not entry.optional and entry.category == "sqlite"
    ]
    required_missing = [
        rel
        for rel in required_missing
        if not any(str(path).endswith(rel) for path in inventory.sqlite_files)
    ]
    if required_missing:
        log_warn(f"Required SQLite sources missing: {', '.join(required_missing)}")
        return 1

    log_info("Dry-run validation passed — sources are discoverable for Pelican backup")
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=DEFAULT_REPO_ROOT)
    parser.add_argument("--db-path", type=Path, default=DEFAULT_DB_PATH)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    return run_verify(args.repo_root.resolve(), db_path=args.db_path.resolve())


if __name__ == "__main__":
    raise SystemExit(main())
