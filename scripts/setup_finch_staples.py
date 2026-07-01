#!/usr/bin/env python3
"""Interactive Finch staple preference setup — no cart or trip-ledger writes."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from finch.aliases import ensure_seeded
from finch.cart_choice import save_preference_from_pending, search_products_for_choice
from finch.cart_ops import CartResolveError
from finch.env_util import load_env
from finch.kroger_client import KrogerError, load_kroger_client_from_env
from finch.pending_selection import PendingSelection
from finch.staples import (
    build_staples_status_report,
    list_staple_items,
    resolve_staple_preference,
    seed_initial_staples,
)


def _search_staple_products(query: str, *, client, limit: int = 10):
    return search_products_for_choice(query, client=client, limit=limit)


def _format_product_line(index: int, result) -> str:
    description = str(result.description or "item").strip()
    brand = str(result.brand or "").strip()
    size = str(result.size or "").strip()
    price = str(result.price or "").strip()
    title = f"{brand} {description}".strip() if brand else description
    parts = [title]
    if size:
        parts.append(size)
    if price:
        parts.append(price)
    return f"{index}. {' — '.join(parts)}"


from finch.cart_choice import search_products_for_choice


def _save_preference_for_staple(
    staple,
    result,
    *,
    alias_db_path: Path | None,
) -> None:
    pending = PendingSelection(
        chat_key="setup-script",
        requested_item=staple.display_name,
        normalized_name=staple.normalized_key,
        search_query=staple.normalized_key,
        quantity=int(staple.default_quantity),
        cached_results=[result],
        page_offset=0,
        page_size=10,
        total_count=1,
        created_at="",
        expires_at="",
    )
    save_preference_from_pending(pending, result, db_path=alias_db_path)


def print_status(*, staples_db_path: Path | None, alias_db_path: Path | None) -> int:
    seed_initial_staples(staples_db_path)
    report = build_staples_status_report(
        staples_db_path=staples_db_path,
        alias_db_path=alias_db_path,
    )
    for row in report:
        status = "resolved" if row["preference_resolved"] else "unresolved"
        product = row["preferred_product"] or "—"
        enabled = "yes" if row["enabled"] else "no"
        qty = row["default_quantity"]
        if qty == int(qty):
            qty_text = str(int(qty))
        else:
            qty_text = f"{qty:g}"
        print(
            f"{row['display_name']}\n"
            f"  key: {row['normalized_key']}\n"
            f"  quantity: {qty_text}\n"
            f"  enabled: {enabled}\n"
            f"  preference: {status}\n"
            f"  product: {product}\n"
        )
    return 0


def _prompt_selection(
    staple,
    results,
    *,
    review_all: bool,
    input_fn=input,
    print_fn=print,
) -> str:
    current = resolve_staple_preference(staple)
    current_product = current.get("preferred_product") or "none"
    print_fn(f"\nStaple: {staple.normalized_key}")
    print_fn(f"Current preference: {current_product}")
    print_fn()
    for index, result in enumerate(results, start=1):
        print_fn(_format_product_line(index, result))
    print_fn()
    if review_all and current.get("preference_resolved"):
        prompt = (
            "Enter a number to replace this preference, "
            "k = keep current, s = search again, q = quit: "
        )
    else:
        prompt = "Enter a number to save this preference.\ns = search again\nk = keep unresolved\nq = quit and preserve progress\n\nChoice: "
    return input_fn(prompt).strip().lower()


def run_setup(
    *,
    review_all: bool = False,
    staples_db_path: Path | None = None,
    alias_db_path: Path | None = None,
    input_fn=input,
    print_fn=print,
) -> int:
    load_env()
    ensure_seeded(alias_db_path)
    seed_initial_staples(staples_db_path)

    staples = list_staple_items(enabled_only=True, db_path=staples_db_path)
    unresolved = [
        staple
        for staple in staples
        if not resolve_staple_preference(staple, alias_db_path=alias_db_path)[
            "preference_resolved"
        ]
    ]
    to_process = staples if review_all else unresolved

    if not to_process:
        print_fn("All enabled staples already have preferences.")
        return 0

    try:
        client = load_kroger_client_from_env()
    except (RuntimeError, KrogerError) as exc:
        print_fn(f"Error: {exc}", file=sys.stderr)
        return 1

    for staple in to_process:
        if not review_all:
            resolution = resolve_staple_preference(staple, alias_db_path=alias_db_path)
            if resolution["preference_resolved"]:
                continue

        search_query = staple.normalized_key
        while True:
            try:
                results, _total = _search_staple_products(search_query, client=client)
            except (RuntimeError, KrogerError, CartResolveError) as exc:
                print_fn(f"Search failed for {staple.display_name!r}: {exc}")
                return 1

            if not results:
                print_fn(f"No Kroger results for {search_query!r}.")
                retry = input_fn("New search term (or q to quit): ").strip()
                if retry.lower() == "q":
                    return 0
                search_query = retry
                continue

            choice = _prompt_selection(
                staple,
                results,
                review_all=review_all,
                input_fn=input_fn,
                print_fn=print_fn,
            )
            if choice == "q":
                return 0
            if choice == "k":
                break
            if choice == "s":
                retry = input_fn("Search term: ").strip()
                if retry:
                    search_query = retry
                continue
            if not choice.isdigit():
                print_fn("Invalid choice.")
                continue

            pick = int(choice)
            if pick < 1 or pick > len(results):
                print_fn(f"Pick a number between 1 and {len(results)}.")
                continue

            if review_all:
                current = resolve_staple_preference(staple, alias_db_path=alias_db_path)
                if current.get("preference_resolved"):
                    confirm = input_fn(
                        f"Replace preference for {staple.normalized_key!r}? [y/N] "
                    ).strip().lower()
                    if confirm not in ("y", "yes"):
                        break

            result = results[pick - 1]
            _save_preference_for_staple(staple, result, alias_db_path=alias_db_path)
            print_fn(f"Saved preference for {staple.normalized_key!r} → {result.description!r}")
            break

    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Set up Finch staple product preferences without adding to cart.",
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="Print staple status and exit without writing preferences.",
    )
    parser.add_argument(
        "--review-all",
        action="store_true",
        help="Review every enabled staple, including those with existing preferences.",
    )
    parser.add_argument(
        "--staples-db",
        type=Path,
        default=None,
        help="Override FINCH_STAPLES_DB_PATH for this run.",
    )
    parser.add_argument(
        "--aliases-db",
        type=Path,
        default=None,
        help="Override FINCH_ALIASES_DB_PATH for this run.",
    )
    args = parser.parse_args(argv)

    if args.status:
        return print_status(
            staples_db_path=args.staples_db,
            alias_db_path=args.aliases_db,
        )

    return run_setup(
        review_all=args.review_all,
        staples_db_path=args.staples_db,
        alias_db_path=args.aliases_db,
    )


if __name__ == "__main__":
    raise SystemExit(main())
