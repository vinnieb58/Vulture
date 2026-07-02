"""Long-term Aviary telemetry and history data discovery for Pelican backups."""

from __future__ import annotations

import fnmatch
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from .sqlite_backup import SqliteBackupResult, backup_and_verify_sqlite

# SQLite files that are ephemeral UI state, not long-term history.
EPHEMERAL_DB_NAMES = frozenset(
    {
        "finch_pending_selection.db",
    }
)

# Repo-relative directory prefixes that hold disposable probe artifacts.
DISPOSABLE_DIR_PREFIXES = (
    "data/kestrel/debug/",
    "experiments/debug/",
    "experiments/simplyfresh_probe/.auth/",
    "experiments/simplyfresh_probe/artifacts/",
)

# Repo-relative glob patterns for disposable files (never backed up as telemetry).
DISPOSABLE_FILE_GLOBS = (
    "**/*.trace.zip",
    "**/*screenshot*",
    "**/*_error.json",
    "data/canary_status.json",
    "data/backup_monitor_status.json",
    "data/canary_alert_state.json",
    "data/concert_watch_status.json",
    "tuya-raw.json",
)

# Dashboard rolling JSONL (short retention; long-term copies live in data/telemetry/*_archive.jsonl).
CRITICAL_JSONL_DASHBOARD_REL_PATHS = (
    "data/kestrel_nest_history.jsonl",
    "data/kestrel_tuya_power_history.jsonl",
    "data/raven_metrics_history.jsonl",
)

# Indefinite long-term telemetry archives (append-only; never pruned at runtime).
CRITICAL_JSONL_ARCHIVE_REL_PATHS = (
    "data/telemetry/nest_history_archive.jsonl",
    "data/telemetry/tuya_power_history_archive.jsonl",
    "data/telemetry/raven_metrics_history_archive.jsonl",
)

CRITICAL_JSONL_REL_PATHS = CRITICAL_JSONL_ARCHIVE_REL_PATHS + CRITICAL_JSONL_DASHBOARD_REL_PATHS

# Latest-value snapshots needed for dashboard/probe continuity after restore.
CRITICAL_SNAPSHOT_REL_PATHS = (
    "data/kestrel/kestrel_status.json",
    "data/kestrel_nest_status.json",
    "data/kestrel_tuya_power_status.json",
)

# Non-.env config needed to restore integrations (optional when absent).
CRITICAL_CONFIG_REL_PATHS = (
    "devices.json",
    "tinytuya.json",
    "snapshot.json",
    "data/finch_tokens.json",
    "data/finch_config.json",
)

SQLITE_SUFFIXES = (".db", ".sqlite", ".sqlite3")


@dataclass(frozen=True)
class LongTermDataEntry:
    rel_path: str
    category: str
    description: str
    optional: bool = False


@dataclass
class TelemetryInventory:
    sqlite_files: list[Path] = field(default_factory=list)
    jsonl_files: list[Path] = field(default_factory=list)
    snapshot_files: list[Path] = field(default_factory=list)
    config_files: list[Path] = field(default_factory=list)
    catalog: list[LongTermDataEntry] = field(default_factory=list)
    missing_optional: list[str] = field(default_factory=list)

    @property
    def all_source_paths(self) -> list[Path]:
        return self.sqlite_files + self.jsonl_files + self.snapshot_files + self.config_files


@dataclass
class JsonlVerifyResult:
    rel_path: str
    source_present: bool
    source_nonempty: bool
    dest_present: bool
    dest_nonempty: bool
    ok: bool
    message: str


@dataclass
class TelemetryBackupResult:
    sqlite_results: list[SqliteBackupResult] = field(default_factory=list)
    jsonl_results: list[JsonlVerifyResult] = field(default_factory=list)
    copied_files: list[str] = field(default_factory=list)
    missing_optional: list[str] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.failures


def _rel_posix(path: Path, repo_root: Path) -> str:
    return path.resolve().relative_to(repo_root.resolve()).as_posix()


def _matches_disposable(rel_posix: str) -> bool:
    if any(rel_posix.startswith(prefix) for prefix in DISPOSABLE_DIR_PREFIXES):
        return True
    return any(fnmatch.fnmatch(rel_posix, pattern) for pattern in DISPOSABLE_FILE_GLOBS)


def _is_sqlite_file(path: Path) -> bool:
    return path.suffix.lower() in SQLITE_SUFFIXES


def discover_sqlite_databases(repo_root: Path, *, primary_db: Path | None = None) -> list[Path]:
    """Discover long-term SQLite databases under data/ (recursive)."""
    repo_root = repo_root.resolve()
    data_dir = repo_root / "data"
    found: dict[str, Path] = {}

    if primary_db and primary_db.is_file():
        found[_rel_posix(primary_db, repo_root)] = primary_db.resolve()

    if data_dir.is_dir():
        for path in sorted(data_dir.rglob("*")):
            if not path.is_file() or not _is_sqlite_file(path):
                continue
            rel = _rel_posix(path, repo_root)
            if _matches_disposable(rel):
                continue
            if path.name in EPHEMERAL_DB_NAMES:
                continue
            found[rel] = path.resolve()

    return [found[key] for key in sorted(found)]


def discover_long_term_data(
    repo_root: Path,
    *,
    primary_db: Path | None = None,
) -> TelemetryInventory:
    """Build an inventory of long-term telemetry/history sources."""
    repo_root = repo_root.resolve()
    inventory = TelemetryInventory()

    for db_path in discover_sqlite_databases(repo_root, primary_db=primary_db):
        rel = _rel_posix(db_path, repo_root)
        inventory.sqlite_files.append(db_path)
        inventory.catalog.append(
            LongTermDataEntry(
                rel_path=rel,
                category="sqlite",
                description="SQLite long-term history/state",
                optional=False,
            )
        )

    for rel in CRITICAL_JSONL_REL_PATHS:
        path = repo_root / rel
        is_archive = rel.startswith("data/telemetry/") and rel.endswith("_archive.jsonl")
        entry = LongTermDataEntry(
            rel_path=rel,
            category="jsonl_archive" if is_archive else "jsonl",
            description=(
                "Indefinite append-only telemetry archive"
                if is_archive
                else "Dashboard rolling telemetry JSONL"
            ),
            optional=True,
        )
        inventory.catalog.append(entry)
        if path.is_file():
            if path not in inventory.jsonl_files:
                inventory.jsonl_files.append(path.resolve())
        else:
            inventory.missing_optional.append(rel)

    telemetry_dir = repo_root / "data" / "telemetry"
    if telemetry_dir.is_dir():
        for path in sorted(telemetry_dir.glob("*.jsonl")):
            rel = _rel_posix(path, repo_root)
            if rel in {entry.rel_path for entry in inventory.catalog}:
                continue
            inventory.catalog.append(
                LongTermDataEntry(
                    rel_path=rel,
                    category="jsonl_archive",
                    description="Indefinite telemetry archive (discovered)",
                    optional=True,
                )
            )
            if path.is_file() and path.resolve() not in inventory.jsonl_files:
                inventory.jsonl_files.append(path.resolve())

    for rel in CRITICAL_SNAPSHOT_REL_PATHS:
        path = repo_root / rel
        entry = LongTermDataEntry(
            rel_path=rel,
            category="snapshot",
            description="Latest probe/dashboard snapshot JSON",
            optional=True,
        )
        inventory.catalog.append(entry)
        if path.is_file():
            inventory.snapshot_files.append(path.resolve())
        else:
            inventory.missing_optional.append(rel)

    for rel in CRITICAL_CONFIG_REL_PATHS:
        path = repo_root / rel
        entry = LongTermDataEntry(
            rel_path=rel,
            category="config",
            description="Integration restore config (non-.env)",
            optional=True,
        )
        inventory.catalog.append(entry)
        if path.is_file():
            inventory.config_files.append(path.resolve())
        else:
            inventory.missing_optional.append(rel)

    return inventory


def _database_dest_path(dest_root: Path, repo_root: Path, source: Path) -> Path:
    rel = source.resolve().relative_to(repo_root.resolve())
    if rel.parts[:2] == ("data", "vulture.db") or rel.as_posix() == "data/vulture.db":
        return dest_root / "vulture.db"
    return dest_root / rel.relative_to("data")


def verify_jsonl_copy(source: Path, dest: Path) -> JsonlVerifyResult:
    rel = source.name
    source_present = source.is_file()
    source_nonempty = source_present and source.stat().st_size > 0
    dest_present = dest.is_file()
    dest_nonempty = dest_present and dest.stat().st_size > 0

    if source_nonempty and not dest_nonempty:
        return JsonlVerifyResult(
            rel_path=rel,
            source_present=source_present,
            source_nonempty=source_nonempty,
            dest_present=dest_present,
            dest_nonempty=dest_nonempty,
            ok=False,
            message=f"JSONL backup missing or empty for non-empty source: {source}",
        )

    return JsonlVerifyResult(
        rel_path=rel,
        source_present=source_present,
        source_nonempty=source_nonempty,
        dest_present=dest_present,
        dest_nonempty=dest_nonempty,
        ok=True,
        message="JSONL backup verified" if source_nonempty else "JSONL source absent or empty",
    )


def backup_telemetry_data(
    repo_root: Path,
    *,
    dest_root: Path,
    primary_db: Path,
    inventory: TelemetryInventory | None = None,
) -> TelemetryBackupResult:
    """
    Copy long-term telemetry data into dest_root with SQLite integrity checks.

    Layout:
      database/           — SQLite backups (vulture.db at root; others preserve data/ subpaths)
      telemetry/history/  — JSONL history files
      telemetry/snapshots/ — latest JSON snapshots (preserve subdirs)
      telemetry/config/   — integration config files
    """
    repo_root = repo_root.resolve()
    inv = inventory or discover_long_term_data(repo_root, primary_db=primary_db)
    result = TelemetryBackupResult(missing_optional=list(inv.missing_optional))

    database_root = dest_root / "database"
    history_root = dest_root / "telemetry" / "history"
    snapshot_root = dest_root / "telemetry" / "snapshots"
    config_root = dest_root / "telemetry" / "config"

    primary_resolved = primary_db.resolve()
    for source in inv.sqlite_files:
        if source.resolve() == primary_resolved:
            continue

        db_dest = _database_dest_path(database_root, repo_root, source)
        sqlite_result = backup_and_verify_sqlite(source, db_dest)
        result.sqlite_results.append(sqlite_result)
        integrity_note = database_root / "integrity" / f"{db_dest.relative_to(database_root).as_posix()}.txt"
        integrity_note.parent.mkdir(parents=True, exist_ok=True)
        integrity_note.write_text(sqlite_result.integrity_result, encoding="utf-8")
        result.copied_files.extend([str(db_dest), str(integrity_note)])
        if not sqlite_result.ok:
            result.failures.append(sqlite_result.message)

    for source in inv.jsonl_files:
        rel = _rel_posix(source, repo_root)
        dest = history_root / Path(rel).name
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, dest)
        result.copied_files.append(str(dest))
        jsonl_result = verify_jsonl_copy(source, dest)
        jsonl_result = JsonlVerifyResult(
            rel_path=rel,
            source_present=jsonl_result.source_present,
            source_nonempty=jsonl_result.source_nonempty,
            dest_present=jsonl_result.dest_present,
            dest_nonempty=jsonl_result.dest_nonempty,
            ok=jsonl_result.ok,
            message=jsonl_result.message,
        )
        result.jsonl_results.append(jsonl_result)
        if not jsonl_result.ok:
            result.failures.append(jsonl_result.message)

    for source in inv.snapshot_files:
        rel = _rel_posix(source, repo_root)
        rel_under_data = Path(rel).relative_to("data")
        dest = snapshot_root / rel_under_data
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, dest)
        result.copied_files.append(str(dest))

    for source in inv.config_files:
        rel = _rel_posix(source, repo_root)
        if rel.startswith("data/"):
            dest = config_root / Path(rel).relative_to("data")
        else:
            dest = config_root / Path(rel).name
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, dest)
        if rel.endswith("finch_tokens.json"):
            dest.chmod(0o600)
        result.copied_files.append(str(dest))

    return result


def render_telemetry_catalog(inventory: TelemetryInventory) -> list[str]:
    lines: list[str] = []
    for entry in inventory.catalog:
        optional = "optional" if entry.optional else "required"
        lines.append(f"  - [{entry.category}/{optional}] {entry.rel_path} — {entry.description}")
    return lines


def expected_telemetry_manifest_markers() -> tuple[str, ...]:
    """Strings that should appear in a recovery bundle manifest for telemetry coverage."""
    return (
        "telemetry_coverage:",
        "long_term_data_catalog:",
        "sqlite_databases:",
        "jsonl_history:",
    )
