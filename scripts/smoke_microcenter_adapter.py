#!/usr/bin/env python3
"""Smoke test for the Micro Center adapter (experimental, Playwright-required)."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from adapters.microcenter import search_microcenter
from adapters.registry import get_adapter, get_capabilities


def main() -> int:
    parser = argparse.ArgumentParser(description="Micro Center adapter smoke test")
    parser.add_argument("--query", default="ryzen 7 7800x3d", help="Search query")
    parser.add_argument("--storeid", default="141", help="Micro Center store ID")
    parser.add_argument("--limit", type=int, default=5, help="Max listings")
    args = parser.parse_args()

    caps = get_capabilities("microcenter") or {}
    print(
        f"[INFO] microcenter: experimental={caps.get('experimental')}, "
        f"requires_browser={caps.get('requires_browser')}, "
        f"location_control={caps.get('location_control')}"
    )

    if get_adapter("microcenter") is None:
        print("[FAIL] registry lookup returned None")
        return 1

    try:
        results = search_microcenter(
            args.query,
            limit=args.limit,
            storeid=args.storeid,
        )
    except Exception as exc:
        print(f"[FAIL] adapter raised unexpectedly: {exc}")
        return 1

    print(f"[INFO] returned {len(results)} listing(s) for storeid={args.storeid!r}")
    for idx, listing in enumerate(results[:5], 1):
        print(
            f"  [{idx}] {listing.title!r}\n"
            f"       price=${listing.price} location={listing.location!r}\n"
            f"       link={listing.link}"
        )

    if not results:
        print(
            "[WARN] zero listings — Cloudflare block or no in-stock cards; "
            "adapter returned [] cleanly"
        )
        return 0

    for idx, listing in enumerate(results, 1):
        if listing.source != "microcenter" or not listing.title or not listing.link:
            print(f"[FAIL] listing {idx} invalid: {listing!r}")
            return 1
        if not listing.link.startswith("https://www.microcenter.com/product/"):
            print(f"[FAIL] listing {idx} bad link: {listing.link}")
            return 1

    print("[PASS] Micro Center smoke checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
