"""Environment configuration for the Finch Telegram bridge."""

from __future__ import annotations

import os


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in ("1", "true", "yes")


def test_mode() -> bool:
    return _truthy(os.getenv("FINCH_TELEGRAM_TEST_MODE"))


def bot_token() -> str:
    return os.getenv("FINCH_TELEGRAM_BOT_TOKEN", "").strip()


def allowed_user_ids() -> frozenset[str]:
    raw = os.getenv("FINCH_TELEGRAM_ALLOWED_USER_IDS", "")
    ids = {part.strip() for part in raw.split(",") if part.strip()}
    return frozenset(ids)


def whitelist_configured() -> bool:
    return bool(allowed_user_ids())


def finch_api_base_url() -> str:
    return os.getenv("FINCH_API_BASE_URL", "http://127.0.0.1:8091").rstrip("/")


def finch_api_key() -> str:
    return os.getenv("FINCH_API_KEY", "").strip()


def poll_timeout_seconds() -> int:
    return int(os.getenv("FINCH_TELEGRAM_POLL_TIMEOUT", "30"))


def is_allowed_user(user_id: str) -> bool:
    allowed = allowed_user_ids()
    if not allowed:
        return True
    return user_id in allowed


def validate_startup() -> None:
    if test_mode():
        return
    missing: list[str] = []
    if not bot_token():
        missing.append("FINCH_TELEGRAM_BOT_TOKEN")
    if not finch_api_key():
        missing.append("FINCH_API_KEY")
    if missing:
        raise RuntimeError(
            "Missing required Telegram bridge configuration: "
            + ", ".join(missing)
            + ". Set FINCH_TELEGRAM_TEST_MODE=1 for tests."
        )
