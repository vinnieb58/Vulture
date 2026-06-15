"""Environment configuration for the Finch WhatsApp bridge."""

from __future__ import annotations

import os


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in ("1", "true", "yes")


def test_mode() -> bool:
    return _truthy(os.getenv("FINCH_WHATSAPP_TEST_MODE"))


def verify_token() -> str:
    return os.getenv("FINCH_WHATSAPP_VERIFY_TOKEN", "").strip()


def access_token() -> str:
    return os.getenv("FINCH_WHATSAPP_ACCESS_TOKEN", "").strip()


def phone_number_id() -> str:
    return os.getenv("FINCH_WHATSAPP_PHONE_NUMBER_ID", "").strip()


def allowed_numbers() -> frozenset[str]:
    raw = os.getenv("FINCH_WHATSAPP_ALLOWED_NUMBERS", "")
    numbers = {_normalize_phone(part) for part in raw.split(",") if part.strip()}
    return frozenset(n for n in numbers if n)


def finch_api_base_url() -> str:
    return os.getenv("FINCH_API_BASE_URL", "http://127.0.0.1:8091").rstrip("/")


def finch_api_key() -> str:
    return os.getenv("FINCH_API_KEY", "").strip()


def bind_host() -> str:
    return os.getenv("FINCH_WHATSAPP_HOST", "127.0.0.1").strip() or "127.0.0.1"


def bind_port() -> int:
    return int(os.getenv("FINCH_WHATSAPP_PORT", "8092"))


def graph_api_version() -> str:
    return os.getenv("FINCH_WHATSAPP_GRAPH_API_VERSION", "v21.0").strip() or "v21.0"


def _normalize_phone(value: str) -> str:
    return "".join(ch for ch in value.strip() if ch.isdigit())


def is_allowed_sender(sender: str) -> bool:
    normalized = _normalize_phone(sender)
    if not normalized:
        return False
    allowed = allowed_numbers()
    if not allowed:
        return False
    return normalized in allowed


def validate_startup() -> None:
    if test_mode():
        return
    missing: list[str] = []
    if not verify_token():
        missing.append("FINCH_WHATSAPP_VERIFY_TOKEN")
    if not access_token():
        missing.append("FINCH_WHATSAPP_ACCESS_TOKEN")
    if not phone_number_id():
        missing.append("FINCH_WHATSAPP_PHONE_NUMBER_ID")
    if not allowed_numbers():
        missing.append("FINCH_WHATSAPP_ALLOWED_NUMBERS")
    if not finch_api_key():
        missing.append("FINCH_API_KEY")
    if missing:
        raise RuntimeError(
            "Missing required WhatsApp bridge configuration: "
            + ", ".join(missing)
            + ". Set FINCH_WHATSAPP_TEST_MODE=1 for tests."
        )
