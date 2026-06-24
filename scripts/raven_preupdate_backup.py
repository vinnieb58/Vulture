#!/usr/bin/env python3
"""Small pre-update backup of critical Raven mutable state."""

from __future__ import annotations

import argparse
import re
import shutil
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from pelican.mount import verify_backup_target  # noqa: E402
from pelican.naming import format_backup_timestamp, stamp_sort_key  # noqa: E402

DEFAULT_PELICAN_TARGET = Path("/mnt/storage/pelican_backup")
DEFAULT_PREUPDATE_SUBDIR = "raven-preupdate"
DEFAULT_RETENTION_COUNT = 20
BACKUP_DIR_RE = re.compile(r"^raven-preupdate-(?P<stamp>\d{8}T\d{6}Z)$")
EXCLUDED_DATA_FILENAMES = frozenset({"raven_metrics_history.jsonl"})


@dataclass(frozen=True)
class PreupdateBackupResult:
    ok: bool
    backup_path: Path | None
    files_included: int
    pruned_count: int
    message: str


def collect_source_files(repo_root: Path) -> list[Path]:
    """Return repo-relative paths for critical mutable state."""
    repo_root = repo_root.resolve()
    candidates: list[Path] = []

    env_path = repo_root / ".env"
    if env_path.is_file():
        candidates.append(env_path)

    data_dir = repo_root / "data"
    if data_dir.is_dir():
        for pattern in ("*.db", "*tokens*.json", "*ledger*.db"):
            candidates.extend(sorted(data_dir.glob(pattern)))

        kestrel_dir = data_dir / "kestrel"
        if kestrel_dir.is_dir():
            candidates.extend(sorted(kestrel_dir.glob("*.db")))

        for name in ("kestrel_nest_status.json", "kestrel_nest_history.jsonl"):
            path = data_dir / name
            if path.is_file():
                candidates.append(path)

    seen: set[Path] = set()
    included: list[Path] = []
    for path in candidates:
        resolved = path.resolve()
        if resolved in seen:
            continue
        if resolved.name in EXCLUDED_DATA_FILENAMES:
            continue
        if "logs" in resolved.parts or "__pycache__" in resolved.parts:
            continue
        seen.add(resolved)
        included.append(resolved)

    included.sort(key=lambda p: p.relative_to(repo_root).as_posix())
    return included


def list_completed_preupdate_backups(parent_dir: Path) -> list[Path]:
    if not parent_dir.is_dir():
        return []
    backups = [
        entry
        for entry in parent_dir.iterdir()
        if entry.is_dir() and BACKUP_DIR_RE.match(entry.name)
    ]
    backups.sort(
        key=lambda p: stamp_sort_key(BACKUP_DIR_RE.match(p.name).group("stamp"))  # type: ignore[union-attr]
    )
    return backups


def plan_preupdate_retention(
    parent_dir: Path,
    *,
    retain_count: int,
    pending_new_backup: Path | None = None,
) -> tuple[list[Path], list[Path]]:
    if retain_count < 1:
        raise ValueError("retain_count must be at least 1")

    existing = list_completed_preupdate_backups(parent_dir)
    combined = list(existing)
    if pending_new_backup is not None and pending_new_backup not in existing:
        combined.append(pending_new_backup)
        combined.sort(
            key=lambda p: stamp_sort_key(BACKUP_DIR_RE.match(p.name).group("stamp"))  # type: ignore[union-attr]
        )

    if not combined:
        return [], []

    keep = combined[-retain_count:]
    keep_names = {path.name for path in keep}
    delete = [path for path in existing if path.name not in keep_names]
    return keep, delete


def _verify_writable_backup_parent(pelican_target: Path) -> tuple[bool, str]:
    mount = verify_backup_target(pelican_target)
    if not mount.ok:
        return False, mount.message

    preupdate_parent = pelican_target / DEFAULT_PREUPDATE_SUBDIR
    try:
        preupdate_parent.mkdir(parents=True, exist_ok=True)
        probe = preupdate_parent / ".write-probe"
        probe.write_text("ok\n", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        return False, f"pre-update backup directory is not writable: {exc}"

    return True, "pre-update backup target verified"


def run_preupdate_backup(
    repo_root: Path,
    *,
    pelican_target: Path = DEFAULT_PELICAN_TARGET,
    retention_count: int = DEFAULT_RETENTION_COUNT,
    timestamp: datetime | None = None,
) -> PreupdateBackupResult:
    repo_root = repo_root.resolve()
    writable_ok, writable_message = _verify_writable_backup_parent(pelican_target)
    if not writable_ok:
        return PreupdateBackupResult(
            ok=False,
            backup_path=None,
            files_included=0,
            pruned_count=0,
            message=writable_message,
        )

    sources = collect_source_files(repo_root)
    stamp = format_backup_timestamp(timestamp)
    backup_parent = pelican_target / DEFAULT_PREUPDATE_SUBDIR
    backup_dir = backup_parent / f"raven-preupdate-{stamp}"
    if backup_dir.exists():
        return PreupdateBackupResult(
            ok=False,
            backup_path=None,
            files_included=0,
            pruned_count=0,
            message=f"backup directory already exists: {backup_dir}",
        )

    backup_dir.mkdir(parents=True, exist_ok=False)
    files_included = 0
    try:
        for source in sources:
            rel = source.relative_to(repo_root)
            dest = backup_dir / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, dest)
            files_included += 1
    except OSError as exc:
        shutil.rmtree(backup_dir, ignore_errors=True)
        return PreupdateBackupResult(
            ok=False,
            backup_path=None,
            files_included=0,
            pruned_count=0,
            message=f"failed while copying pre-update backup files: {exc}",
        )

    _, delete = plan_preupdate_retention(
        backup_parent,
        retain_count=retention_count,
        pending_new_backup=backup_dir,
    )
    pruned_count = 0
    for old_backup in delete:
        try:
            shutil.rmtree(old_backup)
            pruned_count += 1
        except OSError as exc:
            return PreupdateBackupResult(
                ok=False,
                backup_path=backup_dir,
                files_included=files_included,
                pruned_count=pruned_count,
                message=f"backup created but failed pruning {old_backup}: {exc}",
            )

    return PreupdateBackupResult(
        ok=True,
        backup_path=backup_dir,
        files_included=files_included,
        pruned_count=pruned_count,
        message="pre-update backup completed",
    )


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path.cwd(),
        help="Vulture repository root (default: current directory)",
    )
    parser.add_argument(
        "--pelican-target",
        type=Path,
        default=DEFAULT_PELICAN_TARGET,
        help="Pelican backup mountpoint (default: /mnt/storage/pelican_backup)",
    )
    parser.add_argument(
        "--retention-count",
        type=int,
        default=DEFAULT_RETENTION_COUNT,
        help="Number of pre-update backups to retain (default: 20)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    result = run_preupdate_backup(
        args.repo_root,
        pelican_target=args.pelican_target,
        retention_count=args.retention_count,
    )
    if result.ok:
        print(f"raven-preupdate-backup: INFO: {result.message}")
        print(f"raven-preupdate-backup: INFO: Backup path: {result.backup_path}")
        print(f"raven-preupdate-backup: INFO: Files included: {result.files_included}")
        print(f"raven-preupdate-backup: INFO: Pruned older backups: {result.pruned_count}")
        return 0

    print(f"raven-preupdate-backup: WARNING: {result.message}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
