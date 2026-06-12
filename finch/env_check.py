"""Finch environment checks — validates config without printing secrets."""

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import Enum


class CheckStatus(str, Enum):
    OK = "ok"
    MISSING = "missing"
    WARN = "warn"


@dataclass(frozen=True)
class EnvCheck:
    name: str
    status: CheckStatus
    message: str
    required_for: str


def _is_set(name: str) -> bool:
    return bool(os.getenv(name, "").strip())


def _live_cart_enabled() -> bool:
    return os.getenv("FINCH_LIVE_CART", "").strip().lower() in ("1", "true", "yes")


def run_env_checks() -> list[EnvCheck]:
    """Return status for each Finch Kroger env var (values never included)."""
    checks: list[EnvCheck] = []

    for var, purpose in (
        ("FINCH_KROGER_CLIENT_ID", "live Kroger API (search)"),
        ("FINCH_KROGER_CLIENT_SECRET", "live Kroger API (search)"),
        ("FINCH_KROGER_LOCATION_ID", "priced product search at your store"),
        ("FINCH_KROGER_REDIRECT_URI", "future cart add (OAuth — not needed for search)"),
    ):
        if _is_set(var):
            checks.append(EnvCheck(var, CheckStatus.OK, "is set", purpose))
        else:
            checks.append(
                EnvCheck(var, CheckStatus.MISSING, "not set", purpose)
            )

    if _live_cart_enabled():
        checks.append(
            EnvCheck(
                "FINCH_LIVE_CART",
                CheckStatus.WARN,
                "is ON — cart add enabled (checkout still manual)",
                "cart add only",
            )
        )
    else:
        checks.append(
            EnvCheck(
                "FINCH_LIVE_CART",
                CheckStatus.OK,
                "is off (default — cart add disabled)",
                "cart add only",
            )
        )

    return checks


def search_ready(checks: list[EnvCheck] | None = None) -> bool:
    """True when client credentials search can run."""
    checks = checks or run_env_checks()
    required = {"FINCH_KROGER_CLIENT_ID", "FINCH_KROGER_CLIENT_SECRET"}
    return all(c.status == CheckStatus.OK for c in checks if c.name in required)


def search_with_prices_ready(checks: list[EnvCheck] | None = None) -> bool:
    checks = checks or run_env_checks()
    return search_ready(checks) and any(
        c.name == "FINCH_KROGER_LOCATION_ID" and c.status == CheckStatus.OK for c in checks
    )


def format_check_line(check: EnvCheck) -> str:
    tag = check.status.value.upper()
    return f"[{tag}] {check.name}: {check.message}  ({check.required_for})"
