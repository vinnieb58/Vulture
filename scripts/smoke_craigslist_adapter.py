#!/usr/bin/env python3
"""Smoke test for the Craigslist adapter (stable)."""

from __future__ import annotations

from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from adapters.craigslist import search_craigslist
from adapters.registry import get_adapter, get_capabilities


def main() -> int:
    query, city, limit = "rtx 3080", "houston", 5
    caps = get_capabilities("craigslist") or {}
    print(f"[INFO] craigslist: status={caps.get('status')}, stable={caps.get('stable')}")
    try:
        results = search_craigslist(query, city=city, limit=limit)
    except Exception as exc:
        print(f"[FAIL] adapter raised: {exc}")
        return 1
    print(f"[INFO] returned {len(results)} listing(s)")
    for idx, listing in enumerate(results[:3], 1):
        print(f"  {idx}. {listing.title!r} | ${listing.price} | {listing.location}")
    for idx, listing in enumerate(results, 1):
        if not listing.source or not listing.title or not listing.link:
            print(f"[FAIL] listing {idx} missing required fields")
            return 1
    if not results:
        print("[WARN] zero listings; adapter code OK")
    elif get_adapter("craigslist") is None:
        print("[FAIL] registry lookup returned None")
        return 1
    else:
        print("[PASS] Craigslist smoke checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
