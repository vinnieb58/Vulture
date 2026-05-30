#!/usr/bin/env python3
"""Smoke test for the Swappa adapter (experimental, no location targeting)."""

from __future__ import annotations

from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from adapters.registry import get_adapter, get_capabilities
from adapters.swappa import search_swappa

_QUERIES = ("macbook air", "steam deck", "rtx 3080")


def _check_listing(listing, query: str, idx: int) -> str | None:
    if listing.source != "swappa":
        return f"[{query}] listing {idx}: source={listing.source!r}"
    if not listing.title or not listing.link:
        return f"[{query}] listing {idx}: missing title or link"
    if not listing.link.startswith("https://swappa.com/listing/view/"):
        return f"[{query}] listing {idx}: non-canonical link {listing.link!r}"
    return None


def main() -> int:
    caps = get_capabilities("swappa") or {}
    print(
        f"[INFO] swappa: experimental={caps.get('experimental')}, "
        f"stable={caps.get('stable')}, location_control={caps.get('location_control')}"
    )

    if get_adapter("swappa") is None:
        print("[FAIL] registry lookup returned None for swappa")
        return 1

    failures = 0
    any_results = False

    for query in _QUERIES:
        print(f"\n--- query: {query!r} ---")
        try:
            results = search_swappa(query, city=None, limit=10)
        except Exception as exc:
            print(f"[FAIL] adapter raised for {query!r}: {exc}")
            failures += 1
            continue

        print(f"[INFO] returned {len(results)} listing(s)")
        for idx, listing in enumerate(results[:3], 1):
            print(
                f"  {idx}. {listing.title!r} | ${listing.price} | "
                f"{listing.location} | {listing.link}"
            )

        for idx, listing in enumerate(results, 1):
            err = _check_listing(listing, query, idx)
            if err:
                print(f"[FAIL] {err}")
                failures += 1

        if results:
            any_results = True
        else:
            print(f"[WARN] zero listings for {query!r} (no match or transient block)")

    if failures:
        print(f"\n[FAIL] {failures} smoke check(s) failed")
        return 1

    if any_results:
        print("\n[PASS] Swappa smoke checks passed (at least one query returned listings)")
    else:
        print(
            "\n[WARN] all queries returned zero listings — adapter code OK but live fetch empty"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
