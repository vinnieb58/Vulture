"""Guided Finch setup — check env without printing secrets."""

from __future__ import annotations

import argparse

from finch.env_check import (
    CheckStatus,
    format_check_line,
    run_env_checks,
    search_ready,
    search_with_prices_ready,
)
from finch.env_util import load_env


def print_setup_report() -> int:
    load_env()
    checks = run_env_checks()
    print("Finch setup check")
    print("=" * 40)
    for check in checks:
        print(format_check_line(check))
    print()

    if search_with_prices_ready(checks):
        print("Ready for: preview (local), live product search with prices")
    elif search_ready(checks):
        print("Ready for: preview (local), live product search (no store selected — prices may be missing)")
        print("Run: python -m finch.locations <your_zip> --save")
    else:
        print("Ready for: preview (local) only")
        print("Missing: FINCH_KROGER_CLIENT_ID and/or FINCH_KROGER_CLIENT_SECRET in .env")

    redirect_ok = any(
        c.name == "FINCH_KROGER_REDIRECT_URI" and c.status == CheckStatus.OK for c in checks
    )
    if not redirect_ok:
        print("Note: FINCH_KROGER_REDIRECT_URI not set — fine for search; needed later for cart add.")

    live_cart = any(c.name == "FINCH_LIVE_CART" and c.status == CheckStatus.WARN for c in checks)
    if not live_cart:
        print("Cart add: disabled (good for now — build your alias map first).")

    print()
    print("Operator flow:")
    print("  1. Add FINCH_KROGER_CLIENT_ID/SECRET to .env")
    if search_ready(checks):
        if not search_with_prices_ready(checks):
            print("  2. python -m finch.locations <your_zip> --save --pick 1 --confirm")
        else:
            print("  2. python -m finch.locations <your_zip>  (already have a saved store)")
        print("  3. python -m finch.preview \"eggs, milk\"")
        print("  4. python -m finch.search \"eggs\" --save-alias eggs --pick 1 --confirm")
    else:
        print("  2. python -m finch.locations <your_zip> --save  (after credentials)")
        print("  3. python -m finch.preview \"eggs, milk\"")
    print("  (Later) OAuth + FINCH_LIVE_CART for cart add")

    missing_required = [c for c in checks if c.status == CheckStatus.MISSING]
    if missing_required and not search_ready(checks):
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Check Finch/Kroger environment configuration (no secrets printed).",
    )
    parser.parse_args(argv)
    return print_setup_report()


if __name__ == "__main__":
    raise SystemExit(main())
