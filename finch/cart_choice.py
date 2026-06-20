"""Kroger search-and-choose flow for unresolved grocery items."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from finch.aliases import delete_aliases_matching_normalized, upsert_alias
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
    search_page_size,
    update_pending_selection_page,
)
from finch.preference_norm import normalize_preference_key
from finch.preview import resolve_intent
from finch.search import product_to_alias, run_search

logger = logging.getLogger(__name__)


def search_result_limit() -> int:
    """Page size for search-and-choose results (default 10)."""
    return search_page_size()


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
    start: int = 0,
) -> tuple[list[PendingSearchResult], int | None]:
    page_size = limit or search_result_limit()
    search_result = run_search(
        query,
        limit=page_size,
        start=start,
        client=client,
    )
    results = [product_to_pending_result(product) for product in search_result.products]
    return results, search_result.total_count


@dataclass(frozen=True)
class NeedsChoiceOutcome:
    requested_item: str
    normalized_name: str
    search_query: str
    quantity: int
    results: list[PendingSearchResult]
    pending: PendingSelection | None = None
    page_start: int = 1
    page_end: int = 0
    has_more: bool = False
    has_back: bool = False
    total_count: int | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "needs_choice": True,
            "ok": False,
            "requested_item": self.requested_item,
            "normalized_name": self.normalized_name,
            "search_query": self.search_query,
            "quantity": self.quantity,
            "results": [item.to_dict() for item in self.results],
            "page_start": self.page_start,
            "page_end": self.page_end,
            "has_more": self.has_more,
            "has_back": self.has_back,
            "total_count": self.total_count,
        }
        if self.pending is not None:
            payload["pending"] = self.pending.to_dict()
        return payload


def _needs_choice_from_pending(pending: PendingSelection) -> NeedsChoiceOutcome:
    return NeedsChoiceOutcome(
        requested_item=pending.requested_item,
        normalized_name=pending.normalized_name,
        search_query=pending.search_query,
        quantity=pending.quantity,
        results=pending.results,
        pending=pending,
        page_start=pending.page_start,
        page_end=pending.page_end,
        has_more=pending.has_more,
        has_back=pending.has_back,
        total_count=pending.total_count,
    )


def build_needs_choice_outcome(
    *,
    requested_item: str,
    normalized_name: str,
    search_query: str,
    quantity: int,
    cached_results: list[PendingSearchResult],
    total_count: int | None = None,
    page_offset: int = 0,
    page_size: int | None = None,
    chat_key: str | None = None,
    pending_db_path: Path | None = None,
) -> NeedsChoiceOutcome:
    pending = None
    resolved_page_size = page_size or search_result_limit()
    if chat_key:
        pending = save_pending_selection(
            chat_key=chat_key,
            requested_item=requested_item,
            normalized_name=normalized_name,
            search_query=search_query,
            quantity=quantity,
            cached_results=cached_results,
            page_offset=page_offset,
            page_size=resolved_page_size,
            total_count=total_count,
            db_path=pending_db_path,
        )
    page_results = cached_results[
        page_offset * resolved_page_size : page_offset * resolved_page_size + resolved_page_size
    ]
    page_start = page_offset * resolved_page_size + 1 if page_results else 0
    page_end = page_start + len(page_results) - 1 if page_results else 0
    has_more = False
    has_back = page_offset > 0
    if page_results:
        next_page_start = (page_offset + 1) * resolved_page_size
        if next_page_start < len(cached_results):
            has_more = True
        elif total_count is not None:
            has_more = len(cached_results) < total_count
        else:
            has_more = len(cached_results) % resolved_page_size == 0

    return NeedsChoiceOutcome(
        requested_item=requested_item,
        normalized_name=normalized_name,
        search_query=search_query,
        quantity=quantity,
        results=page_results,
        pending=pending,
        page_start=page_start,
        page_end=page_end,
        has_more=has_more,
        has_back=has_back,
        total_count=total_count,
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
    results, total_count = search_products_for_choice(search_query, client=client)
    return build_needs_choice_outcome(
        requested_item=requested_item,
        normalized_name=normalized_name,
        search_query=search_query,
        quantity=qty,
        cached_results=results,
        total_count=total_count,
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

    canonical = normalize_preference_key(pending.normalized_name)
    delete_aliases_matching_normalized(canonical, db_path=db_path)

    product = pending_result_to_product(result)
    entry = product_to_alias(
        canonical,
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
            product_size=result.size,
            product_price=result.price,
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

    if selection < 1 or selection > len(pending.cached_results):
        visible = f"{pending.page_start}-{pending.page_end}" if pending.results else "none"
        return {
            "ok": False,
            "message": (
                f"Pick a number between {pending.page_start} and {pending.page_end} "
                f"(showing {visible})."
            ),
        }

    result = pending.cached_results[selection - 1]
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
            results, total_count = search_products_for_choice(search_query, client=client)
            return AddListProgress(
                added_outcomes=added_outcomes,
                needs_choice=build_needs_choice_outcome(
                    requested_item=requested_item,
                    normalized_name=normalized_name,
                    search_query=search_query,
                    quantity=qty,
                    cached_results=results,
                    total_count=total_count,
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

    results, total_count = search_products_for_choice(cleaned, client=client)
    return build_needs_choice_outcome(
        requested_item=pending.requested_item,
        normalized_name=pending.normalized_name,
        search_query=cleaned,
        quantity=pending.quantity,
        cached_results=results,
        total_count=total_count,
        page_offset=0,
        page_size=pending.page_size,
        chat_key=chat_key,
        pending_db_path=pending_db_path,
    )


def paginate_pending_more(
    *,
    chat_key: str,
    client: KrogerClient,
    pending_db_path: Path | None = None,
) -> NeedsChoiceOutcome | dict[str, Any]:
    pending = get_pending_selection(chat_key, db_path=pending_db_path)
    if not pending:
        return {
            "ok": False,
            "message": "No pending product choice. Try add <item> first.",
        }

    if not pending.has_more:
        return {
            "ok": False,
            "message": "No more search results.",
        }

    next_page_offset = pending.page_offset + 1
    next_page_start = next_page_offset * pending.page_size
    cached_results = list(pending.cached_results)
    total_count = pending.total_count

    if next_page_start >= len(cached_results):
        logger.info(
            "Fetching more Kroger search results chat_key=%s query=%r start=%d limit=%d",
            chat_key,
            pending.search_query,
            len(cached_results),
            pending.page_size,
        )
        new_results, fetched_total = search_products_for_choice(
            pending.search_query,
            client=client,
            limit=pending.page_size,
            start=len(cached_results),
        )
        if fetched_total is not None:
            total_count = fetched_total
        if not new_results:
            return {
                "ok": False,
                "message": "No more search results.",
            }
        cached_results.extend(new_results)
    else:
        logger.info(
            "Showing cached Kroger search page chat_key=%s query=%r page_offset=%d",
            chat_key,
            pending.search_query,
            next_page_offset,
        )

    updated = update_pending_selection_page(
        pending,
        page_offset=next_page_offset,
        cached_results=cached_results,
        total_count=total_count,
        db_path=pending_db_path,
    )
    logger.info(
        "Advanced search pagination chat_key=%s query=%r page=%d-%d cached=%d total=%s",
        chat_key,
        pending.search_query,
        updated.page_start,
        updated.page_end,
        len(updated.cached_results),
        updated.total_count,
    )
    return _needs_choice_from_pending(updated)


def paginate_pending_back(
    *,
    chat_key: str,
    pending_db_path: Path | None = None,
) -> NeedsChoiceOutcome | dict[str, Any]:
    pending = get_pending_selection(chat_key, db_path=pending_db_path)
    if not pending:
        return {
            "ok": False,
            "message": "No pending product choice. Try add <item> first.",
        }

    if not pending.has_back:
        return {
            "ok": False,
            "message": "Already on the first page of results.",
        }

    updated = update_pending_selection_page(
        pending,
        page_offset=pending.page_offset - 1,
        db_path=pending_db_path,
    )
    logger.info(
        "Moved back in search pagination chat_key=%s query=%r page=%d-%d",
        chat_key,
        pending.search_query,
        updated.page_start,
        updated.page_end,
    )
    return _needs_choice_from_pending(updated)
