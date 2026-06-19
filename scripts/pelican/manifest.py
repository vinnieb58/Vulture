"""Human-readable Pelican backup manifest generation."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .config import SCRIPT_VERSION
from .redaction import assert_manifest_safe


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
