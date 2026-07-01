"""Local storage, path organization, and manifest tracking for Robin photos."""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

from robin.dedupe import compute_sha256, is_duplicate
from robin.redact import safe_url_for_log

MANIFEST_SCHEMA = """
CREATE TABLE IF NOT EXISTS photo_manifest (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_portal TEXT NOT NULL,
    detected_date TEXT,
    downloaded_path TEXT,
    sha256 TEXT NOT NULL UNIQUE,
    original_url TEXT,
    first_seen TEXT NOT NULL,
    status TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_photo_manifest_status
    ON photo_manifest(status);
CREATE INDEX IF NOT EXISTS idx_photo_manifest_detected_date
    ON photo_manifest(detected_date);
"""

STATUS_DISCOVERED = "discovered"
STATUS_DOWNLOADED = "downloaded"
STATUS_SKIPPED_DUPLICATE = "skipped_duplicate"
STATUS_FAILED = "failed"
STATUS_DRY_RUN = "dry_run"

_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic", ".heif"}
_UNSAFE_FILENAME = re.compile(r"[^A-Za-z0-9._-]+")


@dataclass(frozen=True)
class PhotoManifestEntry:
    source_portal: str
    detected_date: str | None
    downloaded_path: str | None
    sha256: str
    original_url: str | None
    first_seen: str
    status: str


@dataclass(frozen=True)
class DownloadResult:
    status: str
    entry: PhotoManifestEntry | None = None
    error: str | None = None


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def safe_filename(name: str, *, max_length: int = 80) -> str:
    """Return a filesystem-safe filename stem (no directory components)."""
    base = Path(name).name
    base = _UNSAFE_FILENAME.sub("_", base).strip("._")
    if not base:
        return "photo"
    if len(base) > max_length:
        stem = Path(base).stem[: max_length - 10]
        suffix = Path(base).suffix[:10]
        base = f"{stem}{suffix}"
    return base or "photo"


def guess_extension(url: str | None, content_type: str | None = None) -> str:
    """Infer a file extension from URL path or content type."""
    if url:
        suffix = Path(urlparse(url).path).suffix.lower()
        if suffix in _IMAGE_EXTENSIONS:
            return suffix
    if content_type:
        lowered = content_type.lower()
        if "jpeg" in lowered or "jpg" in lowered:
            return ".jpg"
        if "png" in lowered:
            return ".png"
        if "webp" in lowered:
            return ".webp"
        if "gif" in lowered:
            return ".gif"
        if "heic" in lowered or "heif" in lowered:
            return ".heic"
    return ".jpg"


def organize_photo_path(
    output_dir: Path,
    photo_date: date | None,
    content_hash: str,
    *,
    extension: str = ".jpg",
    original_filename: str | None = None,
) -> Path:
    """
    Build the on-disk path for a photo:

    ``{output_dir}/photos/YYYY-MM-DD/<hash_or_safe_filename>.ext``
    """
    date_folder = (photo_date or date.today()).isoformat()
    ext = extension if extension.startswith(".") else f".{extension}"
    hash_prefix = content_hash[:16]

    if original_filename:
        safe = safe_filename(original_filename)
        stem = f"{hash_prefix}_{safe}" if safe != "photo" else hash_prefix
    else:
        stem = hash_prefix

    return output_dir / "photos" / date_folder / f"{stem}{ext}"


def get_connection(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def init_manifest(db_path: Path) -> None:
    with get_connection(db_path) as conn:
        conn.executescript(MANIFEST_SCHEMA)
        conn.commit()


def load_known_hashes(db_path: Path) -> set[str]:
    init_manifest(db_path)
    with get_connection(db_path) as conn:
        rows = conn.execute("SELECT sha256 FROM photo_manifest").fetchall()
    return {str(row["sha256"]) for row in rows}


def upsert_manifest_entry(db_path: Path, entry: PhotoManifestEntry) -> tuple[bool, bool]:
    """
    Insert a manifest row, ignoring duplicates on sha256.

    Returns (inserted, skipped_duplicate).
    """
    init_manifest(db_path)
    with get_connection(db_path) as conn:
        cursor = conn.execute(
            """
            INSERT OR IGNORE INTO photo_manifest (
                source_portal, detected_date, downloaded_path, sha256,
                original_url, first_seen, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                entry.source_portal,
                entry.detected_date,
                entry.downloaded_path,
                entry.sha256,
                entry.original_url,
                entry.first_seen,
                entry.status,
            ),
        )
        conn.commit()
        rowcount = cursor.rowcount
    if rowcount:
        return True, False
    return False, True


def fetch_manifest_entries(
    db_path: Path,
    *,
    status: str | None = None,
    since_date: date | None = None,
) -> list[PhotoManifestEntry]:
    init_manifest(db_path)
    clauses: list[str] = []
    params: list[object] = []

    if status:
        clauses.append("status = ?")
        params.append(status)
    if since_date:
        clauses.append("(detected_date IS NULL OR detected_date >= ?)")
        params.append(since_date.isoformat())

    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    query = f"""
        SELECT source_portal, detected_date, downloaded_path, sha256,
               original_url, first_seen, status
        FROM photo_manifest
        {where}
        ORDER BY first_seen ASC
    """

    with get_connection(db_path) as conn:
        rows = conn.execute(query, params).fetchall()

    return [
        PhotoManifestEntry(
            source_portal=row["source_portal"],
            detected_date=row["detected_date"],
            downloaded_path=row["downloaded_path"],
            sha256=row["sha256"],
            original_url=row["original_url"],
            first_seen=row["first_seen"],
            status=row["status"],
        )
        for row in rows
    ]


def count_manifest_by_status(db_path: Path) -> dict[str, int]:
    init_manifest(db_path)
    with get_connection(db_path) as conn:
        rows = conn.execute(
            "SELECT status, COUNT(*) AS n FROM photo_manifest GROUP BY status"
        ).fetchall()
    return {str(row["status"]): int(row["n"]) for row in rows}


def default_http_get(url: str) -> tuple[bytes, str | None]:
    """Download bytes from a URL using stdlib urllib."""
    import urllib.request

    request = urllib.request.Request(
        url,
        headers={"User-Agent": "RobinDaycarePhotoProbe/0.1 (+local-archive)"},
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        content_type = response.headers.get("Content-Type")
        return response.read(), content_type


def download_and_store(
    *,
    url: str,
    output_dir: Path,
    manifest_path: Path,
    source_portal: str,
    detected_date: date | None,
    known_hashes: set[str] | None = None,
    dry_run: bool = False,
    original_filename: str | None = None,
    http_get: Callable[[str], tuple[bytes, str | None]] | None = None,
) -> DownloadResult:
    """
    Download a photo candidate, dedupe by content hash, organize locally, and record manifest.

    Returns a DownloadResult with status downloaded, skipped_duplicate, failed, or dry_run.
    """
    now = utc_now_iso()
    safe_log_url = safe_url_for_log(url)
    hashes = known_hashes if known_hashes is not None else set()
    if not dry_run and known_hashes is None:
        hashes = load_known_hashes(manifest_path)
    getter = http_get or default_http_get

    if dry_run:
        entry = PhotoManifestEntry(
            source_portal=source_portal,
            detected_date=detected_date.isoformat() if detected_date else None,
            downloaded_path=None,
            sha256="",
            original_url=safe_log_url,
            first_seen=now,
            status=STATUS_DRY_RUN,
        )
        return DownloadResult(status=STATUS_DRY_RUN, entry=entry)

    try:
        content, content_type = getter(url)
    except Exception as exc:  # noqa: BLE001
        entry = PhotoManifestEntry(
            source_portal=source_portal,
            detected_date=detected_date.isoformat() if detected_date else None,
            downloaded_path=None,
            sha256="",
            original_url=safe_log_url,
            first_seen=now,
            status=STATUS_FAILED,
        )
        return DownloadResult(status=STATUS_FAILED, entry=entry, error=type(exc).__name__)

    if not content:
        entry = PhotoManifestEntry(
            source_portal=source_portal,
            detected_date=detected_date.isoformat() if detected_date else None,
            downloaded_path=None,
            sha256="",
            original_url=safe_log_url,
            first_seen=now,
            status=STATUS_FAILED,
        )
        return DownloadResult(status=STATUS_FAILED, entry=entry, error="empty_response")

    content_hash = compute_sha256(content)
    if is_duplicate(content_hash, hashes):
        entry = PhotoManifestEntry(
            source_portal=source_portal,
            detected_date=detected_date.isoformat() if detected_date else None,
            downloaded_path=None,
            sha256=content_hash,
            original_url=safe_log_url,
            first_seen=now,
            status=STATUS_SKIPPED_DUPLICATE,
        )
        upsert_manifest_entry(manifest_path, entry)
        return DownloadResult(status=STATUS_SKIPPED_DUPLICATE, entry=entry)

    extension = guess_extension(url, content_type)
    dest_path = organize_photo_path(
        output_dir,
        detected_date,
        content_hash,
        extension=extension,
        original_filename=original_filename,
    )
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    dest_path.write_bytes(content)

    entry = PhotoManifestEntry(
        source_portal=source_portal,
        detected_date=detected_date.isoformat() if detected_date else None,
        downloaded_path=str(dest_path),
        sha256=content_hash,
        original_url=safe_log_url,
        first_seen=now,
        status=STATUS_DOWNLOADED,
    )
    inserted, skipped = upsert_manifest_entry(manifest_path, entry)
    if skipped:
        if dest_path.exists():
            dest_path.unlink()
        entry = PhotoManifestEntry(
            source_portal=source_portal,
            detected_date=detected_date.isoformat() if detected_date else None,
            downloaded_path=None,
            sha256=content_hash,
            original_url=safe_log_url,
            first_seen=now,
            status=STATUS_SKIPPED_DUPLICATE,
        )
        return DownloadResult(status=STATUS_SKIPPED_DUPLICATE, entry=entry)

    if inserted:
        hashes.add(content_hash)

    return DownloadResult(status=STATUS_DOWNLOADED, entry=entry)
