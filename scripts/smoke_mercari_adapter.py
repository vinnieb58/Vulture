#!/usr/bin/env python3
from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from adapters.mercari import search_mercari
from adapters.registry import get_adapter
from engine.rules import rejection_reason


def _validate_listings_or_exit(listings, context: str) -> None:
    if not listings:
        print(f"[FAIL] {context}: zero listings returned")
        raise SystemExit(1)
    for idx, listing in enumerate(listings, 1):
        if not listing.title or not listing.link:
            print(f"[FAIL] {context}: listing {idx} missing title/link: {listing!r}")
            raise SystemExit(1)


def main() -> int:
    query = "rtx 3080"
    limit = 5

    print(f"[INFO] Running direct adapter search_mercari({query!r}, limit={limit})")
    direct_results = search_mercari(query, limit=limit)
    _validate_listings_or_exit(direct_results, "direct adapter")
    print(f"[INFO] Direct adapter returned {len(direct_results)} listings")
    for listing in direct_results:
        print(" -", listing)

    print("[INFO] Running registry adapter get_adapter('mercari')")
    try:
        registry_adapter = get_adapter("mercari")
    except Exception as exc:  # pragma: no cover - smoke safety
        print(f"[FAIL] registry lookup failed: {exc}")
        return 1
    if registry_adapter is None:
        print("[FAIL] registry lookup returned None")
        return 1

    registry_results = registry_adapter(query, limit=limit)
    _validate_listings_or_exit(registry_results, "registry adapter")
    print(f"[INFO] Registry adapter returned {len(registry_results)} listings")
    for listing in registry_results:
        reason = rejection_reason(listing, {"max_price": 500})
        print(f" - {listing.title} -> {reason}")

    # Explicitly touch price handling to surface normalization/runtime issues.
    for listing in registry_results:
        _ = listing.price is None or isinstance(listing.price, int)

    print("[PASS] Mercari smoke checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
