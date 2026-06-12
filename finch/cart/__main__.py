"""Finch cart CLI — guarded add and smoke test."""

from __future__ import annotations

import argparse
import sys

from finch.cart_ops import (
    CartGuardError,
    CartResolveError,
    ensure_fresh_user_token,
    execute_cart_add,
    format_attempt_result,
    live_cart_enabled,
    pick_test_alias,
    require_live_cart,
    require_saved_token,
    resolve_cart_item,
)
from finch.env_util import load_env
from finch.kroger_client import KrogerAuthError, KrogerError, load_kroger_client_from_env
from finch.token_store import resolve_user_access_token


def cmd_add(item: str, *, quantity: int = 1) -> int:
    require_live_cart()
    require_saved_token()

    try:
        attempt = resolve_cart_item(item, quantity=quantity)
    except CartResolveError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    client = load_kroger_client_from_env()
    try:
        ensure_fresh_user_token(client)
        result = execute_cart_add(attempt, client)
    except (CartGuardError, KrogerAuthError, KrogerError) as exc:
        print(format_attempt_result(attempt, result=f"failed — {exc}"))
        return 1

    status = result.get("status", "ok") if isinstance(result, dict) else "ok"
    print(format_attempt_result(attempt, result=f"ok ({status})"))
    print()
    print("Review and checkout manually in the Kroger app.")
    return 0


def cmd_test() -> int:
    load_env()
    test_item = pick_test_alias()
    if not test_item:
        print("Cart test: validation only")
        print("  no alias with UPC found — pin a product first:")
        print('  python -m finch.search "eggs" --save-alias eggs --pick 1 --confirm')
        return 1

    try:
        attempt = resolve_cart_item(test_item, quantity=1)
    except CartResolveError as exc:
        print(f"Cart test failed: {exc}", file=sys.stderr)
        return 1

    print("Cart test")
    print("=" * 40)
    for line in attempt.summary_lines():
        print(line)

    if not live_cart_enabled():
        print()
        print("  result: validation ok — FINCH_LIVE_CART is off (no cart mutation)")
        print("  enable: set FINCH_LIVE_CART=true in .env, then re-run")
        return 0

    if not resolve_user_access_token_check():
        print()
        print("  result: validation ok — no saved user token")
        print("  next: python -m finch.auth")
        return 0

    client = load_kroger_client_from_env()
    try:
        ensure_fresh_user_token(client)
        result = execute_cart_add(attempt, client)
    except (CartGuardError, KrogerAuthError, KrogerError) as exc:
        print()
        print(f"  result: failed — {exc}")
        return 1

    status = result.get("status", "ok") if isinstance(result, dict) else "ok"
    print()
    print(f"  result: ok ({status}) — added test item {test_item!r}")
    print("Review and checkout manually in the Kroger app.")
    return 0


def resolve_user_access_token_check() -> bool:
    return bool(resolve_user_access_token())


def main(argv: list[str] | None = None) -> int:
    load_env()
    parser = argparse.ArgumentParser(
        description="Add alias-resolved items to Kroger cart (no checkout).",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    add_parser = sub.add_parser("add", help="Add one item by alias name")
    add_parser.add_argument("item", help='Grocery term, e.g. eggs or "coffee pods"')
    add_parser.add_argument("--quantity", type=int, default=1, help="Quantity (default 1)")

    sub.add_parser("test", help="Smoke test — add eggs (or first pinned alias)")

    args = parser.parse_args(argv)
    if args.command == "add":
        return cmd_add(args.item, quantity=args.quantity)
    if args.command == "test":
        return cmd_test()
    parser.error("unknown command")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
