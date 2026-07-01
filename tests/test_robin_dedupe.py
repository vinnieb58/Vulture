"""Unit tests for Robin content-hash deduplication."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from robin.dedupe import compute_file_hash, compute_sha256, is_duplicate


class TestRobinDedupe:
    def test_compute_sha256_stable(self) -> None:
        content = b"daycare-photo-bytes"
        assert compute_sha256(content) == compute_sha256(content)
        assert len(compute_sha256(content)) == 64

    def test_compute_sha256_differs_for_different_content(self) -> None:
        assert compute_sha256(b"photo-a") != compute_sha256(b"photo-b")

    def test_compute_file_hash_matches_bytes(self, tmp_path: Path) -> None:
        photo_path = tmp_path / "sample.jpg"
        content = b"\xff\xd8\xff fake-jpeg"
        photo_path.write_bytes(content)
        assert compute_file_hash(photo_path) == compute_sha256(content)

    def test_is_duplicate_detects_known_hash(self) -> None:
        known = {compute_sha256(b"already-have-this")}
        assert is_duplicate(compute_sha256(b"already-have-this"), known) is True
        assert is_duplicate(compute_sha256(b"new-photo"), known) is False
