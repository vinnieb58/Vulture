"""Source discovery for Pelican Raven database snapshots."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .db_snapshot_config import DEFAULT_MAX_JSON_BYTES, OPTIONAL_JSON_STATE_FILES


@dataclass
class SnapshotInventory:
    sqlite_sources: list[tuple[Path, Path]] = field(default_factory=list)
    json_sources: list[tuple[Path, Path]] = field(default_factory=list)
    skipped_json: list[str] = field(default_factory=list)
    missing_required: list[str] = field(default_factory=list)


def discover_sqlite_sources(repo_root: Path) -> list[tuple[Path, Path]]:
    """Return (relative_path, absolute_path) pairs for Raven SQLite databases."""
    sources: list[tuple[Path, Path]] = []

    data_dir = repo_root / "data"
    if data_dir.is_dir():
        for path in sorted(data_dir.glob("*.db")):
            if path.is_file():
                sources.append((path.relative_to(repo_root), path))

    kestrel_dir = data_dir / "kestrel"
    if kestrel_dir.is_dir():
        for path in sorted(kestrel_dir.glob("*.db")):
            if path.is_file():
                sources.append((path.relative_to(repo_root), path))

    return sources


def discover_json_state_files(
    repo_root: Path,
    *,
    max_bytes: int = DEFAULT_MAX_JSON_BYTES,
) -> tuple[list[tuple[Path, Path]], list[str]]:
    """Return included JSON state files and skip reasons for excluded ones."""
    included: list[tuple[Path, Path]] = []
    skipped: list[str] = []

    for rel_name in OPTIONAL_JSON_STATE_FILES:
        rel_path = Path(rel_name)
        absolute = repo_root / rel_path
        if not absolute.is_file():
            continue
        size = absolute.stat().st_size
        if size > max_bytes:
            skipped.append(
                f"Skipping {rel_name}: size {size} bytes exceeds limit {max_bytes} bytes"
            )
            continue
        included.append((rel_path, absolute))

    return included, skipped


def classify_snapshot_sources(
    repo_root: Path,
    *,
    required_db: Path | None = None,
    max_json_bytes: int = DEFAULT_MAX_JSON_BYTES,
) -> SnapshotInventory:
    inventory = SnapshotInventory()
    inventory.sqlite_sources = discover_sqlite_sources(repo_root)
    inventory.json_sources, inventory.skipped_json = discover_json_state_files(
        repo_root,
        max_bytes=max_json_bytes,
    )

    required = required_db or (repo_root / "data" / "vulture.db")
    if not required.is_file():
        inventory.missing_required.append(f"Required SQLite database not found: {required}")

    return inventory
