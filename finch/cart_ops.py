"""Cart add helpers — alias resolution and guarded Kroger cart mutation."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from finch.aliases import ensure_seeded, get_all_aliases
from finch.config import KROGER_CART_MODALITY
from finch.kroger_client import KrogerAuthError, KrogerCartDisabledError, KrogerClient, KrogerError
from finch.models import MatchStatus
from finch.parser import parse_grocery_text
from finch.preview import resolve_intent
from finch.token_store import (
    StoredTokens,
    load_tokens,
    resolve_user_access_token,
    save_tokens_from_response,
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


def resolve_cart_item(
    item_text: str,
    *,
    quantity: int = 1,
    db_path: Path | None = None,
) -> CartAttempt:
    ensure_seeded(db_path)
    intents = parse_grocery_text(item_text)
    if not intents:
        raise CartResolveError(f"Could not parse item: {item_text!r}")
    if len(intents) > 1:
        raise CartResolveError(
            f"Multiple items parsed from {item_text!r}; pass one item at a time."
        )

    intent = intents[0]
    line = resolve_intent(intent, db_path=db_path)

    if line.status == MatchStatus.AMBIGUOUS:
        raise CartResolveError(
            f"Ambiguous alias for {item_text!r}. Pin a preferred product first: "
            f"python -m finch.search \"{intent.normalized_name}\" --save-alias {intent.normalized_name!r} --pick N --confirm"
        )
    if line.status == MatchStatus.MISSING:
        raise CartResolveError(
            f"No alias for {item_text!r}. Search and pin first: "
            f"python -m finch.search \"{intent.normalized_name}\" --save-alias ..."
        )
    if not line.upc:
        raise CartResolveError(
            f"Alias for {item_text!r} has no UPC. Pin a product with: "
            f"python -m finch.search \"{line.search_term or intent.normalized_name}\" --save-alias {intent.normalized_name!r} --pick N --confirm"
        )

    qty = int(quantity) if quantity else 1
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


def execute_cart_add(
    attempt: CartAttempt,
    client: KrogerClient,
    *,
    live: bool | None = None,
) -> dict[str, Any]:
    allow = live_cart_enabled() if live is None else live
    if not allow:
        raise CartGuardError(
            "Cart add is disabled. Set FINCH_LIVE_CART=true in .env after reviewing aliases."
        )
    return client.add_to_cart(
        attempt.upc,
        quantity=attempt.quantity,
        modality=attempt.modality,
        live=True,
    )


def format_attempt_result(attempt: CartAttempt, *, result: str) -> str:
    lines = ["Cart add attempt:"] + attempt.summary_lines() + [f"  result: {result}"]
    return "\n".join(lines)
