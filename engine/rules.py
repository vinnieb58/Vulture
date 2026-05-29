import logging
import re
from typing import Optional

from models.listing import Listing

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Vehicle model-only matching and subtype title filters
# ---------------------------------------------------------------------------

_DISTINCTIVE_VEHICLE_MODELS: dict[str, bool] = {
    "sequoia": False, "4runner": False, "telluride": False, "palisade": False,
    "prius": False, "highlander": False, "tacoma": False, "tundra": False,
    "camry": True,
}

_GPU_WHOLE_SYSTEM_PATTERNS = (
    "laptop", "gaming pc", "gaming desktop", "desktop build",
    "alienware m15", "alienware r13", "full pc", "tower",
)

_RAM_WHOLE_COMPUTER_PATTERNS = (
    "mini pc", "optiplex", "elitedesk", "aio", "all-in-one",
    "pavilion desktop", "gaming computer", "prebuilt pc",
)

_RAM_DESKTOP_KIT_HINTS = (
    "ddr", "dimm", "288-pin", "288 pin", " sodimm", "so-dimm",
    "ripjaws", "vengeance", " kit", "x8gb", "x16gb", " pc ram",
    " desktop ram", " memory module",
)


def _word_in_title(title_lower: str, word: str) -> bool:
    return bool(re.search(r"\b" + re.escape(word.lower()) + r"\b", title_lower))


def _vehicle_models_in_title(title_lower: str) -> list[str]:
    return [m for m in _DISTINCTIVE_VEHICLE_MODELS if _word_in_title(title_lower, m)]


def _vehicle_include_matches(title_lower: str, make: str, model: str, include_keywords: list) -> bool:
    if len(_vehicle_models_in_title(title_lower)) > 1:
        return False
    make_l, model_l = make.lower(), model.lower()
    for kw in include_keywords:
        if str(kw).lower() in title_lower:
            return True
    has_make, has_model = _word_in_title(title_lower, make_l), _word_in_title(title_lower, model_l)
    if has_make and has_model:
        return True
    requires_make = _DISTINCTIVE_VEHICLE_MODELS.get(model_l)
    if requires_make is None:
        return False
    if requires_make:
        return has_make and has_model
    models_present = _vehicle_models_in_title(title_lower)
    return has_model and (not models_present or models_present == [model_l])


def _vehicle_include_rejection(title_lower: str, make: str, model: str, include_keywords: list, vpfx: str) -> Optional[str]:
    models_present = _vehicle_models_in_title(title_lower)
    if len(models_present) > 1:
        return f"{vpfx}ambiguous multi-model vehicle title ({', '.join(models_present)})"
    if _vehicle_include_matches(title_lower, make, model, include_keywords):
        return None
    shown = ", ".join(f'"{kw}"' for kw in include_keywords[:4])
    if len(include_keywords) > 4:
        shown += f" +{len(include_keywords) - 4} more"
    return f"{vpfx}title missing include keyword (any of: {shown})"


def _looks_like_gpu_whole_system(title_lower: str) -> bool:
    return any(p in title_lower for p in _GPU_WHOLE_SYSTEM_PATTERNS)


def _looks_like_ram_whole_computer(title_lower: str) -> bool:
    if any(p in title_lower for p in _RAM_WHOLE_COMPUTER_PATTERNS):
        return True
    if "desktop" in title_lower:
        if any(h in title_lower for h in _RAM_DESKTOP_KIT_HINTS):
            return False
        if re.search(r"\bdesktop\s+(?:computer|pc|system|bundle)\b", title_lower):
            return True
        if re.search(r"\b(?:hp|dell|lenovo|acer)\s+desktop\b", title_lower):
            return True
    return False


# ---------------------------------------------------------------------------
# Structured-constraint helpers
# ---------------------------------------------------------------------------

# TV screen size: "75 inch", "75-inch", '75"', "75in" (no-space variant).
# Two alternatives:
#   A — digit(s) followed by "inch" or literal " (with optional dash/space).
#   B — digits immediately adjacent to "in" (no space), e.g. "55in".
#       Requires word boundary on both sides to avoid "65 in good condition".
# Sanity check: only accept 20–120 inches (realistic TV range).
_TV_SIZE_TITLE_RE = re.compile(
    r'\b(\d{2,3})\s*[-]?\s*(?:inch(?:es?)?|")'  # "75 inch", "75-inch", '75"'
    r'|\b(\d{2,3})in\b',                          # "75in" (no space before "in")
    re.IGNORECASE,
)
_TV_SIZE_MIN = 20
_TV_SIZE_MAX = 120

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


def _extract_tv_size_from_title(title: str) -> Optional[int]:
    """
    Try to extract a TV screen size in inches from a listing title.
    Returns None if size cannot be confidently determined.

    Matches:
      "75 inch TV"   → 75
      "75-inch TV"   → 75
      '75" TV'       → 75
      "75in TV"      → 75  (no-space shorthand)

    Conservative: bare numbers without an inch/in/" suffix are NOT matched
    (e.g. "Samsung 75 OLED TV" → None, "65 in good condition" → None).
    Only values in the realistic TV range (20–120 inches) are returned.
    """
    m = _TV_SIZE_TITLE_RE.search(title)
    if not m:
        return None
    raw = m.group(1) or m.group(2)
    if raw is None:
        return None
    size = int(raw)
    return size if _TV_SIZE_MIN <= size <= _TV_SIZE_MAX else None


def _extract_gpu_tier_rank_from_title(title: str) -> Optional[int]:
    """
    Return the highest GPU tier rank found in a listing title, or None.

    Checks every model in GPU_TIER_RANK against the title (uppercase) and
    returns the maximum rank.  Taking the maximum means a title containing
    both "RTX 3080" and "RTX 3080 TI" (as substrings) resolves to the
    higher-tier TI rank — correct behaviour.

    Conservative: if no recognised GPU model is found → returns None, and
    the caller must pass the listing through (never false-reject on unknown
    hardware).
    """
    from engine.verticals import GPU_TIER_RANK  # local import to avoid circular deps

    title_upper = title.upper()
    best_rank: Optional[int] = None
    for model_upper, rank in GPU_TIER_RANK.items():
        if model_upper in title_upper:
            if best_rank is None or rank > best_rank:
                best_rank = rank
    return best_rank


# ---------------------------------------------------------------------------
# Rule evaluator
# ---------------------------------------------------------------------------

def _find_rejection_reason(listing: Listing, rules: dict) -> Optional[str]:
    """
    Core rule evaluator.  Returns a short human-readable string describing
    the FIRST rule that rejects the listing, or None if all rules pass.

    Evaluation order:
      1. min_price / max_price
      2. include_keywords  (OR — any match required)
      3. require_all_keywords  (AND — all must be present)
      4. exclude_keywords  (any match rejects)
      5. Structured title-parsed constraints (conservative: missing = pass)
         a. max_miles (vehicles)
         b. min_capacity_gb (RAM)
         c. min_year / max_year (vehicles)
         d. min_vram_gb (GPU — only when user said "vram" explicitly)
         e. min_speed_mhz (RAM)
         f. min_size_inches / max_size_inches (TV)
         g. min_gpu_class (GPU — tier-based, conservative)

    The optional "vertical" key in the rules dict is metadata only; it is
    included in log/debug messages to clarify which vertical's logic fired.
    """
    if not rules:
        return None

    # Vertical label for contextual log messages (metadata only, not enforced).
    _vertical = rules.get("vertical", "")
    _vpfx = f"[{_vertical}] " if _vertical else ""

    # --- price bounds ---
    min_price = rules.get("min_price")
    if min_price is not None:
        p = listing.price
        if p is None:
            return f"{_vpfx}missing price while min_price filter is active"
        if p < min_price:
            return f"{_vpfx}price ${p} < min_price ${min_price}"

    max_price = rules.get("max_price")
    if max_price is not None:
        p = listing.price
        if p is None:
            return f"{_vpfx}missing price while max_price is required"
        if p > max_price:
            return f"{_vpfx}price ${p} > max_price ${max_price}"

    # --- keyword filters ---

    # include_keywords — OR / any() semantics (backward-compatible).
    include_keywords = rules.get("include_keywords") or []
    if include_keywords:
        title_lower = listing.title.lower()
        vehicle_make = rules.get("vehicle_make")
        vehicle_model = rules.get("vehicle_model")
        if _vertical == "vehicles" and vehicle_make and vehicle_model:
            reason = _vehicle_include_rejection(
                title_lower, str(vehicle_make), str(vehicle_model), include_keywords, _vpfx,
            )
            if reason:
                return reason
        elif not any(str(kw).lower() in title_lower for kw in include_keywords):
            shown = ", ".join(f'"{kw}"' for kw in include_keywords[:4])
            if len(include_keywords) > 4:
                shown += f" +{len(include_keywords)-4} more"
            return f"{_vpfx}title missing include keyword (any of: {shown})"

    # require_all_keywords — AND / all() semantics (strict, opt-in).
    require_all_keywords = rules.get("require_all_keywords") or []
    if require_all_keywords:
        title_lower = listing.title.lower()
        for kw in require_all_keywords:
            if str(kw).lower() not in title_lower:
                return f'{_vpfx}title missing required keyword "{kw}"'

    # exclude_keywords — any match rejects.
    exclude_keywords = rules.get("exclude_keywords") or []
    if exclude_keywords:
        title_lower = listing.title.lower()
        for kw in exclude_keywords:
            if str(kw).lower() in title_lower:
                return f'{_vpfx}excluded keyword "{kw}"'

    hunt_subtype = rules.get("hunt_subtype")
    title_lower = listing.title.lower()
    if hunt_subtype == "gpu" and _looks_like_gpu_whole_system(title_lower):
        return f"{_vpfx}whole system/laptop listing (GPU card hunt)"
    if hunt_subtype == "ram" and _looks_like_ram_whole_computer(title_lower):
        return f"{_vpfx}whole computer listing (RAM kit hunt)"

    # --- structured constraints extracted from title ---
    # Conservative: if the value cannot be parsed from the title, pass through.

    # vehicles: mileage cap
    max_miles = rules.get("max_miles")
    if max_miles is not None:
        miles = _extract_miles_from_title(listing.title)
        if miles is not None and miles > max_miles:
            return f"{_vpfx}mileage {miles:,} > max_miles {max_miles:,}"

    # RAM: minimum capacity
    min_capacity_gb = rules.get("min_capacity_gb")
    if min_capacity_gb is not None:
        capacity_gb = _extract_ram_gb_from_title(listing.title)
        if capacity_gb is not None and capacity_gb < min_capacity_gb:
            return f"{_vpfx}capacity {capacity_gb}GB < min_capacity_gb {min_capacity_gb}GB"

    # vehicles: model-year range
    min_year = rules.get("min_year")
    max_year = rules.get("max_year")
    if min_year is not None or max_year is not None:
        year = _extract_year_from_title(listing.title)
        if year is not None:
            if min_year is not None and year < min_year:
                return f"{_vpfx}year {year} < min_year {min_year}"
            if max_year is not None and year > max_year:
                return f"{_vpfx}year {year} > max_year {max_year}"

    # GPU: VRAM minimum (only set when user explicitly said "vram")
    min_vram_gb = rules.get("min_vram_gb")
    if min_vram_gb is not None:
        vram_gb = _extract_vram_gb_from_title(listing.title)
        if vram_gb is not None and vram_gb < min_vram_gb:
            return f"{_vpfx}vram {vram_gb}GB < min_vram_gb {min_vram_gb}GB"

    # RAM: speed minimum
    min_speed_mhz = rules.get("min_speed_mhz")
    if min_speed_mhz is not None:
        speed_mhz = _extract_speed_mhz_from_title(listing.title)
        if speed_mhz is not None and speed_mhz < min_speed_mhz:
            return f"{_vpfx}speed {speed_mhz}MHz < min_speed_mhz {min_speed_mhz}MHz"

    # TV: screen size range (structured check, more precise than substring match)
    # Conservative: if size cannot be extracted from the title → pass through.
    min_size_inches = rules.get("min_size_inches")
    max_size_inches = rules.get("max_size_inches")
    if min_size_inches is not None or max_size_inches is not None:
        size_in = _extract_tv_size_from_title(listing.title)
        if size_in is not None:
            if min_size_inches is not None and size_in < min_size_inches:
                return (
                    f'{_vpfx}TV size {size_in}" < min_size_inches {min_size_inches}"'
                )
            if max_size_inches is not None and size_in > max_size_inches:
                return (
                    f'{_vpfx}TV size {size_in}" > max_size_inches {max_size_inches}"'
                )
        else:
            log.debug(
                "%s[vertical-filter] TV size not found in title — passing through: %r",
                _vpfx,
                listing.title[:80],
            )

    # GPU: tier-based minimum class (conservative — unknown GPU in title → pass)
    min_gpu_class = rules.get("min_gpu_class")
    if min_gpu_class is not None:
        from engine.verticals import GPU_TIER_RANK  # local import — avoids circular dep

        min_rank = GPU_TIER_RANK.get(min_gpu_class.upper())
        if min_rank is not None:
            title_rank = _extract_gpu_tier_rank_from_title(listing.title)
            if title_rank is not None and title_rank < min_rank:
                return (
                    f"{_vpfx}GPU in title is below min_gpu_class "
                    f'"{min_gpu_class}" (tier {title_rank} < {min_rank})'
                )
            if title_rank is None:
                log.debug(
                    "%s[vertical-filter] GPU model not identified in title — "
                    "passing through: %r",
                    _vpfx,
                    listing.title[:80],
                )

    return None


def matches_rules(listing: Listing, rules: dict) -> bool:
    """Return True if the listing passes all rules defined in the hunt config."""
    return _find_rejection_reason(listing, rules) is None


def rejection_reason(listing: Listing, rules: dict) -> Optional[str]:
    """
    Return a human-readable rejection reason string, or None if the listing
    passes all rules.

    Use this in run_hunt to log exactly why a listing was filtered:

        reason = rejection_reason(listing, rules)
        if reason is not None:
            log.info("FILTERED %r: %s", listing.title[:60], reason)
    """
    return _find_rejection_reason(listing, rules)
