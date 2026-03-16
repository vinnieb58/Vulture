import re
from typing import Optional

from models.listing import Listing


# ---------------------------------------------------------------------------
# Structured-constraint helpers
# ---------------------------------------------------------------------------

# Mileage: "89k miles", "89,000 miles", "89k mi", "65K mi.", "120000 miles"
# Intentionally requires "miles" or "mi" so that bare "87K" (could be price)
# is never mistaken for mileage.  Conservative = no match → no rejection.
_MILES_TITLE_RE = re.compile(
    r'\b(\d[\d,]*)\s*([kK])?\s*(?:miles?|mi\.?)\b',
    re.IGNORECASE,
)

# RAM capacity: "16GB", "16 GB", "2x8GB", "2 x 8 GB", "4x4GB", "32gb kit"
# Group layout:  (1) N-part of NxM  (2) M-part of NxM  (3) standalone GB value
_RAM_CAP_RE = re.compile(
    r'\b(\d+)\s*[xX]\s*(\d+)\s*[gG][bB]\b'   # NxMGB  → groups 1, 2
    r'|\b(\d+)\s*[gG][bB]\b',                  # XGB    → group 3
    re.IGNORECASE,
)


def _extract_miles_from_title(title: str) -> Optional[int]:
    """
    Try to extract mileage from a listing title.
    Returns None if mileage cannot be confidently determined.

    Conservative: only fires when a number is immediately followed by
    'miles' or 'mi' (with optional k-suffix).  Phrases like "87K" or
    "low mileage" return None, leaving the listing to pass.
    """
    m = _MILES_TITLE_RE.search(title)
    if not m:
        return None
    raw = m.group(1).replace(",", "")
    value = int(raw)
    if m.group(2):          # k / K suffix
        value *= 1000
    return value


def _extract_ram_gb_from_title(title: str) -> Optional[int]:
    """
    Try to extract total RAM capacity in GB from a listing title.
    Returns None if capacity cannot be confidently determined.

    Handles:
      16GB  →  16
      16 GB →  16
      2x8GB → 16  (kit total)
      4x4GB → 16
      2 x 8 GB → 16
      32gb kit → 32
    """
    m = _RAM_CAP_RE.search(title)
    if not m:
        return None
    if m.group(1) is not None:          # NxM pattern
        return int(m.group(1)) * int(m.group(2))
    return int(m.group(3))              # standalone GB


# ---------------------------------------------------------------------------
# Public rule evaluator
# ---------------------------------------------------------------------------

def matches_rules(listing: Listing, rules: dict) -> bool:
    """Return True if the listing passes all rules defined in the hunt config."""
    if not rules:
        return True

    # --- price bounds ---
    min_price = rules.get("min_price")
    if min_price is not None:
        # Reject listings with no price or a suspiciously low placeholder price
        if listing.price is None or listing.price < min_price:
            return False

    max_price = rules.get("max_price")
    if max_price is not None:
        if listing.price is None or listing.price > max_price:
            return False

    # --- keyword filters ---
    include_keywords = rules.get("include_keywords") or []
    if include_keywords:
        title_lower = listing.title.lower()
        if not any(str(kw).lower() in title_lower for kw in include_keywords):
            return False

    exclude_keywords = rules.get("exclude_keywords") or []
    if exclude_keywords:
        title_lower = listing.title.lower()
        if any(str(kw).lower() in title_lower for kw in exclude_keywords):
            return False

    # --- structured constraints (extracted from title) ---

    # max_miles: only reject when mileage is explicitly stated AND over the limit.
    # If a title has no parseable mileage, the listing is allowed through.
    max_miles = rules.get("max_miles")
    if max_miles is not None:
        miles = _extract_miles_from_title(listing.title)
        if miles is not None and miles > max_miles:
            return False

    # min_capacity_gb: only reject when capacity is explicitly stated AND under
    # the threshold.  Ambiguous titles (no GB number found) are allowed through.
    min_capacity_gb = rules.get("min_capacity_gb")
    if min_capacity_gb is not None:
        capacity_gb = _extract_ram_gb_from_title(listing.title)
        if capacity_gb is not None and capacity_gb < min_capacity_gb:
            return False

    return True
