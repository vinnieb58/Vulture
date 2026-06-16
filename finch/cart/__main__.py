"""Finch cart CLI — guarded add, list add, history, and smoke test."""

from __future__ import annotations

import argparse
import sys

from finch.cart_ops import (
    CartGuardError,
    CartResolveError,
    check_trip_duplicate,
    ensure_fresh_user_token,
    execute_cart_add,
    format_attempt_result,
    live_cart_enabled,
    parse_add_item,
    pick_test_alias,
    record_cart_activity,
    record_successful_trip_add,
    require_live_cart,
    require_saved_token,
    resolve_cart_item,
    resolve_cart_list,
)
from finch.trip_ledger import (
    format_added_list,
    get_or_create_open_trip,
    list_added_today,
    list_trip_items,
    reset_trip,
    undo_last_trip_item,
)
from finch.env_util import load_env
from finch.kroger_client import KrogerAuthError, KrogerError, load_kroger_client_from_env
from finch.token_store import resolve_user_access_token


def _run_single_add(
    attempt,
    client,
    *,
    action: str = "cart_add",
    activity_db_path=None,
    trip_ledger_db_path=None,
    source: str | None = None,
    force: bool = False,
) -> tuple[int, str]:
    duplicate_msg = check_trip_duplicate(
        attempt,
        force=force,
        trip_ledger_db_path=trip_ledger_db_path,
    )
    if duplicate_msg:
        record_cart_activity(
            attempt,
            action=action,
            result="skipped — duplicate this trip",
            activity_db_path=activity_db_path,
        )
        return 0, duplicate_msg

    try:
        result = execute_cart_add(
            attempt,
            client,
            action=action,
            activity_db_path=activity_db_path,
        )
    except (CartGuardError, KrogerAuthError, KrogerError) as exc:
        return 1, format_attempt_result(attempt, result=f"failed — {exc}")

    record_successful_trip_add(
        attempt,
        source=source or "cli",
        trip_ledger_db_path=trip_ledger_db_path,
    )
    status = result.get("status", "ok") if isinstance(result, dict) else "ok"
    return 0, format_attempt_result(attempt, result=f"ok ({status})")


def cmd_add(
    item: str,
    *,
    quantity: int | None = None,
    force: bool = False,
    source: str | None = None,
    trip_ledger_db_path=None,
) -> int:
    require_live_cart()
    require_saved_token()

    item_text, parsed_force = parse_add_item(item)
    force = force or parsed_force

    try:
        attempt = resolve_cart_item(item_text, quantity=quantity)
    except CartResolveError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    client = load_kroger_client_from_env()
    try:
        ensure_fresh_user_token(client)
    except (KrogerAuthError, KrogerError) as exc:
        print(format_attempt_result(attempt, result=f"failed — {exc}"))
        return 1

    rc, output = _run_single_add(
        attempt,
        client,
        source=source or "cli",
        trip_ledger_db_path=trip_ledger_db_path,
        force=force,
    )
    print(output)
    if rc == 0 and "already added" not in output:
        print()
        print("Review and checkout manually in the Kroger app.")
    return rc


def cmd_add_list(list_text: str) -> int:
    require_live_cart()
    require_saved_token()

    try:
        parsed = resolve_cart_list(list_text)
    except CartResolveError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    for item_text, err in parsed.failed:
        print(f"Skip {item_text!r}: {err}", file=sys.stderr)
        record_cart_activity_from_text(item_text, err)

    if not parsed.succeeded:
        print("Error: no items could be resolved for cart add.", file=sys.stderr)
        return 1

    client = load_kroger_client_from_env()
    try:
        ensure_fresh_user_token(client)
    except (KrogerAuthError, KrogerError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    rc = 0
    for attempt in parsed.succeeded:
        item_rc, output = _run_single_add(
            attempt,
            client,
            action="cart_add_list",
            source="cli",
        )
        print(output)
        print()
        if item_rc != 0:
            rc = 1

    if rc == 0:
        print("Review and checkout manually in the Kroger app.")
    return rc


def record_cart_activity_from_text(item_text: str, err: str) -> None:
    from finch.activity import log_activity

    log_activity(
        requested_text=item_text,
        resolved_alias=None,
        upc=None,
        product_id=None,
        quantity=0,
        action="cart_add_list",
        result=f"skipped — {err}",
    )


def cmd_history(
    *,
    limit: int = 50,
    scope: str = "trip",
    trip_ledger_db_path=None,
) -> int:
    if scope == "today":
        items = list_added_today(db_path=trip_ledger_db_path)[:limit]
        print(format_added_list(items, title="Finch added list (today)"))
        return 0

    trip_id = get_or_create_open_trip(db_path=trip_ledger_db_path)
    items = list_trip_items(trip_id, db_path=trip_ledger_db_path)[:limit]
    print(format_added_list(items, trip_id=trip_id))
    return 0


def cmd_reset_trip(*, trip_ledger_db_path=None) -> int:
    trip_id = reset_trip(db_path=trip_ledger_db_path)
    print(f"Started new Finch grocery trip (trip {trip_id}).")
    print("Duplicate guard cleared for this trip.")
    return 0


def cmd_undo_last(*, trip_ledger_db_path=None) -> int:
    trip_id = get_or_create_open_trip(db_path=trip_ledger_db_path)
    item = undo_last_trip_item(trip_id, db_path=trip_ledger_db_path)
    if not item:
        print("Nothing to undo on the Finch added list for this trip.")
        return 0
    label = item.display_name or item.normalized_name
    print(
        f"Removed {label!r} from the Finch added list (local only). "
        "Your Kroger cart was not changed — review it in the Kroger app."
    )
    return 0


def cmd_test() -> int:
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
        record_cart_activity(
            attempt,
            action="cart_test",
            result="validation ok — FINCH_LIVE_CART off",
        )
        return 0

    if not resolve_user_access_token():
        print()
        print("  result: validation ok — no saved user token")
        print("  next: python -m finch.auth")
        record_cart_activity(
            attempt,
            action="cart_test",
            result="validation ok — no token",
        )
        return 0

    client = load_kroger_client_from_env()
    try:
        ensure_fresh_user_token(client)
        rc, output = _run_single_add(attempt, client, action="cart_test")
    except (CartGuardError, KrogerAuthError, KrogerError) as exc:
        print()
        print(f"  result: failed — {exc}")
        record_cart_activity(attempt, action="cart_test", result=f"failed — {exc}")
        return 1

    print()
    print(output.split("result:", 1)[-1].strip())
    if rc == 0:
        print("Review and checkout manually in the Kroger app.")
    return rc


def main(argv: list[str] | None = None) -> int:
    load_env()
    parser = argparse.ArgumentParser(
        description="Add alias-resolved items to Kroger cart (no checkout).",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    add_parser = sub.add_parser("add", help="Add one item by alias name")
    add_parser.add_argument("item", help='Grocery term, e.g. "2 eggs" or "coffee pods"')
    add_parser.add_argument(
        "--force",
        action="store_true",
        help="Force add even if this item was already added this trip",
    )

    add_parser.add_argument(
        "--quantity",
        type=int,
        default=None,
        help="Override parsed quantity (default: use quantity from text, else 1)",
    )

    add_list_parser = sub.add_parser("add-list", help="Add multiple comma-separated items")
    add_list_parser.add_argument(
        "list_text",
        help='Grocery list, e.g. "eggs, milk, coffee pods"',
    )

    history_parser = sub.add_parser("history", help="Show Finch added list for this trip")
    history_parser.add_argument("--limit", type=int, default=50, help="Max entries (default 50)")
    history_parser.add_argument(
        "--scope",
        choices=("trip", "today"),
        default="trip",
        help="Show current trip (default) or everything added today",
    )

    sub.add_parser("reset-trip", help="Start a new Finch grocery trip (clears duplicate guard)")
    sub.add_parser("undo-last", help="Undo last Finch-added item locally (no Kroger cart remove)")

    sub.add_parser("test", help="Smoke test — add eggs (or first pinned alias)")

    args = parser.parse_args(argv)
    if args.command == "add":
        return cmd_add(args.item, quantity=args.quantity, force=args.force)
    if args.command == "add-list":
        return cmd_add_list(args.list_text)
    if args.command == "history":
        return cmd_history(limit=args.limit, scope=args.scope)
    if args.command == "reset-trip":
        return cmd_reset_trip()
    if args.command == "undo-last":
        return cmd_undo_last()
    if args.command == "test":
        return cmd_test()
    parser.error("unknown command")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
