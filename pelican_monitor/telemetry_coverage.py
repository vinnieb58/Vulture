"""Parse Pelican recovery bundle manifests for telemetry backup coverage."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from scripts.pelican.telemetry_data import expected_telemetry_manifest_markers


@dataclass
class TelemetryCoverageStatus:
    manifest_path: str | None = None
    covered: bool = False
    sqlite_count: int = 0
    jsonl_count: int = 0
    snapshot_count: int = 0
    config_count: int = 0
    missing_markers: list[str] = field(default_factory=list)
    message: str = "Telemetry coverage not checked"


def _count_section_items(text: str, section: str) -> int:
    lines = text.splitlines()
    in_section = False
    count = 0
    for line in lines:
        if line.strip() == f"{section}:":
            in_section = True
            continue
        if in_section:
            stripped = line.strip()
            if not stripped:
                continue
            if stripped.endswith(":") and not stripped.startswith("- "):
                break
            if stripped.startswith("- ") and stripped != "- (none)":
                count += 1
    return count


def evaluate_manifest_telemetry_coverage(manifest_text: str) -> TelemetryCoverageStatus:
    markers = expected_telemetry_manifest_markers()
    missing = [marker for marker in markers if marker not in manifest_text]
    sqlite_count = _count_section_items(manifest_text, "sqlite_databases")
    jsonl_count = _count_section_items(manifest_text, "jsonl_history")
    snapshot_count = _count_section_items(manifest_text, "telemetry_snapshots")
    config_count = _count_section_items(manifest_text, "telemetry_config")

    covered = not missing and sqlite_count >= 1
    if covered:
        message = (
            "Telemetry/history backup covered "
            f"(sqlite={sqlite_count}, jsonl={jsonl_count}, snapshots={snapshot_count}, config={config_count})"
        )
    elif missing:
        message = f"Manifest missing telemetry markers: {', '.join(missing)}"
    else:
        message = "Manifest lacks backed-up SQLite databases in telemetry_coverage"

    return TelemetryCoverageStatus(
        covered=covered,
        sqlite_count=sqlite_count,
        jsonl_count=jsonl_count,
        snapshot_count=snapshot_count,
        config_count=config_count,
        missing_markers=missing,
        message=message,
    )


def read_latest_companion_manifest(target_dir: Path, archive_name: str | None) -> tuple[Path | None, str | None]:
    if not archive_name:
        return None, None
    manifest_path = target_dir / f"{archive_name}.manifest"
    if not manifest_path.is_file():
        return manifest_path, None
    try:
        return manifest_path, manifest_path.read_text(encoding="utf-8")
    except OSError:
        return manifest_path, None


def evaluate_latest_archive_telemetry(target_dir: Path, archive_name: str | None) -> TelemetryCoverageStatus:
    manifest_path, text = read_latest_companion_manifest(target_dir, archive_name)
    if text is None:
        return TelemetryCoverageStatus(
            manifest_path=str(manifest_path) if manifest_path else None,
            covered=False,
            message="Latest archive companion manifest missing or unreadable",
        )
    status = evaluate_manifest_telemetry_coverage(text)
    status.manifest_path = str(manifest_path) if manifest_path else None
    return status
