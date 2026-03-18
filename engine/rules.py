import re
from typing import Optional

from models.listing import Listing


# ---------------------------------------------------------------------------
# Structured-constraint helpers
# ---------------------------------------------------------------------------

# Mileage: "89k miles", "89,000 miles", "89k mi", "65K mi.", "120000 miles"
# Requires "miles" or "mi" word — bare "87K" is intentionally ignored (could be price).
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

# Vehicle model year: 4-digit number in range 1960–2030.
# For title parsing, the first matching year is used (almost always the model year).
_YEAR_TITLE_RE = re.compile(r'\b((?:19|20)\d{2})\b')
_YEAR_TITLE_MIN = 1960
_YEAR_TITLE_MAX = 2030

# RAM speed from title: "3200MHz", "6000 MHz", "4800MT/s"
# Takes the highest value in the title — RAM listings lead with their rated speed.
_SPEED_TITLE_RE = re.compile(r'\b(\d{3,5})\s*(?:mhz|mt/s)\b', re.IGNORECASE)

# GPU VRAM from title.
# Pattern A — explicit "vram" / "video memory" keyword:
#   "8GB VRAM",  "24GB video memory"
_VRAM_TITLE_EXPLICIT_RE = re.compile(
    r'(\d+)\s*[gG][bB]\s+(?:vram|video\s+(?:memory|ram))',
    re.IGNORECASE,
)
# Pattern B — GB number immediately after a GPU model number, e.g.:
#   "RTX 3080 10GB",  "GTX 1080 Ti 11GB",  "RX 6800 XT 16GB"
# Optional tier suffix (Ti / Super / XT / XTX) before the GB.
_VRAM_TITLE_MODEL_RE = re.compile(
    r'\b(?:rtx|gtx)\s*\d{3,4}(?:\s+(?:ti|super))?\s+(\d+)\s*[gG][bB]\b'
    r'|\b(?:rx)\s*\d{3,4}(?:\s+(?:xt|xtx))?\s+(\d+)\s*[gG][bB]\b',
    re.IGNORECASE,
)


def _extract_miles_from_title(title: str) -> Optional[int]:
    """
    Try to extract mileage from a listing title.
    Returns None if mileage cannot be confidently determined.

    Conservative: only fires when a number is immediately followed by
    'miles' or 'mi'.  Phrases like "87K" or "low mileage" return None.
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
      32gb kit → 32
    """
    m = _RAM_CAP_RE.search(title)
    if not m:
        return None
    if m.group(1) is not None:          # NxM pattern
        return int(m.group(1)) * int(m.group(2))
    return int(m.group(3))              # standalone GB


def _extract_year_from_title(title: str) -> Optional[int]:
    """
    Extract the vehicle model year from a listing title.

    Returns the first 4-digit number in range 1960–2030, or None if not found.
    Conservative: only the first match is used since CL titles almost always
    lead with the year (e.g. "2019 Toyota RAV4 85k miles").
    """
    for m in _YEAR_TITLE_RE.finditer(title):
        yr = int(m.group(1))
        if _YEAR_TITLE_MIN <= yr <= _YEAR_TITLE_MAX:
            return yr
    return None


def _extract_speed_mhz_from_title(title: str) -> Optional[int]:
    """
    Try to extract the RAM speed in MHz / MT/s from a listing title.
    Returns the highest value found, or None if no speed is stated.

    Conservative: bare numbers without MHz/MT/s are ignored.
    Taking the highest value gives the rated speed when multiple figures appear.

    Examples:
      "Kingston 16GB DDR4 3200MHz"  → 3200
      "Corsair 32GB DDR5 6000 MHz"  → 6000
      "4800MT/s DDR5 16GB"          → 4800
    """
    values = [int(m.group(1)) for m in _SPEED_TITLE_RE.finditer(title)]
    return max(values) if values else None


def _extract_vram_gb_from_title(title: str) -> Optional[int]:
    """
    Try to extract GPU VRAM (GB) from a listing title.
    Returns None if VRAM cannot be confidently determined.

    Two strategies, most-specific first:
      A. Explicit 'Xgb VRAM' or 'Xgb video memory' keyword.
      B. GB number immediately after a recognised GPU model string,
         e.g. 'RTX 3080 10GB' or 'RX 6800 XT 16GB'.

    Conservative: bare 'Xgb' alone is NOT treated as VRAM (could be system RAM
    in a whole-PC listing).
    """
    # Prefer explicit VRAM keyword
    m = _VRAM_TITLE_EXPLICIT_RE.search(title)
    if m:
        return int(m.group(1))

    # GB right after GPU model number
    m = _VRAM_TITLE_MODEL_RE.search(title)
    if m:
        raw = m.group(1) or m.group(2)
        if raw:
            return int(raw)

    return None


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

    # include_keywords  — OR / any() semantics (backward-compatible).
    # At least one keyword in the list must appear in the title.
    # Used by YAML hunts and simple single-discriminator translator hunts.
    include_keywords = rules.get("include_keywords") or []
    if include_keywords:
        title_lower = listing.title.lower()
        if not any(str(kw).lower() in title_lower for kw in include_keywords):
            return False

    # require_all_keywords — AND / all() semantics (strict, opt-in).
    # Every keyword in the list must appear in the title.
    # Set by the translator via adapter_options for hunts that need multiple
    # co-occurring discriminators, e.g. TV brand + size + panel type.
    # Never set by v1.0 YAML hunts; does not affect existing hunts.
    require_all_keywords = rules.get("require_all_keywords") or []
    if require_all_keywords:
        title_lower = listing.title.lower()
        if not all(str(kw).lower() in title_lower for kw in require_all_keywords):
            return False

    exclude_keywords = rules.get("exclude_keywords") or []
    if exclude_keywords:
        title_lower = listing.title.lower()
        if any(str(kw).lower() in title_lower for kw in exclude_keywords):
            return False

    # --- structured constraints extracted from title ---
    # For all of these: if the value cannot be parsed from the title, the
    # listing is allowed through (conservative / no-match = pass).

    # max_miles: reject only when mileage is explicitly stated AND over the limit.
    max_miles = rules.get("max_miles")
    if max_miles is not None:
        miles = _extract_miles_from_title(listing.title)
        if miles is not None and miles > max_miles:
            return False

    # min_capacity_gb: reject only when capacity is stated AND under the threshold.
    min_capacity_gb = rules.get("min_capacity_gb")
    if min_capacity_gb is not None:
        capacity_gb = _extract_ram_gb_from_title(listing.title)
        if capacity_gb is not None and capacity_gb < min_capacity_gb:
            return False

    # min_year / max_year: reject only when a year is stated AND out of range.
    min_year = rules.get("min_year")
    max_year = rules.get("max_year")
    if min_year is not None or max_year is not None:
        year = _extract_year_from_title(listing.title)
        if year is not None:
            if min_year is not None and year < min_year:
                return False
            if max_year is not None and year > max_year:
                return False

    # min_vram_gb: reject only when VRAM is explicitly stated AND under the threshold.
    min_vram_gb = rules.get("min_vram_gb")
    if min_vram_gb is not None:
        vram_gb = _extract_vram_gb_from_title(listing.title)
        if vram_gb is not None and vram_gb < min_vram_gb:
            return False

    # min_speed_mhz: reject only when speed is explicitly stated AND below the threshold.
    min_speed_mhz = rules.get("min_speed_mhz")
    if min_speed_mhz is not None:
        speed_mhz = _extract_speed_mhz_from_title(listing.title)
        if speed_mhz is not None and speed_mhz < min_speed_mhz:
            return False

    return True
