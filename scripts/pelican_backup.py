#!/usr/bin/env python3
"""
Pelican v1 — Raven recovery bundle backup (Step 1).

Creates a timestamped recovery bundle on the Pelican backup volume.
Read-only with respect to production state except for writing backup artifacts.
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

from pelican.config import (  # noqa: E402
    DEFAULT_BACKUP_TARGET,
    DEFAULT_DB_PATH,
    DEFAULT_ENV_PATH,
    DEFAULT_REPO_ROOT,
    DEFAULT_RETENTION_COUNT,
    STAGING_DIR_NAME,
)
from pelican.inventory import (  # noqa: E402
    InventoryResult,
    classify_required_paths,
    copy_optional_host_config,
    copy_recovery_docs,
    copy_repo_docker_compose,
    copy_repo_systemd_defs,
    should_exclude_repo_path,
)
from pelican.manifest import ManifestData, TelemetryCoverage, render_manifest, utc_now_iso, write_manifest  # noqa: E402
from pelican.mount import verify_backup_target  # noqa: E402
from pelican.naming import (  # noqa: E402
    archive_suffix,
    build_backup_basename,
    complete_bundle_name,
    format_backup_timestamp,
    incomplete_dir_name,
    is_pelican_managed_name,
)
from pelican.retention import plan_retention  # noqa: E402
from pelican.sqlite_backup import backup_and_verify_sqlite, verify_concert_tables  # noqa: E402
from pelican.telemetry_data import (  # noqa: E402
    backup_telemetry_data,
    discover_long_term_data,
    render_telemetry_catalog,
)


@dataclass
class BackupContext:
    repo_root: Path
    backup_target: Path
    db_path: Path
    env_path: Path
    retention_count: int
    stamp: str
    bundle_root_name: str
    archive_suffix: str
    staging_parent: Path
    work_dir: Path
    final_archive: Path
    inventory: InventoryResult = field(default_factory=InventoryResult)
    git_branch: str = ""
    git_commit: str = ""
    sqlite_integrity: str = ""
    sqlite_integrity_all: dict[str, str] = field(default_factory=dict)
    concert_table_counts: dict[str, int] = field(default_factory=dict)
    archive_checksum: str = ""
    published: bool = False


def log_info(message: str) -> None:
    print(f"pelican-backup: INFO: {message}")


def log_error(message: str) -> None:
    print(f"pelican-backup: ERROR: {message}", file=sys.stderr)


def run_git(repo_root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo_root), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def capture_git_recovery(
    repo_root: Path, dest_dir: Path, inventory: InventoryResult
) -> tuple[str, str]:
    dest_dir.mkdir(parents=True, exist_ok=True)
    branch = "unknown"
    commit = "unknown"

    branch_proc = run_git(repo_root, "rev-parse", "--abbrev-ref", "HEAD")
    if branch_proc.returncode == 0 and branch_proc.stdout.strip():
        branch = branch_proc.stdout.strip()

    commit_proc = run_git(repo_root, "rev-parse", "HEAD")
    if commit_proc.returncode == 0 and commit_proc.stdout.strip():
        commit = commit_proc.stdout.strip()

    sections = [
        ("branch.txt", branch),
        ("commit.txt", commit),
        ("status.txt", run_git(repo_root, "status", "--short", "--branch")),
        ("remotes.txt", run_git(repo_root, "remote", "-v")),
        ("recent-log.txt", run_git(repo_root, "log", "-n", "25", "--oneline", "--decorate")),
    ]
    for filename, proc in sections[2:]:
        text = proc.stdout if proc.returncode == 0 else proc.stderr
        path = dest_dir / filename
        path.write_text(text or "", encoding="utf-8")
        inventory.included.append(str(path))

    for filename, text in sections[:2]:
        path = dest_dir / filename
        path.write_text(text, encoding="utf-8")
        inventory.included.append(str(path))

    bundle_path = dest_dir / "vulture.bundle"
    bundle_proc = run_git(repo_root, "bundle", "create", str(bundle_path), "--all")
    if bundle_proc.returncode != 0:
        bundle_proc = run_git(repo_root, "bundle", "create", str(bundle_path), "HEAD")
    if bundle_proc.returncode == 0 and bundle_path.is_file():
        inventory.included.append(str(bundle_path))
    else:
        raise RuntimeError(
            "git bundle creation failed: "
            + (bundle_proc.stderr.strip() or bundle_proc.stdout.strip() or "unknown error")
        )

    return branch, commit


def _git_tracked_files(repo_root: Path) -> list[Path]:
    proc = run_git(repo_root, "ls-files", "-z")
    if proc.returncode != 0:
        raise RuntimeError(f"git ls-files failed: {proc.stderr.strip()}")
    return [Path(p) for p in proc.stdout.split("\0") if p]


def _git_untracked_files(repo_root: Path) -> list[Path]:
    proc = run_git(repo_root, "ls-files", "-z", "--others", "--exclude-standard")
    if proc.returncode != 0:
        return []
    return [Path(p) for p in proc.stdout.split("\0") if p]


def capture_repository_tree(repo_root: Path, dest_dir: Path, inventory: InventoryResult) -> None:
    dest_dir.mkdir(parents=True, exist_ok=True)
    for rel in _git_tracked_files(repo_root):
        if should_exclude_repo_path(rel):
            continue
        src = repo_root / rel
        if not src.is_file():
            continue
        dst = dest_dir / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        inventory.included.append(str(dst))

    for rel in _git_untracked_files(repo_root):
        if should_exclude_repo_path(rel):
            continue
        src = repo_root / rel
        if not src.is_file():
            continue
        dst = dest_dir / "untracked" / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        inventory.included.append(str(dst))


def copy_secrets(env_path: Path, dest_path: Path, inventory: InventoryResult) -> None:
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(env_path, dest_path)
    os.chmod(dest_path, 0o600)
    inventory.included.append(str(dest_path))
    log_info("Copied secrets file into bundle (contents not logged)")


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


def apply_retention(ctx: BackupContext) -> None:
    plan = plan_retention(
        ctx.backup_target,
        retain_count=ctx.retention_count,
        pending_new_bundle=ctx.final_archive,
    )
    for old in plan.delete:
        if not is_pelican_managed_name(old.name):
            continue
        log_info(f"Retention: removing old bundle {old.name}")
        old.unlink(missing_ok=True)


def build_context(
    *,
    repo_root: Path,
    backup_target: Path,
    db_path: Path,
    env_path: Path,
    retention_count: int,
    stamp: str | None = None,
    prefer_zstd: bool = True,
) -> BackupContext:
    backup_stamp = stamp or format_backup_timestamp()
    bundle_root = build_backup_basename(backup_stamp)
    suffix = archive_suffix(prefer_zstd=prefer_zstd)
    staging_parent = backup_target / STAGING_DIR_NAME
    work_dir = staging_parent / incomplete_dir_name(backup_stamp)
    final_name = complete_bundle_name(backup_stamp, prefer_zstd=prefer_zstd)
    return BackupContext(
        repo_root=repo_root,
        backup_target=backup_target,
        db_path=db_path,
        env_path=env_path,
        retention_count=retention_count,
        stamp=backup_stamp,
        bundle_root_name=bundle_root,
        archive_suffix=suffix,
        staging_parent=staging_parent,
        work_dir=work_dir,
        final_archive=backup_target / final_name,
    )


def run_backup(ctx: BackupContext, *, skip_retention: bool = False) -> int:
    hostname = socket.gethostname()
    timestamp_iso = utc_now_iso()

    mount = verify_backup_target(ctx.backup_target)
    if not mount.ok:
        log_error(mount.message)
        return 1
    log_info(mount.message)

    required = classify_required_paths(
        repo_root=ctx.repo_root,
        db_path=ctx.db_path,
        env_path=ctx.env_path,
    )
    if required.required_failures:
        for failure in required.required_failures:
            log_error(failure)
        return 1

    if ctx.work_dir.exists():
        shutil.rmtree(ctx.work_dir)
    ctx.staging_parent.mkdir(parents=True, exist_ok=True)
    ctx.work_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(ctx.work_dir, 0o700)

    bundle_contents = ctx.work_dir / ctx.bundle_root_name
    bundle_contents.mkdir(parents=True, exist_ok=True)
    partial_archive = ctx.staging_parent / f"{ctx.bundle_root_name}{ctx.archive_suffix}.partial"
    ctx.published = False
    companion_sha: Path | None = None
    companion_manifest: Path | None = None

    try:
        git_dir = bundle_contents / "git"
        ctx.git_branch, ctx.git_commit = capture_git_recovery(
            ctx.repo_root, git_dir, ctx.inventory
        )
        log_info(f"Captured git recovery data ({ctx.git_branch} @ {ctx.git_commit[:12]})")

        repo_tree = bundle_contents / "repository"
        capture_repository_tree(ctx.repo_root, repo_tree, ctx.inventory)
        log_info("Captured repository working tree")

        db_dest = bundle_contents / "database" / "vulture.db"
        sqlite_result = backup_and_verify_sqlite(ctx.db_path, db_dest)
        if not sqlite_result.ok:
            log_error(sqlite_result.message)
            return 1
        ctx.sqlite_integrity = sqlite_result.integrity_result
        ctx.sqlite_integrity_all[str(ctx.db_path)] = sqlite_result.integrity_result
        integrity_note = bundle_contents / "database" / "integrity_check.txt"
        integrity_note.write_text(sqlite_result.integrity_result, encoding="utf-8")
        ctx.inventory.included.extend([str(db_dest), str(integrity_note)])
        log_info(sqlite_result.message)

        concert_verify = verify_concert_tables(db_dest)
        ctx.concert_table_counts = dict(concert_verify.counts)
        if concert_verify.ok:
            log_info(concert_verify.message)
        else:
            log_error(concert_verify.message)
            return 1

        telemetry_inventory = discover_long_term_data(ctx.repo_root, primary_db=ctx.db_path)
        telemetry_dest = bundle_contents
        telemetry_result = backup_telemetry_data(
            ctx.repo_root,
            dest_root=telemetry_dest,
            primary_db=ctx.db_path,
            inventory=telemetry_inventory,
        )
        if not telemetry_result.ok:
            for failure in telemetry_result.failures:
                log_error(failure)
            return 1
        for sqlite_backup in telemetry_result.sqlite_results:
            rel = sqlite_backup.source.as_posix()
            ctx.sqlite_integrity_all[rel] = sqlite_backup.integrity_result
            log_info(sqlite_backup.message)
        for jsonl_backup in telemetry_result.jsonl_results:
            if jsonl_backup.source_nonempty:
                log_info(f"JSONL history verified: {jsonl_backup.rel_path}")
        ctx.inventory.included.extend(telemetry_result.copied_files)
        ctx.inventory.missing_optional.extend(telemetry_result.missing_optional)
        log_info(
            "Captured long-term telemetry data "
            f"({len(telemetry_inventory.sqlite_files)} SQLite, "
            f"{len(telemetry_inventory.jsonl_files)} JSONL, "
            f"{len(telemetry_inventory.snapshot_files)} snapshots, "
            f"{len(telemetry_inventory.config_files)} config)"
        )

        secrets_dest = bundle_contents / "secrets" / ".env"
        copy_secrets(ctx.env_path, secrets_dest, ctx.inventory)

        config_root = bundle_contents / "config"
        copy_repo_systemd_defs(ctx.repo_root, config_root / "systemd-repo", ctx.inventory)
        copy_repo_docker_compose(ctx.repo_root, config_root / "docker-compose", ctx.inventory)
        copy_optional_host_config(config_root, ctx.inventory)

        docs_root = bundle_contents / "docs"
        copy_recovery_docs(ctx.repo_root, docs_root, ctx.inventory)

        manifest_data = ManifestData(
            backup_timestamp=timestamp_iso,
            hostname=hostname,
            backup_target=str(ctx.backup_target),
            repo_root=str(ctx.repo_root),
            git_branch=ctx.git_branch,
            git_commit=ctx.git_commit,
            sqlite_integrity=ctx.sqlite_integrity,
            archive_name=ctx.final_archive.name,
            included_files=list(ctx.inventory.included),
            missing_optional=list(ctx.inventory.missing_optional),
            source_paths=[
                str(ctx.repo_root),
                str(ctx.db_path),
                str(ctx.env_path),
                str(ctx.backup_target),
            ],
            notes=[
                "Incomplete bundles use the .incomplete suffix and are not retention-eligible.",
                f"Completed bundles use the {ctx.archive_suffix} suffix.",
                "The published archive checksum is written to a companion .sha256 file beside the archive.",
                "Long-term Aviary telemetry (Kestrel/Nest/Tuya/Finch SQLite, JSONL history, snapshots) "
                "is copied under database/ and telemetry/ with per-DB integrity checks.",
            ],
            telemetry_coverage=TelemetryCoverage(
                sqlite_databases=sorted(
                    {
                        str(path)
                        for path in telemetry_inventory.sqlite_files
                    }
                ),
                jsonl_history=[
                    str(path) for path in telemetry_inventory.jsonl_files
                ],
                snapshots=[str(path) for path in telemetry_inventory.snapshot_files],
                config_files=[str(path) for path in telemetry_inventory.config_files],
                sqlite_integrity=dict(ctx.sqlite_integrity_all),
                concert_table_counts=dict(ctx.concert_table_counts),
                missing_optional=list(telemetry_inventory.missing_optional),
                catalog_lines=render_telemetry_catalog(telemetry_inventory),
            ),
        )
        manifest_path = bundle_contents / "MANIFEST.txt"
        write_manifest(manifest_path, manifest_data)
        ctx.inventory.included.append(str(manifest_path))

        create_archive(bundle_contents, partial_archive)
        ctx.archive_checksum = sha256_file(partial_archive)

        if ctx.final_archive.exists():
            log_error(f"Refusing to overwrite existing bundle: {ctx.final_archive}")
            return 1

        partial_archive.rename(ctx.final_archive)
        os.chmod(ctx.final_archive, 0o600)

        companion_sha = Path(f"{ctx.final_archive}.sha256")
        companion_manifest = Path(f"{ctx.final_archive}.manifest")
        companion_sha.write_text(f"{ctx.archive_checksum}  {ctx.final_archive.name}\n", encoding="utf-8")

        manifest_data.archive_checksum = ctx.archive_checksum
        companion_manifest.write_text(render_manifest(manifest_data), encoding="utf-8")
        os.chmod(companion_sha, 0o600)
        os.chmod(companion_manifest, 0o644)
        ctx.published = True
        log_info(f"Published recovery bundle: {ctx.final_archive}")
        log_info(f"Archive sha256: {ctx.archive_checksum}")

        if not skip_retention:
            apply_retention(ctx)

        log_info("Pelican backup completed successfully")
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
        if not ctx.published:
            if companion_sha and companion_sha.exists():
                companion_sha.unlink(missing_ok=True)
            if companion_manifest and companion_manifest.exists():
                companion_manifest.unlink(missing_ok=True)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Pelican Raven recovery bundle backup")
    parser.add_argument("--repo-root", type=Path, default=DEFAULT_REPO_ROOT)
    parser.add_argument("--backup-target", type=Path, default=DEFAULT_BACKUP_TARGET)
    parser.add_argument("--db-path", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--env-path", type=Path, default=DEFAULT_ENV_PATH)
    parser.add_argument("--retention-count", type=int, default=DEFAULT_RETENTION_COUNT)
    parser.add_argument(
        "--skip-retention",
        action="store_true",
        help="Skip deleting older completed bundles after a successful run",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    ctx = build_context(
        repo_root=args.repo_root.resolve(),
        backup_target=args.backup_target.resolve(),
        db_path=args.db_path.resolve(),
        env_path=args.env_path.resolve(),
        retention_count=args.retention_count,
    )
    return run_backup(ctx, skip_retention=args.skip_retention)


if __name__ == "__main__":
    sys.exit(main())
