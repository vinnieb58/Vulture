"""Live Kroger product search and optional alias pinning."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from finch.aliases import ensure_seeded, get_alias, upsert_alias
from finch.env_check import search_ready
from finch.env_util import load_env
from finch.kroger_client import (
    KrogerError,
    KrogerProduct,
    ProductSearchResult,
    load_kroger_client_from_env,
)
from finch.models import AliasEntry
from finch.preference_norm import normalize_preference_key


def format_product_line(index: int, product: KrogerProduct) -> str:
    price = product.format_price()
    parts = [
        f"  [{index}] {product.description}",
        f"      brand: {product.brand or '—'}",
        f"size: {product.size or '—'}",
        f"UPC: {product.upc or '—'}",
        f"product_id: {product.product_id}",
    ]
    if price:
        parts.append(f"price: {price}")
    else:
        parts.append("price: —")
    return "\n".join([parts[0], "      " + " | ".join(parts[1:])])


def format_search_header(term: str, location_id: str | None, count: int) -> str:
    loc = location_id or "(no location — prices may be missing)"
    return f'Search: "{term}" at location {loc}\nFound {count} result(s):\n'


def product_to_alias(
    alias_key: str,
    product: KrogerProduct,
    *,
    search_term: str,
) -> AliasEntry:
    return AliasEntry(
        alias_key=normalize_preference_key(alias_key),
        display_name=product.description,
        kroger_product_id=product.product_id,
        upc=product.upc,
        search_term=search_term,
        notes="Pinned via finch.search",
    )


def confirm_save(
    alias_key: str,
    product: KrogerProduct,
    *,
    confirm: bool = False,
    input_fn=input,
) -> bool:
    if confirm:
        return True
    existing = get_alias(alias_key)
    prompt = f"Save alias {alias_key!r} → {product.description!r}?"
    if existing:
        prompt = (
            f"Replace alias {alias_key!r} ({existing.display_name!r}) "
            f"with {product.description!r}?"
        )
    answer = input_fn(f"{prompt} [y/N] ").strip().lower()
    return answer in ("y", "yes")


def save_alias_from_product(
    alias_key: str,
    product: KrogerProduct,
    search_term: str,
    *,
    db_path: Path | None = None,
    confirm: bool = False,
    input_fn=input,
) -> AliasEntry | None:
    if not confirm_save(alias_key, product, confirm=confirm, input_fn=input_fn):
        print("Alias not saved.")
        return None
    entry = product_to_alias(alias_key, product, search_term=search_term)
    upsert_alias(entry, db_path)
    print(f"Saved alias {entry.alias_key!r} → {entry.display_name!r}")
    if entry.upc:
        print(f"  UPC: {entry.upc}")
    return entry


def run_search(
    term: str,
    *,
    limit: int = 10,
    start: int = 0,
    client=None,
) -> ProductSearchResult:
    if client is None:
        if not search_ready():
            raise RuntimeError(
                "Kroger search not configured. Run: python -m finch.setup\n"
                "Set FINCH_KROGER_CLIENT_ID and FINCH_KROGER_CLIENT_SECRET in .env"
            )
        client = load_kroger_client_from_env()
    return client.search_products(term, limit=limit, start=start)


def print_search_results(
    term: str,
    products: list[KrogerProduct],
    *,
    location_id: str | None = None,
    as_json: bool = False,
) -> None:
    if as_json:
        payload = [
            {
                "description": p.description,
                "brand": p.brand,
                "size": p.size,
                "upc": p.upc,
                "product_id": p.product_id,
                "price": p.format_price(),
            }
            for p in products
        ]
        print(json.dumps({"term": term, "results": payload}, indent=2))
        return

    if not products:
        print(f'Search: "{term}" — no results.')
        return

    print(format_search_header(term, location_id, len(products)))
    for i, product in enumerate(products, start=1):
        print(format_product_line(i, product))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Search Kroger products (client credentials — no cart access).",
    )
    parser.add_argument("term", help='Search term, e.g. "eggs" or "coffee pods"')
    parser.add_argument("--limit", type=int, default=10, help="Max results (default 10)")
    parser.add_argument("--json", action="store_true", help="JSON output")
    parser.add_argument(
        "--save-alias",
        metavar="KEY",
        help='Save a result as alias, e.g. --save-alias eggs',
    )
    parser.add_argument(
        "--pick",
        type=int,
        metavar="N",
        help="Result number to save (1-based). Prompts if omitted with --save-alias.",
    )
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="Skip confirmation prompt when saving alias",
    )
    args = parser.parse_args(argv)

    load_env()
    ensure_seeded()

    try:
        client = load_kroger_client_from_env() if search_ready() else None
        search_result = run_search(args.term, limit=args.limit, client=client)
    except (RuntimeError, KrogerError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    location_id = client.oauth.location_id if client else None
    print_search_results(
        args.term,
        search_result.products,
        location_id=location_id,
        as_json=args.json,
    )

    if not args.save_alias:
        return 0

    products = search_result.products
    if not products:
        print("No results to save.", file=sys.stderr)
        return 1

    pick = args.pick
    if pick is None:
        if args.confirm or not sys.stdin.isatty():
            print("Error: --pick N is required when stdin is not interactive.", file=sys.stderr)
            return 1
        raw = input(f"Pick result to save as {args.save_alias!r} [1-{len(products)}]: ").strip()
        try:
            pick = int(raw)
        except ValueError:
            print("Invalid pick.", file=sys.stderr)
            return 1

    if pick < 1 or pick > len(products):
        print(f"Pick must be between 1 and {len(products)}.", file=sys.stderr)
        return 1

    product = products[pick - 1]
    saved = save_alias_from_product(
        args.save_alias,
        product,
        args.term,
        confirm=args.confirm,
    )
    return 0 if saved else 1


if __name__ == "__main__":
    raise SystemExit(main())
