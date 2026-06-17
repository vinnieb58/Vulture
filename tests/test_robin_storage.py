"""Unit tests for Robin storage, manifest, and path organization."""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from robin.dedupe import compute_sha256
from robin.storage import (
    STATUS_DOWNLOADED,
    STATUS_SKIPPED_DUPLICATE,
    PhotoManifestEntry,
    download_and_store,
    fetch_manifest_entries,
    organize_photo_path,
    safe_filename,
    upsert_manifest_entry,
    utc_now_iso,
)


class TestRobinSafeFilename:
    def test_strips_path_components(self) -> None:
        assert safe_filename("../../etc/passwd") == "passwd"

    def test_replaces_unsafe_characters(self) -> None:
        assert safe_filename('baby "smile" <3.jpg') == "baby_smile_3.jpg"

    def test_empty_becomes_photo(self) -> None:
        assert safe_filename("***") == "photo"


class TestRobinOrganizePhotoPath:
    def test_uses_date_folder_and_hash_prefix(self) -> None:
        photo_date = date(2026, 6, 15)
        content_hash = "a" * 64
        path = organize_photo_path(Path("/data/robin"), photo_date, content_hash, extension=".jpg")
        assert path == Path("/data/robin/photos/2026-06-15") / f"{'a' * 16}.jpg"

    def test_includes_safe_original_filename_when_provided(self) -> None:
        photo_date = date(2026, 6, 15)
        content_hash = "b" * 64
        path = organize_photo_path(
            Path("data/robin"),
            photo_date,
            content_hash,
            extension=".png",
            original_filename="nap time.png",
        )
        assert path.name.startswith("bbbbbbbbbbbbbbbb_nap_time.png")

    def test_defaults_to_today_when_date_missing(self) -> None:
        content_hash = "c" * 64
        path = organize_photo_path(Path("data/robin"), None, content_hash)
        assert path.parent.name == date.today().isoformat()


class TestRobinManifest:
    def test_upsert_and_fetch(self, tmp_path: Path) -> None:
        db_path = tmp_path / "manifest.db"
        entry = PhotoManifestEntry(
            source_portal="daycare_portal",
            detected_date="2026-06-15",
            downloaded_path=str(tmp_path / "photo.jpg"),
            sha256="d" * 64,
            original_url="https://example.com/photos/1.jpg",
            first_seen=utc_now_iso(),
            status=STATUS_DOWNLOADED,
        )
        inserted, skipped = upsert_manifest_entry(db_path, entry)
        assert inserted is True
        assert skipped is False

        stored = fetch_manifest_entries(db_path)
        assert len(stored) == 1
        assert stored[0].sha256 == "d" * 64
        assert stored[0].status == STATUS_DOWNLOADED

    def test_upsert_skips_duplicate_hash(self, tmp_path: Path) -> None:
        db_path = tmp_path / "manifest.db"
        sha = "e" * 64
        first = PhotoManifestEntry(
            source_portal="daycare_portal",
            detected_date="2026-06-15",
            downloaded_path=str(tmp_path / "one.jpg"),
            sha256=sha,
            original_url="https://example.com/one.jpg",
            first_seen=utc_now_iso(),
            status=STATUS_DOWNLOADED,
        )
        duplicate = PhotoManifestEntry(
            source_portal="daycare_portal",
            detected_date="2026-06-16",
            downloaded_path=str(tmp_path / "two.jpg"),
            sha256=sha,
            original_url="https://example.com/two.jpg",
            first_seen=utc_now_iso(),
            status=STATUS_SKIPPED_DUPLICATE,
        )

        upsert_manifest_entry(db_path, first)
        inserted, skipped = upsert_manifest_entry(db_path, duplicate)
        assert inserted is False
        assert skipped is True
        assert len(fetch_manifest_entries(db_path)) == 1


class TestRobinDownloadAndStore:
    def test_download_organizes_by_date_and_writes_manifest(self, tmp_path: Path) -> None:
        content = b"fake-image-content"
        url = "https://cdn.example.com/gallery/2026-06-15/smile.jpg"

        def fake_get(request_url: str) -> tuple[bytes, str | None]:
            assert request_url == url
            return content, "image/jpeg"

        result = download_and_store(
            url=url,
            output_dir=tmp_path,
            manifest_path=tmp_path / "manifest.db",
            source_portal="daycare_portal",
            detected_date=date(2026, 6, 15),
            http_get=fake_get,
        )

        assert result.status == STATUS_DOWNLOADED
        assert result.entry is not None
        expected_hash = compute_sha256(content)
        assert result.entry.sha256 == expected_hash
        dest = Path(result.entry.downloaded_path or "")
        assert dest.exists()
        assert dest.parent == tmp_path / "photos" / "2026-06-15"
        assert dest.read_bytes() == content

    def test_duplicate_skip_does_not_write_second_file(self, tmp_path: Path) -> None:
        content = b"same-bytes"
        url_a = "https://cdn.example.com/a.jpg"
        url_b = "https://cdn.example.com/b.jpg"

        def fake_get(request_url: str) -> tuple[bytes, str | None]:
            return content, "image/jpeg"

        first = download_and_store(
            url=url_a,
            output_dir=tmp_path,
            manifest_path=tmp_path / "manifest.db",
            source_portal="daycare_portal",
            detected_date=date(2026, 6, 15),
            http_get=fake_get,
        )
        second = download_and_store(
            url=url_b,
            output_dir=tmp_path,
            manifest_path=tmp_path / "manifest.db",
            source_portal="daycare_portal",
            detected_date=date(2026, 6, 16),
            http_get=fake_get,
        )

        assert first.status == STATUS_DOWNLOADED
        assert second.status == STATUS_SKIPPED_DUPLICATE
        files = list((tmp_path / "photos").rglob("*"))
        assert len([path for path in files if path.is_file()]) == 1

    def test_dry_run_does_not_write_files_or_manifest(self, tmp_path: Path) -> None:
        def fake_get(_url: str) -> tuple[bytes, str | None]:
            raise AssertionError("dry-run should not download")

        result = download_and_store(
            url="https://cdn.example.com/x.jpg",
            output_dir=tmp_path,
            manifest_path=tmp_path / "manifest.db",
            source_portal="daycare_portal",
            detected_date=date(2026, 6, 15),
            dry_run=True,
            http_get=fake_get,
        )

        assert result.status == "dry_run"
        assert not (tmp_path / "photos").exists()
        assert not (tmp_path / "manifest.db").exists()
