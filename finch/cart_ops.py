"""Cart add helpers — alias resolution and guarded Kroger cart mutation."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from finch.activity import log_activity
from finch.aliases import ensure_seeded, get_all_aliases
from finch.config import KROGER_CART_MODALITY
from finch.kroger_client import KrogerAuthError, KrogerClient, KrogerError
from finch.models import GroceryIntent, MatchStatus
from finch.parser import parse_grocery_text
from finch.preview import resolve_intent
from finch.token_store import (
    load_tokens,
    resolve_user_access_token,
    save_tokens_from_response,
)
from finch.preference_norm import normalize_preference_key
from finch.trip_ledger import (
    find_trip_duplicate,
    format_duplicate_message,
    get_or_create_open_trip,
    record_trip_add,
)

_FORCE_ADD_PREFIX_RE = re.compile(r"^force\s+add\s+", re.IGNORECASE)
_FORCE_ADD_AGAIN_RE = re.compile(
    r"^(?:force\s+add|add)\s+(.+?)\s+again$",
    re.IGNORECASE,
)


class CartResolveError(Exception):
    """Could not map a grocery term to a cart-ready UPC."""


class CartGuardError(Exception):
    """Cart operation blocked by Finch guardrails."""


@dataclass(frozen=True)
class CartAttempt:
    requested_item: str
    normalized_name: str
    alias_name: str | None
    upc: str
    product_id: str | None
    quantity: int
    modality: str
    status: MatchStatus

    def summary_lines(self) -> list[str]:
        lines = [
            f"  requested: {self.requested_item!r}",
            f"  normalized: {self.normalized_name!r}",
        ]
        if self.alias_name:
            lines.append(f"  alias: {self.alias_name!r}")
        lines.extend(
            [
                f"  upc: {self.upc}",
                f"  quantity: {self.quantity}",
                f"  modality: {self.modality}",
            ]
        )
        if self.product_id:
            lines.append(f"  product_id: {self.product_id}")
        return lines


@dataclass(frozen=True)
class CartListResult:
    succeeded: list[CartAttempt]
    failed: list[tuple[str, str]]


def live_cart_enabled() -> bool:
    return os.getenv("FINCH_LIVE_CART", "").strip().lower() in ("1", "true", "yes")


def require_live_cart() -> None:
    if not live_cart_enabled():
        raise CartGuardError(
            "Cart add is disabled. Set FINCH_LIVE_CART=true in .env after reviewing aliases."
        )


def require_saved_token(tokens_path: Path | None = None) -> None:
    if not resolve_user_access_token(tokens_path):
        raise CartGuardError("No saved user token. Run: python -m finch.auth")


def _resolve_intent_to_attempt(
    intent: GroceryIntent,
    *,
    quantity_override: int | None = None,
    db_path: Path | None = None,
) -> CartAttempt:
    line = resolve_intent(intent, db_path=db_path)

    if line.status == MatchStatus.AMBIGUOUS:
        raise CartResolveError(
            f"Ambiguous alias for {intent.raw_text!r}. Pin a preferred product first: "
            f"python -m finch.search \"{intent.normalized_name}\" --save-alias {intent.normalized_name!r} --pick N --confirm"
        )
    if line.status == MatchStatus.MISSING:
        raise CartResolveError(
            f"No alias for {intent.raw_text!r}. Search and pin first: "
            f"python -m finch.search \"{intent.normalized_name}\" --save-alias ..."
        )
    if not line.upc:
        raise CartResolveError(
            f"Alias for {intent.raw_text!r} has no UPC. Pin a product with: "
            f"python -m finch.search \"{line.search_term or intent.normalized_name}\" --save-alias {intent.normalized_name!r} --pick N --confirm"
        )

    if quantity_override is not None:
        qty = int(quantity_override)
    else:
        qty = max(1, int(intent.quantity))

    if qty < 1:
        raise CartResolveError("Quantity must be at least 1.")

    return CartAttempt(
        requested_item=intent.raw_text,
        normalized_name=intent.normalized_name,
        alias_name=line.matched_alias,
        upc=line.upc,
        product_id=line.kroger_product_id,
        quantity=qty,
        modality=KROGER_CART_MODALITY,
        status=line.status,
    )


def resolve_cart_item(
    item_text: str,
    *,
    quantity: int | None = None,
    db_path: Path | None = None,
) -> CartAttempt:
    ensure_seeded(db_path)
    intents = parse_grocery_text(item_text)
    if not intents:
        raise CartResolveError(f"Could not parse item: {item_text!r}")
    if len(intents) > 1:
        raise CartResolveError(
            f"Multiple items parsed from {item_text!r}; use add-list or pass one item at a time."
        )

    return _resolve_intent_to_attempt(intents[0], quantity_override=quantity, db_path=db_path)


def resolve_cart_list(
    list_text: str,
    *,
    db_path: Path | None = None,
) -> CartListResult:
    ensure_seeded(db_path)
    intents = parse_grocery_text(list_text)
    if not intents:
        raise CartResolveError(f"Could not parse list: {list_text!r}")

    succeeded: list[CartAttempt] = []
    failed: list[tuple[str, str]] = []
    for intent in intents:
        try:
            succeeded.append(_resolve_intent_to_attempt(intent, db_path=db_path))
        except CartResolveError as exc:
            failed.append((intent.raw_text, str(exc)))
    return CartListResult(succeeded=succeeded, failed=failed)


def pick_test_alias(db_path: Path | None = None) -> str | None:
    """Prefer 'eggs', else first alias with a UPC."""
    ensure_seeded(db_path)
    aliases = get_all_aliases(db_path)
    by_key = {a.alias_key: a for a in aliases}
    if "eggs" in by_key and by_key["eggs"].upc:
        return "eggs"
    for entry in aliases:
        if entry.upc:
            return entry.alias_key
    return None


def ensure_fresh_user_token(
    client: KrogerClient,
    *,
    tokens_path: Path | None = None,
) -> str:
    stored = load_tokens(tokens_path)
    if not stored:
        raise KrogerAuthError("No saved user token. Run: python -m finch.auth")

    if not stored.is_expired():
        client.set_user_access_token(stored.access_token)
        return stored.access_token

    if not stored.refresh_token:
        raise KrogerAuthError("Saved token expired and no refresh token. Run: python -m finch.auth")

    refreshed = client.refresh_user_token(stored.refresh_token)
    save_tokens_from_response(refreshed, tokens_path=tokens_path)
    access = str(refreshed["access_token"])
    client.set_user_access_token(access)
    return access


def parse_add_item(item_text: str) -> tuple[str, bool]:
    """Return (item_text, force_add) for add commands with optional force phrasing."""
    text = item_text.strip()
    match = _FORCE_ADD_AGAIN_RE.match(text)
    if match:
        return match.group(1).strip(), True
    if _FORCE_ADD_PREFIX_RE.match(text):
        return _FORCE_ADD_PREFIX_RE.sub("", text).strip(), True
    return text, False


def check_trip_duplicate(
    attempt: CartAttempt,
    *,
    force: bool = False,
    trip_ledger_db_path: Path | None = None,
) -> str | None:
    """Return a user-facing duplicate message, or None if the add may proceed."""
    if force:
        return None
    trip_id = get_or_create_open_trip(db_path=trip_ledger_db_path)
    duplicate = find_trip_duplicate(
        trip_id=trip_id,
        normalized_name=attempt.normalized_name,
        product_id=attempt.product_id,
        upc=attempt.upc,
        db_path=trip_ledger_db_path,
    )
    if duplicate:
        return format_duplicate_message(attempt.normalized_name)
    return None


def record_successful_trip_add(
    attempt: CartAttempt,
    *,
    source: str | None = None,
    trip_ledger_db_path: Path | None = None,
) -> None:
    trip_id = get_or_create_open_trip(db_path=trip_ledger_db_path)
    record_trip_add(
        trip_id=trip_id,
        normalized_name=attempt.normalized_name,
        display_name=attempt.alias_name,
        product_id=attempt.product_id,
        upc=attempt.upc,
        quantity=attempt.quantity,
        requested_text=attempt.requested_item,
        source=source,
        db_path=trip_ledger_db_path,
    )


def record_cart_activity(
    attempt: CartAttempt,
    *,
    action: str,
    result: str,
    activity_db_path: Path | None = None,
) -> None:
    log_activity(
        requested_text=attempt.requested_item,
        resolved_alias=attempt.alias_name,
        upc=attempt.upc,
        product_id=attempt.product_id,
        quantity=attempt.quantity,
        action=action,
        result=result,
        db_path=activity_db_path,
    )


def execute_cart_add(
    attempt: CartAttempt,
    client: KrogerClient,
    *,
    live: bool | None = None,
    action: str = "cart_add",
    activity_db_path: Path | None = None,
) -> dict[str, Any]:
    allow = live_cart_enabled() if live is None else live
    if not allow:
        raise CartGuardError(
            "Cart add is disabled. Set FINCH_LIVE_CART=true in .env after reviewing aliases."
        )
    try:
        result = client.add_to_cart(
            attempt.upc,
            quantity=attempt.quantity,
            modality=attempt.modality,
            live=True,
        )
        status = result.get("status", "ok") if isinstance(result, dict) else "ok"
        record_cart_activity(
            attempt,
            action=action,
            result=f"ok ({status})",
            activity_db_path=activity_db_path,
        )
        return result
    except Exception as exc:
        record_cart_activity(
            attempt,
            action=action,
            result=f"failed — {exc}",
            activity_db_path=activity_db_path,
        )
        raise


def format_attempt_result(attempt: CartAttempt, *, result: str) -> str:
    lines = ["Cart add attempt:"] + attempt.summary_lines() + [f"  result: {result}"]
    return "\n".join(lines)
