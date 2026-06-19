"""Secret redaction helpers for Pelican manifests and logs."""

from __future__ import annotations

import re
from pathlib import Path

from .config import SECRET_KEY_RE


_ENV_LINE_RE = re.compile(
    r"^(?P<key>[A-Za-z_][A-Za-z0-9_]*)=(?P<value>.*)$"
)


def is_secret_key(key: str) -> bool:
    return SECRET_KEY_RE.search(key) is not None


def redact_env_line(line: str) -> str:
    stripped = line.rstrip("\n")
    match = _ENV_LINE_RE.match(stripped)
    if not match:
        return stripped
    key = match.group("key")
    if is_secret_key(key):
        return f"{key}=***REDACTED***"
    return stripped


def redact_text(text: str) -> str:
    lines = []
    for line in text.splitlines():
        if "=" in line and not line.startswith("#"):
            lines.append(redact_env_line(line))
        else:
            lines.append(line)
    return "\n".join(lines)


def safe_path_label(path: Path) -> str:
    """Return a path label that never includes file contents."""
    return str(path)


def assert_manifest_safe(manifest_text: str, env_path: Path | None = None) -> None:
    """Raise ValueError if manifest text appears to disclose secret values."""
    if env_path is not None:
        name = env_path.name
        for line in manifest_text.splitlines():
            if line.strip().startswith(f"{name}="):
                raise ValueError("Manifest must not include .env contents")

    for line in manifest_text.splitlines():
        upper = line.upper()
        if "TOKEN" in upper and "=" in line and "***REDACTED***" not in line:
            if not line.strip().startswith("-") and "included" not in line.lower():
                raise ValueError("Manifest appears to disclose secret values")
