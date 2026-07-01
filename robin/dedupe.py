"""Content-hash deduplication for Robin photo downloads."""

from __future__ import annotations

import hashlib
from pathlib import Path


def compute_sha256(content: bytes) -> str:
    """Return the lowercase hex SHA-256 digest of raw bytes."""
    return hashlib.sha256(content).hexdigest()


def compute_file_hash(path: Path) -> str:
    """Return the SHA-256 digest of a file on disk."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def is_duplicate(content_hash: str, known_hashes: set[str]) -> bool:
    """Return True when the content hash already exists in the known set."""
    return content_hash in known_hashes
