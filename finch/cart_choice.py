"""Kroger search-and-choose flow for unresolved grocery items."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from finch.aliases import upsert_alias
from finch.cart_ops import (
    CartAttempt,
    CartResolveError,
    check_trip_duplicate,
    execute_cart_add,
    parse_add_item,
    record_successful_trip_add,
    resolve_cart_item,
)
from finch.config import KROGER_CART_MODALITY
from finch.kroger_client import KrogerClient, KrogerProduct
from finch.models import MatchStatus
from finch.parser import parse_grocery_text
from finch.pending_selection import (
    PendingSearchResult,
    PendingSelection,
    clear_pending_selection,
    get_pending_selection,
    save_pending_selection,
)
from finch.preview import resolve_intent
from finch.search import product_to_alias, run_search


def search_result_limit() -> int:
    raw = os.getenv("FINCH_SEARCH_RESULT_LIMIT", "5")
    try:
        limit = int(raw)
    except ValueError:
        limit = 5
    return max(1, min(limit, 10))


def product_to_pending_result(product: KrogerProduct) -> PendingSearchResult:
    return PendingSearchResult(
        product_id=product.product_id,
        upc=product.upc,
        description=product.description,
        brand=product.brand,
        size=product.size,
        price=product.format_price(),
    )


def pending_result_to_product(result: PendingSearchResult) -> KrogerProduct:
    return KrogerProduct(
        product_id=result.product_id,
        upc=result.upc,
        description=result.description,
        brand=result.brand,
        size=result.size,
        price=result.price,
    )


def attempt_from_pending_result(
    pending: PendingSelection,
    result: PendingSearchResult,
    *,
    alias_name: str | None = None,
) -> CartAttempt:
    if not result.upc:
        raise CartResolveError(
            f"Selected product {result.description!r} has no UPC and cannot be added to cart."
        )
    return CartAttempt(
        requested_item=pending.requested_item,
        normalized_name=pending.normalized_name,
        alias_name=alias_name or result.description,
        upc=result.upc,
        product_id=result.product_id,
        quantity=pending.quantity,
        modality=KROGER_CART_MODALITY,
        status=MatchStatus.EXACT_DEFAULT,
    )


def intent_needs_product_choice(
    item_text: str,
    *,
    quantity: int | None = None,
    db_path: Path | None = None,
) -> tuple[str, str, str, int] | None:
    """Return (requested_item, normalized_name, search_query, quantity) or None if resolved."""
    intents = parse_grocery_text(item_text)
    if not intents:
        raise CartResolveError(f"Could not parse item: {item_text!r}")
    if len(intents) > 1:
        raise CartResolveError(
            f"Multiple items parsed from {item_text!r}; use add-list or pass one item at a time."
        )

    intent = intents[0]
    line = resolve_intent(intent, db_path=db_path)
    if line.status == MatchStatus.EXACT_DEFAULT and line.upc:
        return None

    if quantity is not None:
        qty = int(quantity)
    else:
        qty = max(1, int(intent.quantity))
    if qty < 1:
        raise CartResolveError("Quantity must be at least 1.")

    search_query = line.search_term or intent.normalized_name
    return intent.raw_text, intent.normalized_name, search_query, qty


def search_products_for_choice(
    query: str,
    *,
    client: KrogerClient,
    limit: int | None = None,
) -> list[PendingSearchResult]:
    products = run_search(query, limit=limit or search_result_limit(), client=client)
    return [product_to_pending_result(product) for product in products]


@dataclass(frozen=True)
class NeedsChoiceOutcome:
    requested_item: str
    normalized_name: str
    search_query: str
    quantity: int
    results: list[PendingSearchResult]
    pending: PendingSelection | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "needs_choice": True,
            "ok": False,
            "requested_item": self.requested_item,
            "normalized_name": self.normalized_name,
            "search_query": self.search_query,
            "quantity": self.quantity,
            "results": [item.to_dict() for item in self.results],
        }
        if self.pending is not None:
            payload["pending"] = self.pending.to_dict()
        return payload


def build_needs_choice_outcome(
    *,
    requested_item: str,
    normalized_name: str,
    search_query: str,
    quantity: int,
    results: list[PendingSearchResult],
    chat_key: str | None = None,
    pending_db_path: Path | None = None,
) -> NeedsChoiceOutcome:
    pending = None
    if chat_key:
        pending = save_pending_selection(
            chat_key=chat_key,
            requested_item=requested_item,
            normalized_name=normalized_name,
            search_query=search_query,
            quantity=quantity,
            results=results,
            db_path=pending_db_path,
        )
    return NeedsChoiceOutcome(
        requested_item=requested_item,
        normalized_name=normalized_name,
        search_query=search_query,
        quantity=quantity,
        results=results,
        pending=pending,
    )


def prepare_add_or_needs_choice(
    item_text: str,
    *,
    quantity: int | None = None,
    chat_key: str | None = None,
    client: KrogerClient,
    db_path: Path | None = None,
    pending_db_path: Path | None = None,
) -> CartAttempt | NeedsChoiceOutcome:
    parsed_text, force = parse_add_item(item_text)
    if force:
        return resolve_cart_item(parsed_text, quantity=quantity, db_path=db_path)

    needs = intent_needs_product_choice(
        parsed_text,
        quantity=quantity,
        db_path=db_path,
    )
    if needs is None:
        return resolve_cart_item(parsed_text, quantity=quantity, db_path=db_path)

    requested_item, normalized_name, search_query, qty = needs
    results = search_products_for_choice(search_query, client=client)
    return build_needs_choice_outcome(
        requested_item=requested_item,
        normalized_name=normalized_name,
        search_query=search_query,
        quantity=qty,
        results=results,
        chat_key=chat_key,
        pending_db_path=pending_db_path,
    )


def save_preference_from_pending(
    pending: PendingSelection,
    result: PendingSearchResult,
    *,
    db_path: Path | None = None,
) -> None:
    from finch.models import AliasEntry

    product = pending_result_to_product(result)
    entry = product_to_alias(
        pending.normalized_name,
        product,
        search_term=pending.search_query,
    )
    upsert_alias(
        AliasEntry(
            alias_key=entry.alias_key,
            display_name=entry.display_name,
            kroger_product_id=entry.kroger_product_id,
            upc=entry.upc,
            search_term=entry.search_term,
            notes="Pinned via Finch search selection",
        ),
        db_path,
    )


def execute_pending_choice(
    *,
    chat_key: str,
    selection: int,
    prefer: bool = False,
    force: bool = False,
    source: str | None = None,
    client: KrogerClient,
    db_path: Path | None = None,
    pending_db_path: Path | None = None,
    trip_ledger_db_path: Path | None = None,
) -> dict[str, Any]:
    pending = get_pending_selection(chat_key, db_path=pending_db_path)
    if not pending:
        return {
            "ok": False,
            "message": "No pending product choice. Try add <item> first.",
        }

    if selection < 1 or selection > len(pending.results):
        return {
            "ok": False,
            "message": f"Pick a number between 1 and {len(pending.results)}.",
        }

    result = pending.results[selection - 1]
    try:
        attempt = attempt_from_pending_result(pending, result)
    except CartResolveError as exc:
        return {"ok": False, "message": str(exc)}

    if prefer:
        save_preference_from_pending(pending, result, db_path=db_path)

    duplicate_msg = check_trip_duplicate(
        attempt,
        force=force,
        trip_ledger_db_path=trip_ledger_db_path,
    )
    if duplicate_msg:
        return {
            "ok": False,
            "duplicate": True,
            "message": duplicate_msg,
            "attempt": {
                "requested_item": attempt.requested_item,
                "normalized_name": attempt.normalized_name,
                "alias_name": attempt.alias_name,
                "upc": attempt.upc,
                "product_id": attempt.product_id,
                "quantity": attempt.quantity,
            },
        }

    try:
        cart_result = execute_cart_add(attempt, client)
    except Exception as exc:
        return {"ok": False, "message": str(exc)}

    record_successful_trip_add(
        attempt,
        source=source,
        trip_ledger_db_path=trip_ledger_db_path,
    )
    clear_pending_selection(chat_key, db_path=pending_db_path)

    status = cart_result.get("status", "ok") if isinstance(cart_result, dict) else "ok"
    outcome: dict[str, Any] = {
        "ok": True,
        "duplicate": False,
        "preferred": prefer,
        "attempt": {
            "requested_item": attempt.requested_item,
            "normalized_name": attempt.normalized_name,
            "alias_name": attempt.alias_name,
            "upc": attempt.upc,
            "product_id": attempt.product_id,
            "quantity": attempt.quantity,
        },
        "result": f"ok ({status})",
    }
    return outcome


@dataclass(frozen=True)
class AddListProgress:
    added_outcomes: list[dict[str, Any]]
    needs_choice: NeedsChoiceOutcome | None = None


def process_add_list_with_choice(
    list_text: str,
    *,
    chat_key: str | None = None,
    client: KrogerClient,
    db_path: Path | None = None,
    pending_db_path: Path | None = None,
    trip_ledger_db_path: Path | None = None,
    source: str | None = None,
    execute_add_fn,
) -> AddListProgress:
    """Add resolved items; stop at the first unresolved item with search results."""
    from finch.aliases import ensure_seeded

    ensure_seeded(db_path)
    intents = parse_grocery_text(list_text)
    if not intents:
        raise CartResolveError(f"Could not parse list: {list_text!r}")

    added_outcomes: list[dict[str, Any]] = []
    for intent in intents:
        needs = intent_needs_product_choice(
            intent.raw_text,
            quantity=None,
            db_path=db_path,
        )
        if needs is not None:
            requested_item, normalized_name, search_query, qty = needs
            results = search_products_for_choice(search_query, client=client)
            return AddListProgress(
                added_outcomes=added_outcomes,
                needs_choice=build_needs_choice_outcome(
                    requested_item=requested_item,
                    normalized_name=normalized_name,
                    search_query=search_query,
                    quantity=qty,
                    results=results,
                    chat_key=chat_key,
                    pending_db_path=pending_db_path,
                ),
            )

        attempt = resolve_cart_item(intent.raw_text, db_path=db_path)
        outcome = execute_add_fn(attempt)
        added_outcomes.append(outcome)
        if not outcome.get("ok") and not outcome.get("duplicate"):
            break

    return AddListProgress(added_outcomes=added_outcomes)


def rerun_pending_search(
    *,
    chat_key: str,
    query: str,
    client: KrogerClient,
    pending_db_path: Path | None = None,
) -> NeedsChoiceOutcome | dict[str, Any]:
    pending = get_pending_selection(chat_key, db_path=pending_db_path)
    if not pending:
        return {
            "ok": False,
            "message": "No pending product choice. Try add <item> first.",
        }

    cleaned = query.strip()
    if not cleaned:
        return {"ok": False, "message": "Search query cannot be empty."}

    results = search_products_for_choice(cleaned, client=client)
    return build_needs_choice_outcome(
        requested_item=pending.requested_item,
        normalized_name=pending.normalized_name,
        search_query=cleaned,
        quantity=pending.quantity,
        results=results,
        chat_key=chat_key,
        pending_db_path=pending_db_path,
    )
