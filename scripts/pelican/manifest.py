"""Human-readable Pelican backup manifest generation."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .config import SCRIPT_VERSION
from .redaction import assert_manifest_safe


@dataclass
class TelemetryCoverage:
    sqlite_databases: list[str] = field(default_factory=list)
    jsonl_history: list[str] = field(default_factory=list)
    snapshots: list[str] = field(default_factory=list)
    config_files: list[str] = field(default_factory=list)
    sqlite_integrity: dict[str, str] = field(default_factory=dict)
    concert_table_counts: dict[str, int] = field(default_factory=dict)
    missing_optional: list[str] = field(default_factory=list)
    catalog_lines: list[str] = field(default_factory=list)


@dataclass
class ManifestData:
    backup_timestamp: str
    hostname: str
    script_version: str = SCRIPT_VERSION
    backup_target: str = ""
    repo_root: str = ""
    git_branch: str = ""
    git_commit: str = ""
    sqlite_integrity: str = ""
    archive_checksum: str = ""
    archive_name: str = ""
    included_files: list[str] = field(default_factory=list)
    missing_optional: list[str] = field(default_factory=list)
    source_paths: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    telemetry_coverage: TelemetryCoverage | None = None


def render_manifest(data: ManifestData) -> str:
    lines = [
        "Pelican Raven Recovery Bundle Manifest",
        "======================================",
        f"backup_timestamp: {data.backup_timestamp}",
        f"hostname: {data.hostname}",
        f"script_version: {data.script_version}",
        f"archive_name: {data.archive_name}",
        f"archive_sha256: {data.archive_checksum or '(pending)'}",
        "",
        "git:",
        f"  branch: {data.git_branch or 'unknown'}",
        f"  commit: {data.git_commit or 'unknown'}",
        "",
        "sqlite:",
        f"  integrity_check: {data.sqlite_integrity or 'unknown'}",
        "",
        "backup_target:",
        f"  path: {data.backup_target}",
        "",
        "source_paths:",
    ]
    for path in data.source_paths:
        lines.append(f"  - {path}")

    lines.extend(["", "included_files:"])
    for path in sorted(data.included_files):
        lines.append(f"  - {path}")

    if data.telemetry_coverage is not None:
        cov = data.telemetry_coverage
        lines.extend(
            [
                "",
                "telemetry_coverage:",
                "  Aviary long-term telemetry/history files included in this bundle.",
                "",
                "long_term_data_catalog:",
            ]
        )
        if cov.catalog_lines:
            lines.extend(cov.catalog_lines)
        else:
            lines.append("  - (none cataloged)")

        lines.extend(["", "sqlite_databases:"])
        if cov.sqlite_databases:
            for path in cov.sqlite_databases:
                integrity = cov.sqlite_integrity.get(path, "unknown")
                lines.append(f"  - {path} (integrity={integrity})")
        else:
            lines.append("  - (none)")

        lines.extend(["", "jsonl_history:"])
        if cov.jsonl_history:
            for path in cov.jsonl_history:
                lines.append(f"  - {path}")
        else:
            lines.append("  - (none)")

        lines.extend(["", "telemetry_snapshots:"])
        if cov.snapshots:
            for path in cov.snapshots:
                lines.append(f"  - {path}")
        else:
            lines.append("  - (none)")

        lines.extend(["", "telemetry_config:"])
        if cov.config_files:
            for path in cov.config_files:
                lines.append(f"  - {path}")
        else:
            lines.append("  - (none)")

        lines.extend(["", "vulture_db_concert_tables:"])
        if cov.concert_table_counts:
            for table, count in sorted(cov.concert_table_counts.items()):
                lines.append(f"  - {table}: {count}")
        else:
            lines.append("  - (not verified)")

        lines.extend(["", "telemetry_missing_optional:"])
        if cov.missing_optional:
            for path in cov.missing_optional:
                lines.append(f"  - {path}")
        else:
            lines.append("  - (none)")

    lines.extend(["", "missing_optional:"])
    if data.missing_optional:
        for path in data.missing_optional:
            lines.append(f"  - {path}")
    else:
        lines.append("  - (none)")

    if data.notes:
        lines.extend(["", "notes:"])
        for note in data.notes:
            lines.append(f"  - {note}")

    lines.extend(
        [
            "",
            "security:",
            "  - This bundle may contain Raven secrets (.env). Restrict filesystem access.",
            "  - Secret values are intentionally omitted from this manifest.",
            "",
        ]
    )
    text = "\n".join(lines)
    assert_manifest_safe(text)
    return text


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def write_manifest(path: Path, data: ManifestData) -> str:
    text = render_manifest(data)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return text
