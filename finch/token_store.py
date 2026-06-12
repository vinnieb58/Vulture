"""Finch OAuth token storage — outside git, restrictive permissions."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from finch.config import DATA_DIR

FINCH_TOKENS_PATH = Path(
    os.getenv("FINCH_TOKENS_PATH", str(DATA_DIR / "finch_tokens.json"))
)

_TOKEN_FILE_MODE = 0o600


@dataclass(frozen=True)
class StoredTokens:
    access_token: str
    refresh_token: str | None = None
    expires_in: int | None = None
    token_type: str | None = None
    scope: str | None = None
    saved_at: str | None = None

    def is_expired(self, *, skew_seconds: int = 60) -> bool:
        if not self.expires_in or not self.saved_at:
            return False
        try:
            saved = datetime.fromisoformat(self.saved_at)
        except ValueError:
            return False
        if saved.tzinfo is None:
            saved = saved.replace(tzinfo=timezone.utc)
        expiry = saved + timedelta(seconds=self.expires_in - skew_seconds)
        return datetime.now(timezone.utc) >= expiry


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_tokens(tokens_path: Path | None = None) -> StoredTokens | None:
    path = tokens_path or FINCH_TOKENS_PATH
    if not path.exists():
        return None
    try:
        with path.open(encoding="utf-8") as fh:
            data = json.load(fh)
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(data, dict):
        return None
    access = str(data.get("access_token", "")).strip()
    if not access:
        return None
    refresh = str(data.get("refresh_token", "")).strip() or None
    expires_raw = data.get("expires_in")
    expires_in = int(expires_raw) if expires_raw is not None else None
    return StoredTokens(
        access_token=access,
        refresh_token=refresh,
        expires_in=expires_in,
        token_type=data.get("token_type"),
        scope=data.get("scope"),
        saved_at=data.get("saved_at"),
    )


def save_tokens_from_response(
    token_response: dict[str, Any],
    *,
    tokens_path: Path | None = None,
) -> StoredTokens:
    """Persist token response from Kroger OAuth (access + optional refresh)."""
    access = str(token_response.get("access_token", "")).strip()
    if not access:
        raise ValueError("Token response missing access_token")

    refresh_raw = token_response.get("refresh_token")
    refresh = str(refresh_raw).strip() if refresh_raw else None
    expires_raw = token_response.get("expires_in")
    expires_in = int(expires_raw) if expires_raw is not None else None

    existing = load_tokens(tokens_path)
    if not refresh and existing:
        refresh = existing.refresh_token

    payload = {
        "access_token": access,
        "refresh_token": refresh,
        "expires_in": expires_in,
        "token_type": token_response.get("token_type"),
        "scope": token_response.get("scope"),
        "saved_at": _now_iso(),
    }
    path = tokens_path or FINCH_TOKENS_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2)
        fh.write("\n")
    try:
        os.chmod(path, _TOKEN_FILE_MODE)
    except OSError:
        pass

    return StoredTokens(
        access_token=access,
        refresh_token=refresh,
        expires_in=expires_in,
        token_type=payload.get("token_type"),
        scope=payload.get("scope"),
        saved_at=payload["saved_at"],
    )


def has_saved_tokens(tokens_path: Path | None = None) -> bool:
    return load_tokens(tokens_path) is not None


def resolve_user_access_token(tokens_path: Path | None = None) -> str | None:
    """Return user token from .env override, then finch_tokens.json."""
    env_token = os.getenv("FINCH_KROGER_USER_ACCESS_TOKEN", "").strip()
    if env_token:
        return env_token
    stored = load_tokens(tokens_path)
    return stored.access_token if stored else None
