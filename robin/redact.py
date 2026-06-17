"""Safe redaction helpers for Robin logs and status messages."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse

_BEARER = re.compile(r"(?i)\bbearer\s+\S+")
_AUTH_HEADER = re.compile(r"(?i)\bauthorization\s*[:=]\s*\S+")
_ENV_SECRET = re.compile(
    r"(?i)\b(ROBIN_DAYCARE_PASSWORD|ROBIN_DAYCARE_USERNAME|PASSWORD|USERNAME|TOKEN|SECRET|COOKIE)\s*=\s*\S+"
)
_URL_SECRET_PARAMS = re.compile(
    r"([?&])(token|api_key|apikey|secret|password|access_token|auth|cookie|session)=[^&\s\"']+",
    re.IGNORECASE,
)
_JWT = re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}")
_LONG_HEX = re.compile(r"\b[a-f0-9]{32,}\b", re.IGNORECASE)
_COOKIE_PAIR = re.compile(r"(?i)(?:set-cookie|cookie)\s*[:=]\s*\S+")


def redact_text(text: str) -> str:
    """Redact common secret patterns from free-form text."""
    if not text:
        return text
    result = text
    result = _JWT.sub("[REDACTED_TOKEN]", result)
    result = _BEARER.sub("Bearer [REDACTED]", result)
    result = _AUTH_HEADER.sub("Authorization: [REDACTED]", result)
    result = _ENV_SECRET.sub(r"\1=[REDACTED]", result)
    result = _URL_SECRET_PARAMS.sub(r"\1\2=[REDACTED]", result)
    result = _COOKIE_PAIR.sub("[REDACTED_COOKIE]", result)
    result = _LONG_HEX.sub("[REDACTED_HASH]", result)
    return result


def safe_url_for_log(url: str | None, *, mark_safe: bool = False) -> str | None:
    """
    Return a log-safe URL representation.

    By default photo URLs are redacted to host + path shape only (no query params).
    Pass mark_safe=True when the URL is known to be non-sensitive (e.g. public CDN).
    """
    if not url:
        return None
    if mark_safe:
        return redact_text(url)
    try:
        parsed = urlparse(url)
        host = parsed.netloc or "[unknown-host]"
        path = parsed.path or "/"
        if len(path) > 80:
            path = f"{path[:40]}...{path[-20:]}"
        return f"{parsed.scheme or 'https'}://{host}{path}"
    except Exception:
        return "[REDACTED_URL]"
