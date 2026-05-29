#!/usr/bin/env python3
"""Smoke test for the Cars.com adapter (experimental, browser-required)."""

from __future__ import annotations

from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from adapters.carsdotcom import search_carsdotcom
from adapters.registry import get_adapter, get_capabilities


def main() -> int:
    query, city, limit = "toyota camry", "77471", 5
    caps = get_capabilities("carsdotcom") or {}
    print(
        f"[INFO] carsdotcom: experimental={caps.get('experimental')}, "
        f"requires_browser={caps.get('requires_browser')}, "
        f"blocking_risk={caps.get('blocking_risk')}"
    )
    try:
        results = search_carsdotcom(query, city=city, limit=limit)
    except Exception as exc:
        print(f"[FAIL] adapter raised unexpectedly: {exc}")
        return 1
    print(f"[INFO] returned {len(results)} listing(s)")
    for idx, listing in enumerate(results[:3], 1):
        print(f"  {idx}. {listing.title!r} | ${listing.price} | {listing.location}")
    if not results:
        print("[WARN] zero listings — Cloudflare/HTTP2 block likely; adapter returned [] cleanly")
        return 0
    for idx, listing in enumerate(results, 1):
        if listing.source != "carsdotcom" or not listing.title or not listing.link:
            print(f"[FAIL] listing {idx} invalid: {listing!r}")
            return 1
    if get_adapter("carsdotcom") is None:
        print("[FAIL] registry lookup returned None")
        return 1
    print("[PASS] Cars.com smoke checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
