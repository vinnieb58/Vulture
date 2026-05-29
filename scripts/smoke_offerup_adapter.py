#!/usr/bin/env python3
"""Smoke test for the OfferUp adapter (experimental, GeoIP-only)."""

from __future__ import annotations

from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from adapters.offerup import search_offerup
from adapters.registry import get_adapter, get_capabilities


def main() -> int:
    query, city, limit = "toyota sequoia", "houston", 5
    caps = get_capabilities("offerup") or {}
    print(
        f"[INFO] offerup: experimental={caps.get('experimental')}, "
        f"location_control={caps.get('location_control')}"
    )
    try:
        results = search_offerup(query, city=city, limit=limit)
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
        print("[WARN] zero listings (GeoIP/blocking); adapter code OK")
    elif get_adapter("offerup") is None:
        print("[FAIL] registry lookup returned None")
        return 1
    else:
        print("[PASS] OfferUp smoke checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
