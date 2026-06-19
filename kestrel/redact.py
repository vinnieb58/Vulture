"""Safe redaction helpers for Kestrel logs and status messages."""

from __future__ import annotations

import re
from typing import Any

_BEARER = re.compile(r"(?i)\bbearer\s+\S+")
_AUTH_HEADER = re.compile(r"(?i)\bauthorization\s*[:=]\s*\S+")
_ENV_SECRET = re.compile(
    r"(?i)\b(KESTREL_SMT_PASSWORD|KESTREL_SMT_USERNAME|PASSWORD|USERNAME|TOKEN|SECRET|COOKIE)\s*=\s*\S+"
)
_URL_SECRET_PARAMS = re.compile(
    r"([?&])(token|api_key|apikey|secret|password|access_token|auth|cookie)=[^&\s\"']+",
    re.IGNORECASE,
)
_JWT = re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}")
_GOOGLE_ACCESS_TOKEN = re.compile(r"\bya29\.[A-Za-z0-9._-]+\b")
_ESIID = re.compile(r"\b\d{15,22}\b")
_LONG_HEX = re.compile(r"\b[a-f0-9]{32,}\b", re.IGNORECASE)
_ABSOLUTE_PATH = re.compile(r"(?i)(/[\w./-]+|~[\w./-]+|\\\\[\w\\.-]+)")
_COOKIE_PAIR = re.compile(r"(?i)(?:set-cookie|cookie)\s*[:=]\s*\S+")


def redact_text(text: str) -> str:
    """Redact common secret and identifier patterns from free-form text."""
    if not text:
        return text
    result = text
    result = _JWT.sub("[REDACTED_TOKEN]", result)
    result = _GOOGLE_ACCESS_TOKEN.sub("[REDACTED_TOKEN]", result)
    result = _BEARER.sub("Bearer [REDACTED]", result)
    result = _AUTH_HEADER.sub("Authorization: [REDACTED]", result)
    result = _ENV_SECRET.sub(r"\1=[REDACTED]", result)
    result = _URL_SECRET_PARAMS.sub(r"\1\2=[REDACTED]", result)
    result = _COOKIE_PAIR.sub("[REDACTED_COOKIE]", result)
    result = _ESIID.sub("[REDACTED_ESIID]", result)
    result = _LONG_HEX.sub("[REDACTED_HASH]", result)
    result = _ABSOLUTE_PATH.sub("[REDACTED_PATH]", result)
    return result


def redact_value(value: Any) -> Any:
    """Recursively redact sensitive values in nested structures for safe logging."""
    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            key_lower = str(key).lower()
            if key_lower in {
                "password",
                "username",
                "token",
                "authorization",
                "cookie",
                "cookies",
                "esiid",
                "accountid",
                "account_id",
                "meternumber",
                "meter_number",
            }:
                redacted[key] = "[REDACTED]"
            elif key_lower.endswith("_hash") or "hash" in key_lower:
                redacted[key] = "[REDACTED_HASH]"
            else:
                redacted[key] = redact_value(item)
        return redacted
    if isinstance(value, list):
        return [redact_value(item) for item in value]
    if isinstance(value, str):
        return redact_text(value)
    return value


def describe_payload_shape(payload: Any, *, max_depth: int = 4) -> str:
    """Return a safe structural description of an API payload (keys/types only)."""

    def _shape(value: Any, depth: int) -> Any:
        if depth > max_depth:
            return "..."
        if isinstance(value, dict):
            return {str(key): _shape(item, depth + 1) for key, item in value.items()}
        if isinstance(value, list):
            if not value:
                return []
            return [_shape(value[0], depth + 1), f"...({len(value)} items)"]
        if isinstance(value, str):
            if len(value) > 40:
                return f"str(len={len(value)})"
            return f"str({redact_text(value)!r})"
        if value is None:
            return None
        return type(value).__name__

    import json

    return json.dumps(redact_value(_shape(payload, 0)), sort_keys=True)
