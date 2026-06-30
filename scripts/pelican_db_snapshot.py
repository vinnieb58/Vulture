#!/usr/bin/env python3
"""
Pelican Raven database snapshot backup.

Creates a lightweight, compressed snapshot of Raven SQLite databases and small
operational JSON state files under the Pelican backup volume.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import socket
import subprocess
import sys
import tarfile
from dataclasses import dataclass, field
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from pelican.db_snapshot_config import (  # noqa: E402
    DB_SNAPSHOT_SCRIPT_VERSION,
    DEFAULT_MAX_JSON_BYTES,
    DEFAULT_REPO_ROOT,
    DEFAULT_RETENTION_DAYS,
    DEFAULT_SNAPSHOT_TARGET,
    STAGING_DIR_NAME,
)
from pelican.db_snapshot_inventory import classify_snapshot_sources  # noqa: E402
from pelican.db_snapshot_naming import (  # noqa: E402
    complete_snapshot_name,
    format_backup_timestamp,
    incomplete_snapshot_dir_name,
    is_db_snapshot_managed_name,
)
from pelican.db_snapshot_retention import plan_snapshot_retention  # noqa: E402
from pelican.manifest import utc_now_iso  # noqa: E402
from pelican.mount import verify_backup_target  # noqa: E402
from pelican.naming import archive_suffix  # noqa: E402
from pelican.sqlite_backup import backup_and_verify_sqlite  # noqa: E402


@dataclass
class SnapshotContext:
    repo_root: Path
    snapshot_target: Path
    retention_days: int
    max_json_bytes: int
    stamp: str
    snapshot_root_name: str
    archive_suffix: str
    staging_parent: Path
    work_dir: Path
    final_archive: Path
    included_files: list[str] = field(default_factory=list)
    integrity_results: dict[str, str] = field(default_factory=dict)
    archive_checksum: str = ""
    published: bool = False


def log_info(message: str) -> None:
    print(f"pelican-db-snapshot: INFO: {message}")


def log_error(message: str) -> None:
    print(f"pelican-db-snapshot: ERROR: {message}", file=sys.stderr)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def create_archive(source_dir: Path, archive_path: Path) -> None:
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    temp_archive = archive_path.with_suffix(archive_path.suffix + ".partial")
    if temp_archive.exists():
        temp_archive.unlink()

    if archive_path.name.endswith(".tar.zst") and shutil.which("zstd"):
        tar_proc = subprocess.run(
            ["tar", "-cf", "-", "-C", str(source_dir.parent), source_dir.name],
            capture_output=True,
            check=False,
        )
        if tar_proc.returncode != 0:
            raise RuntimeError(tar_proc.stderr.decode() or "tar create failed")
        zstd_proc = subprocess.run(
            ["zstd", "-q", "-o", str(temp_archive), "-"],
            input=tar_proc.stdout,
            capture_output=True,
            check=False,
        )
        if zstd_proc.returncode != 0:
            raise RuntimeError(zstd_proc.stderr.decode() or "zstd compression failed")
    else:
        with tarfile.open(temp_archive, "w:gz") as tar:
            tar.add(source_dir, arcname=source_dir.name)

    temp_archive.rename(archive_path)


def copy_json_state(rel_path: Path, source: Path, dest_root: Path, included: list[str]) -> None:
    dest = dest_root / rel_path
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, dest)
    included.append(str(dest))


def write_integrity_manifest(dest_root: Path, results: dict[str, str], included: list[str]) -> None:
    lines = [
        f"pelican-db-snapshot version: {DB_SNAPSHOT_SCRIPT_VERSION}",
        "",
    ]
    for rel_name in sorted(results):
        lines.append(f"{rel_name}: {results[rel_name]}")
    manifest = dest_root / "integrity_checks.txt"
    manifest.write_text("\n".join(lines) + "\n", encoding="utf-8")
    included.append(str(manifest))


def write_snapshot_manifest(
    dest_root: Path,
    *,
    timestamp_iso: str,
    hostname: str,
    repo_root: Path,
    snapshot_target: Path,
    archive_name: str,
    included_files: list[str],
    skipped_json: list[str],
) -> None:
    lines = [
        "Pelican Raven database snapshot",
        f"timestamp: {timestamp_iso}",
        f"hostname: {hostname}",
        f"script_version: {DB_SNAPSHOT_SCRIPT_VERSION}",
        f"repo_root: {repo_root}",
        f"snapshot_target: {snapshot_target}",
        f"archive_name: {archive_name}",
        "",
        "included_files:",
    ]
    lines.extend(f"  - {path}" for path in included_files)
    if skipped_json:
        lines.append("")
        lines.append("skipped_json:")
        lines.extend(f"  - {note}" for note in skipped_json)
    manifest = dest_root / "SNAPSHOT.txt"
    manifest.write_text("\n".join(lines) + "\n", encoding="utf-8")
    included_files.append(str(manifest))


def apply_retention(ctx: SnapshotContext) -> None:
    plan = plan_snapshot_retention(
        ctx.snapshot_target,
        retention_days=ctx.retention_days,
        pending_new_snapshot=ctx.final_archive,
    )
    for old in plan.delete:
        if not is_db_snapshot_managed_name(old.name):
            continue
        log_info(f"Retention: removing snapshot older than {ctx.retention_days} days: {old.name}")
        old.unlink(missing_ok=True)
        Path(f"{old}.sha256").unlink(missing_ok=True)


def build_context(
    *,
    repo_root: Path,
    snapshot_target: Path,
    retention_days: int,
    max_json_bytes: int,
    stamp: str | None = None,
    prefer_zstd: bool = True,
) -> SnapshotContext:
    backup_stamp = stamp or format_backup_timestamp()
    suffix = archive_suffix(prefer_zstd=prefer_zstd)
    root_name = f"raven-db-snapshot-{backup_stamp}"
    staging_parent = snapshot_target / STAGING_DIR_NAME
    work_dir = staging_parent / incomplete_snapshot_dir_name(backup_stamp)
    final_name = complete_snapshot_name(backup_stamp, prefer_zstd=prefer_zstd)
    return SnapshotContext(
        repo_root=repo_root,
        snapshot_target=snapshot_target,
        retention_days=retention_days,
        max_json_bytes=max_json_bytes,
        stamp=backup_stamp,
        snapshot_root_name=root_name,
        archive_suffix=suffix,
        staging_parent=staging_parent,
        work_dir=work_dir,
        final_archive=snapshot_target / final_name,
    )


def run_snapshot(ctx: SnapshotContext, *, skip_retention: bool = False) -> int:
    hostname = socket.gethostname()
    timestamp_iso = utc_now_iso()

    mount = verify_backup_target(ctx.snapshot_target.parent)
    if not mount.ok:
        log_error(mount.message)
        return 1
    log_info(mount.message)

    inventory = classify_snapshot_sources(
        ctx.repo_root,
        max_json_bytes=ctx.max_json_bytes,
    )
    for note in inventory.skipped_json:
        log_info(note)

    if inventory.missing_required:
        for failure in inventory.missing_required:
            log_error(failure)
        return 1

    if not inventory.sqlite_sources:
        log_error("No SQLite databases found under data/*.db or data/kestrel/*.db")
        return 1

    if ctx.work_dir.exists():
        shutil.rmtree(ctx.work_dir)
    ctx.staging_parent.mkdir(parents=True, exist_ok=True)
    ctx.work_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(ctx.work_dir, 0o700)

    snapshot_contents = ctx.work_dir / ctx.snapshot_root_name
    snapshot_contents.mkdir(parents=True, exist_ok=True)
    partial_archive = ctx.staging_parent / f"{ctx.snapshot_root_name}{ctx.archive_suffix}.partial"
    ctx.published = False
    companion_sha: Path | None = None

    try:
        for rel_path, source in inventory.sqlite_sources:
            dest = snapshot_contents / rel_path
            result = backup_and_verify_sqlite(source, dest)
            rel_name = str(rel_path)
            ctx.integrity_results[rel_name] = result.integrity_result
            if not result.ok:
                log_error(f"{rel_name}: {result.message}")
                return 1
            ctx.included_files.append(str(dest))
            log_info(f"Backed up {rel_name}: {result.message}")

        for rel_path, source in inventory.json_sources:
            copy_json_state(rel_path, source, snapshot_contents, ctx.included_files)
            log_info(f"Included JSON state file: {rel_path}")

        write_integrity_manifest(snapshot_contents, ctx.integrity_results, ctx.included_files)
        write_snapshot_manifest(
            snapshot_contents,
            timestamp_iso=timestamp_iso,
            hostname=hostname,
            repo_root=ctx.repo_root,
            snapshot_target=ctx.snapshot_target,
            archive_name=ctx.final_archive.name,
            included_files=ctx.included_files,
            skipped_json=inventory.skipped_json,
        )

        create_archive(snapshot_contents, partial_archive)
        ctx.archive_checksum = sha256_file(partial_archive)

        if ctx.final_archive.exists():
            log_error(f"Refusing to overwrite existing snapshot: {ctx.final_archive}")
            return 1

        partial_archive.rename(ctx.final_archive)
        os.chmod(ctx.final_archive, 0o600)

        companion_sha = Path(f"{ctx.final_archive}.sha256")
        companion_sha.write_text(f"{ctx.archive_checksum}  {ctx.final_archive.name}\n", encoding="utf-8")
        os.chmod(companion_sha, 0o600)
        ctx.published = True
        log_info(f"Published database snapshot: {ctx.final_archive}")
        log_info(f"Archive sha256: {ctx.archive_checksum}")

        if not skip_retention:
            apply_retention(ctx)

        log_info("Pelican database snapshot completed successfully")
        return 0
    except Exception as exc:  # noqa: BLE001
        log_error(str(exc))
        return 1
    finally:
        if ctx.work_dir.exists():
            shutil.rmtree(ctx.work_dir, ignore_errors=True)
        if partial_archive.exists() and not ctx.published:
            partial_archive.unlink(missing_ok=True)
        if not ctx.published and ctx.final_archive.exists():
            ctx.final_archive.unlink(missing_ok=True)
        if not ctx.published and companion_sha and companion_sha.exists():
            companion_sha.unlink(missing_ok=True)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Pelican Raven database snapshot backup")
    parser.add_argument("--repo-root", type=Path, default=DEFAULT_REPO_ROOT)
    parser.add_argument("--snapshot-target", type=Path, default=DEFAULT_SNAPSHOT_TARGET)
    parser.add_argument("--retention-days", type=int, default=DEFAULT_RETENTION_DAYS)
    parser.add_argument("--max-json-bytes", type=int, default=DEFAULT_MAX_JSON_BYTES)
    parser.add_argument(
        "--skip-retention",
        action="store_true",
        help="Skip deleting snapshots older than the retention window after a successful run",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    ctx = build_context(
        repo_root=args.repo_root.resolve(),
        snapshot_target=args.snapshot_target.resolve(),
        retention_days=args.retention_days,
        max_json_bytes=args.max_json_bytes,
    )
    return run_snapshot(ctx, skip_retention=args.skip_retention)


if __name__ == "__main__":
    sys.exit(main())
