"""
engine/llm_translator.py

Translates a natural-language hunt intent into a structured, validated
HuntTranslation ready for hunt_service.create_hunt().

Two backends are available, selected via VULTURE_TRANSLATOR env var:
  rules  (default) — deterministic rule-matching; no API key, no network call.
  openai           — GPT-backed; see _translate_openai() for requirements.

The translator is stateless and has no side effects.  It does NOT touch the
database; persistence is the caller's responsibility.

Typical usage:
    from engine.llm_translator import translate, TranslationError
    try:
        t = translate("75 inch 4K TV under $500", location="houston")
    except TranslationError as exc:
        return error_response(str(exc))   # never persist bad translations
    hunt = create_hunt(name=t.name, search_terms=t.search_terms, ...)

What each vertical can extract and enforce at scan time
-------------------------------------------------------
tv_home_theater:
  extracted    : size_inches, resolution, panel_type (oled/qled), brand, max_price
  enforced     : brand + size via include_keywords title match; max_price
  conservative : panel / brand only enforced if found in title; no title parse needed

vehicles:
  extracted    : make, model, max_price, max_miles, min_year, max_year
  enforced     : make+model via include_keywords; max_miles, min_year, max_year
                 from title parse; min_price (placeholder $0/$1 filter)
  conservative : year/mileage only rejected when explicitly stated in title

computer_parts (GPU):
  extracted    : gpu_model, max_price, min_vram_gb (only when "vram" explicit)
  enforced     : model number via include_keywords; min_vram_gb from title parse
  conservative : VRAM only extracted when user says "vram" or "video memory"

computer_parts (RAM):
  extracted    : ram_type, min_capacity_gb, max_price
  enforced     : ddr-type via include_keywords; min_capacity_gb from title parse
  conservative : capacity only rejected when stated explicitly (e.g. "4GB DDR4")
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from typing import Optional

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Backend constants
# ---------------------------------------------------------------------------
_BACKEND_RULES  = "rules"
_BACKEND_OPENAI = "openai"
_VALID_BACKENDS = {_BACKEND_RULES, _BACKEND_OPENAI}


# ---------------------------------------------------------------------------
# Vertical registry
# ---------------------------------------------------------------------------
VERTICALS: dict[str, dict] = {
    "tv_home_theater": {
        "display_name": "TV / Home Theater",
        "sources": ["craigslist"],
        "keywords": [
            "tv", "television", "oled", "qled", "qned",
            "4k", "8k", "uhd", "hdtv", "smart tv",
            "flat screen", "flat panel", "plasma",
        ],
        "size_pattern": True,
        "default_exclude": [
            "stand", "tv stand", "mount", "wall mount", "bracket", "remote",
            "wanted", "looking for", "iso",
        ],
    },
    "computer_parts": {
        "display_name": "Computer Parts",
        "sources": ["craigslist"],
        "keywords": [
            # GPU
            "gpu", "graphics card", "video card",
            "rtx", "gtx", "rx ", "radeon", "nvidia", "geforce",
            # CPU
            "cpu", "processor", "ryzen", "intel core", "xeon",
            "i3 ", "i5 ", "i7 ", "i9 ",
            # RAM / memory — detected here; specialised handling in code
            "ram", "memory", "ddr4", "ddr5", "ddr3", "dimm",
            # Storage
            "ssd", "nvme", "m.2", "hard drive", "hdd",
            # Other
            "motherboard", "mobo", "psu", "power supply",
        ],
        "size_pattern": False,
        "default_exclude": [
            "wanted", "looking for", "iso",
            "broken", "for parts", "not working",
        ],
    },
    "laptops_computers": {
        "display_name": "Laptops / Computers",
        "sources": ["craigslist"],
        "keywords": [
            "laptop", "notebook", "macbook", "chromebook",
            "desktop", "pc", "imac", "mac mini", "gaming pc",
        ],
        "size_pattern": False,
        "default_exclude": [
            "wanted", "looking for", "iso",
            "charger", "adapter", "bag",
        ],
    },
    "vehicles": {
        "display_name": "Vehicles",
        "sources": ["craigslist"],
        "keywords": [
            # Generic body styles
            "car", "truck", "suv", "van", "pickup", "sedan", "coupe",
            # Makes — keep in sync with _VEHICLE_MAKES below
            "honda", "toyota", "ford", "chevy", "chevrolet", "nissan",
            "jeep", "dodge", "hyundai", "kia", "subaru", "mazda",
            "bmw", "mercedes", "audi", "volkswagen", "vw",
            "porsche", "lexus", "acura", "infiniti", "volvo",
            "cadillac", "gmc", "buick", "mitsubishi", "chrysler",
            # Popular models
            "civic", "accord", "corolla", "camry", "tacoma", "tundra",
            "mustang", "f150", "f-150", "silverado", "4runner",
            "highlander", "rav4", "miata", "mx-5",
            # Other vehicle types
            "motorcycle", "scooter", "moped",
            "rv", "motorhome", "camper",
        ],
        "size_pattern": False,
        "default_exclude": [
            # Parts / salvage
            "part out", "parts out", "parts only", "for parts",
            "parting out", "parts car", "salvage",
            "rebuilt title", "flood damage",
            # Components (not whole vehicle)
            "wheel", "wheels", "rim", "rims",
            "tire", "tires", "bumper", "door", "hood", "fender",
            # Non-vehicle merchandise: collectibles, memorabilia, decor
            "sculpture", "figurine", "collectable", "collectible",
            "miniature", "diecast", "die cast", "die-cast",
            "poster", "memorabilia", "keychain", "toy car", "replica",
            # Classified clutter
            "wanted", "looking for", "iso",
            "will trade",
        ],
    },
    "furniture_home": {
        "display_name": "Furniture / Home Goods",
        "sources": ["craigslist"],
        "keywords": [
            "sofa", "couch", "chair", "recliner",
            "table", "coffee table", "dining table",
            "desk", "bed", "mattress", "dresser",
            "bookshelf", "bookcase", "cabinet",
        ],
        "size_pattern": False,
        "default_exclude": ["wanted", "looking for", "iso"],
    },
    "general": {
        "display_name": "General",
        "sources": ["craigslist"],
        "keywords": [],   # fallback — matches everything
        "size_pattern": False,
        "default_exclude": ["wanted", "looking for", "iso"],
    },
}

# Additional exclude keywords used only for RAM sub-hunts inside computer_parts.
# Not part of VERTICALS so they don't pollute GPU/CPU searches.
_RAM_EXCLUDE = [
    "sodimm", "so-dimm", "laptop memory", "laptop ram",
    "ecc", "registered", "buffered", "server memory",
    "broken", "for parts", "not working",
    "wanted", "looking for", "iso",
]


# ---------------------------------------------------------------------------
# HuntTranslation — structured output of the translator
# ---------------------------------------------------------------------------

@dataclass
class HuntTranslation:
    """
    Structured translation result.

    This is NOT a database model.  The caller passes its fields directly to
    hunt_service.create_hunt() and the result is persisted as a Hunt.

    translated_by records the backend and is embedded in the notes field
    so operators can see it in /hunt_show.
    """
    name:             str
    vertical:         str
    category:         str
    source_sites:     list[str]
    search_terms:     list[str]
    include_keywords: list[str]
    exclude_keywords: list[str]
    max_price:        Optional[int]
    location:         Optional[str]
    radius:           Optional[int]
    notes:            str          # reasoning / constraint summary
    adapter_options:  dict = field(default_factory=dict)
    translated_by:    str = _BACKEND_RULES


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class TranslationError(ValueError):
    """
    Raised when translation output fails validation or is too underspecified
    to produce a useful hunt.

    Callers must surface this as a user-facing error and must NOT persist
    an empty or ambiguous hunt on this exception.
    """


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def translate(
    intent: str,
    *,
    location: Optional[str] = None,
    max_price: Optional[int] = None,
) -> HuntTranslation:
    """
    Translate a natural-language hunt intent into a validated HuntTranslation.

    Parameters
    ----------
    intent    : str  — what the user is looking for
    location  : str, optional — Craigslist subdomain override (e.g. 'houston').
                Must be a single-word token.  Multi-word place names are
                rejected to prevent DNS failures.
    max_price : int, optional — price ceiling override.

    Returns
    -------
    HuntTranslation — always validated before returning.

    Raises
    ------
    TranslationError — on blank intent, validation failure, or unavailable backend.
    """
    intent = (intent or "").strip()
    if not intent:
        raise TranslationError("Intent must not be empty")

    raw_backend = os.getenv("VULTURE_TRANSLATOR", _BACKEND_RULES).strip().lower()
    if raw_backend not in _VALID_BACKENDS:
        log.warning(
            "Unknown VULTURE_TRANSLATOR %r; falling back to %r",
            raw_backend, _BACKEND_RULES,
        )
        raw_backend = _BACKEND_RULES

    log.info("Translating intent [backend=%s]: %r", raw_backend, intent)

    if raw_backend == _BACKEND_OPENAI:
        result = _translate_openai(intent, location=location, max_price=max_price)
    else:
        result = _translate_rules_based(intent, location=location, max_price=max_price)

    _validate_translation(result)
    log.info(
        "Translation done: name=%r vertical=%r terms=%s include=%s",
        result.name, result.vertical, result.search_terms, result.include_keywords,
    )
    return result


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def _validate_translation(t: HuntTranslation) -> None:
    """Raise TranslationError if t is malformed or underspecified."""
    errors: list[str] = []

    if not (t.name or "").strip():
        errors.append("'name' is empty")

    if not isinstance(t.source_sites, list) or not t.source_sites:
        errors.append("'source_sites' must be a non-empty list")
    elif any(not isinstance(s, str) or not s.strip() for s in t.source_sites):
        errors.append("all 'source_sites' entries must be non-empty strings")

    if not isinstance(t.search_terms, list) or not t.search_terms:
        errors.append("'search_terms' must be a non-empty list")
    elif any(not isinstance(s, str) or not s.strip() for s in t.search_terms):
        errors.append("all 'search_terms' entries must be non-empty strings")

    for fname in ("include_keywords", "exclude_keywords"):
        lst = getattr(t, fname)
        if not isinstance(lst, list):
            errors.append(f"'{fname}' must be a list")
        elif any(not isinstance(s, str) for s in lst):
            errors.append(f"'{fname}' must contain only strings")

    if t.max_price is not None:
        if not isinstance(t.max_price, int) or t.max_price < 0:
            errors.append("'max_price' must be a non-negative integer or None")

    if t.vertical not in VERTICALS:
        errors.append(f"'vertical' is unknown: {t.vertical!r}")

    if not isinstance(t.adapter_options, dict):
        errors.append("'adapter_options' must be a dict")

    if errors:
        raise TranslationError(
            "Translation output is invalid — hunt was not saved.  "
            f"Issues: {'; '.join(errors)}"
        )


# ---------------------------------------------------------------------------
# Numeric normalization — applied FIRST before all regex extraction
# ---------------------------------------------------------------------------

def _expand_k_numbers(text: str) -> str:
    """
    Expand shorthand numeric notation before extraction.

      '10k'   → '10000'
      '1.5k'  → '1500'
      '100k'  → '100000'
      '2.5k'  → '2500'

    Only replaces bare k/K suffixes on numbers.  Does not affect words
    like 'ok', 'kayak', etc. because those aren't preceded by a digit.
    Applied to the full intent string (already lowercased) so that all
    downstream price, mileage, and capacity regexes see plain integers.
    """
    def _expand(m: re.Match) -> str:
        return str(int(float(m.group(1)) * 1000))
    return re.sub(r'\b(\d+(?:\.\d+)?)\s*[kK]\b', _expand, text)


# ---------------------------------------------------------------------------
# Attribute extraction helpers
# ---------------------------------------------------------------------------

# Price: "under $500", "$500 max", "$500 or less", "< $500"
#
# The negative lookahead (?!\s*(?:miles?|mi)\b) after the number prevents
# "under 100000 miles" from being captured as a price when a mileage constraint
# appears before a dollar constraint in the intent string.  The $-prefixed
# alternative is already unambiguous and needs no lookahead.
_PRICE_RE = re.compile(
    r'(?:under|below|max(?:imum)?|less\s+than|up\s+to|at\s+most|<)\s*\$?\s*'
    r'(\d{1,7}(?:,\d{3})*)(?!\s*(?:miles?|mi)\b)'
    r'|\$\s*(\d{1,7}(?:,\d{3})*)\s*(?:or\s+(?:less|under|below)|max(?:imum)?)?',
    re.IGNORECASE,
)

# Mileage: "less than 100000 miles", "under 80000 miles", "100000 miles or less"
_MILES_RE = re.compile(
    r'(?:less\s+than|under|below|no\s+more\s+than|at\s+most|<)\s*(\d{1,7}(?:,\d{3})*)\s*miles?'
    r'|(\d{1,7}(?:,\d{3})*)\s*miles?\s*(?:or\s+(?:less|under|below)|max(?:imum)?)?',
    re.IGNORECASE,
)

# Minimum RAM speed: "more than 3000MHz", "at least 3200MHz", "3600MHz or faster"
# Also matches MT/s (DDR5 spec sheets often use MT/s instead of MHz).
_MIN_SPEED_MHZ_RE = re.compile(
    r'(?:more\s+than|at\s+least|minimum|min|over|faster\s+than|>\s*|>=\s*)\s*(\d{3,5})\s*(?:mhz|mt/s)\b'
    r'|(\d{3,5})\s*(?:mhz|mt/s)\s+(?:or\s+(?:more|faster|greater|higher))',
    re.IGNORECASE,
)

# Minimum GB: "more than 8gb", "at least 16gb", "8gb or greater", ">8gb"
_MIN_GB_RE = re.compile(
    r'(?:more\s+than|at\s+least|minimum|min|over|>\s*|>=\s*)\s*(\d+)\s*gb'
    r'|(\d+)\s*gb\s+(?:or\s+(?:more|greater|higher)|minimum|min)',
    re.IGNORECASE,
)

# Size in inches: "75 inch", "75-inch", '75"', "75in"
_SIZE_RE = re.compile(r'\b(\d{2,3})\s*(?:-\s*)?(?:inch(?:es?)?|\bin\b|")', re.IGNORECASE)

# Resolution aliases — checked in order (more specific first)
_RESOLUTION_MAP: list[tuple[str, list[str]]] = [
    ("8k",    ["8k", "7680"]),
    ("4k",    ["4k", "uhd", "ultra hd", "2160p"]),
    ("1440p", ["1440p", "1440", "2k", "qhd", "quad hd"]),
    ("1080p", ["1080p", "1080", "fhd", "full hd", "full-hd"]),
    ("720p",  ["720p", "720", "hd ready"]),
]

# TV brands — checked in order (longer/more-specific first)
_TV_BRANDS = [
    "samsung", "sony", "panasonic", "hisense", "toshiba",
    "philips", "vizio", "insignia", "westinghouse",
    "tcl", "onn", "lg",
]

# TV panel types — checked most-specific first (oled before led)
_TV_PANELS = ["oled", "qled", "qned", "mini-led", "mini led", "neo qled", "led", "plasma"]

# Vehicle year range patterns
# "2016 or newer / later"  →  min_year = 2016
# "newer / later than 2016"  →  min_year = 2016  (treated inclusive for simplicity)
# "from / since / after / no older than 2016"  →  min_year = 2016
_YEAR_MIN_RE = re.compile(
    r'((?:19|20)\d{2})\s+(?:or\s+)?(?:newer|later|above)\b'
    r'|(?:newer|later|more\s+recent)\s+than\s+((?:19|20)\d{2})'
    r'|(?:from|since|after|no\s+older\s+than)\s+((?:19|20)\d{2})',
    re.IGNORECASE,
)

# "before / older than / prior to / no newer than 2022"  →  max_year = 2022
# "2022 or older / earlier"  →  max_year = 2022
_YEAR_MAX_RE = re.compile(
    r'(?:before|older\s+than|prior\s+to|no\s+newer\s+than)\s+((?:19|20)\d{2})'
    r'|((?:19|20)\d{2})\s+(?:or\s+)?(?:older|earlier)\b',
    re.IGNORECASE,
)

# GPU VRAM minimum — only fires when "vram" or "video memory" appears explicitly
# "at least 8gb vram", "16gb vram", "vram 12gb or more"
_VRAM_INTENT_RE = re.compile(
    r'(?:at\s+least|minimum|min|over|>=?\s*)?\s*(\d+)\s*[gG][bB]\s*(?:vram|video\s+(?:memory|ram))'
    r'|(?:vram|video\s+(?:memory|ram))\s*(?:of\s+)?(?:at\s+least\s+)?(\d+)\s*[gG][bB]',
    re.IGNORECASE,
)

# Whole-system / non-standalone-card terms excluded from all GPU hunts.
#
# Design principles:
#   - Only terms that unambiguously indicate a complete system, not a bare card.
#   - Bare words like "pc", "desktop", "computer", "tower" are intentionally
#     omitted: sellers routinely write "desktop GPU" or "great for gaming pc"
#     in standalone-card titles.
#   - Compound phrases (e.g. "gaming pc") are safe because they are far less
#     likely to appear in a pure GPU listing title.
#   - "prebuilt" is omitted: sellers sometimes write "pulled from prebuilt"
#     when selling a card they removed from a system.
_GPU_SYSTEM_EXCLUDE: list[str] = [
    "laptop",           # "ASUS TUF Gaming Laptop RTX 3070" — most common false positive
    "notebook",         # synonym for laptop
    "gaming pc",        # complete gaming system
    "gaming desktop",   # complete gaming desktop
    "gaming computer",  # complete gaming computer
    "complete system",  # explicit full-system phrase
    "full system",      # explicit full-system phrase
]

# Words that must NOT be treated as a vehicle model name
_NOT_A_MODEL = {
    "less", "more", "than", "under", "over", "about", "around",
    "with", "without", "and", "or", "for", "the", "a", "an",
    "in", "on", "at", "to", "from", "up", "down", "not",
    "is", "are", "was", "were", "have", "has",
    "dollars", "miles", "km", "years", "year",
    "old", "new", "used", "clean", "good", "low", "high",
    "very", "just", "only", "all", "find", "me", "want",
}

# Vehicle makes (word-boundary matched against the intent)
_VEHICLE_MAKES = {
    "honda", "toyota", "ford", "chevy", "chevrolet", "nissan",
    "bmw", "mercedes", "jeep", "dodge", "hyundai", "kia",
    "subaru", "mazda", "volkswagen", "vw", "ram", "gmc",
    "cadillac", "lexus", "acura", "infiniti", "volvo",
    "audi", "porsche", "mitsubishi", "chrysler", "buick",
}

# Known model → canonical make.
_MODEL_TO_MAKE: dict[str, str] = {
    "civic":      "honda",
    "accord":     "honda",
    "cr-v":       "honda",
    "crv":        "honda",
    "pilot":      "honda",
    "odyssey":    "honda",
    "corolla":    "toyota",
    "camry":      "toyota",
    "tacoma":     "toyota",
    "tundra":     "toyota",
    "4runner":    "toyota",
    "highlander": "toyota",
    "rav4":       "toyota",
    "mustang":    "ford",
    "f150":       "ford",
    "f-150":      "ford",
    "bronco":     "ford",
    "silverado":  "chevrolet",
    "tahoe":      "chevrolet",
    "suburban":   "chevrolet",
    "colorado":   "chevrolet",
    "altima":     "nissan",
    "maxima":     "nissan",
    "sentra":     "nissan",
    "pathfinder": "nissan",
    "frontier":   "nissan",
    "wrangler":   "jeep",
    "cherokee":   "jeep",
    "challenger": "dodge",
    "charger":    "dodge",
    "ram 1500":   "ram",
    "sonata":     "hyundai",
    "elantra":    "hyundai",
    "tucson":     "hyundai",
    "soul":       "kia",
    "optima":     "kia",
    "sportage":   "kia",
    "outback":    "subaru",
    "forester":   "subaru",
    "impreza":    "subaru",
    "cx-5":       "mazda",
    "cx5":        "mazda",
    "miata":      "mazda",
    "mx-5":       "mazda",
    "golf":       "volkswagen",
    "jetta":      "volkswagen",
    "passat":     "volkswagen",
}


def _detect_vertical(intent_lower: str) -> tuple[str, dict]:
    """Return (vertical_key, config) for the best-matching vertical."""
    best_key   = "general"
    best_score = 0
    for key, cfg in VERTICALS.items():
        if key == "general":
            continue
        score = sum(1 for kw in cfg["keywords"] if kw in intent_lower)
        if score > best_score:
            best_score = score
            best_key = key
    return best_key, VERTICALS[best_key]


def _is_ram_hunt(intent_lower: str) -> bool:
    """
    Return True when a computer_parts intent is specifically about system RAM / memory.

    'vram' contains 'ram' as a substring, so a plain 'in' check would misclassify
    GPU intents like "RTX 4080 with 16gb vram" as RAM hunts.  Guard against this
    by checking for GPU-specific VRAM keywords first.
    """
    # GPU-context indicators — "vram" / "video memory" always mean a GPU hunt
    if "vram" in intent_lower or "video memory" in intent_lower:
        return False
    # Word-boundary match for "ram" so "vram" can't sneak through
    if re.search(r'\bram\b', intent_lower):
        return True
    return any(kw in intent_lower for kw in ["memory", "ddr4", "ddr5", "ddr3", "dimm"])


def _extract_size(intent_lower: str) -> Optional[int]:
    m = _SIZE_RE.search(intent_lower)
    return int(m.group(1)) if m else None


def _extract_resolution(intent_lower: str) -> Optional[str]:
    for label, aliases in _RESOLUTION_MAP:
        if any(alias in intent_lower for alias in aliases):
            return label
    return None


def _extract_tv_brand(intent_lower: str) -> Optional[str]:
    """Return the first recognised TV brand found in the intent."""
    for brand in _TV_BRANDS:
        if brand in intent_lower:
            return brand
    return None


def _extract_tv_panel(intent_lower: str) -> Optional[str]:
    """
    Return a panel-type label if explicitly requested.

    Checked most-specific first so 'oled' is never swallowed by 'led'.
    Returns the canonical lowercase token, e.g. 'oled', 'qled', 'mini-led'.
    """
    for panel in _TV_PANELS:
        if panel in intent_lower:
            # Normalise "mini led" → "mini-led"
            return panel.replace(" ", "-")
    return None


def _extract_year_range(intent_lower: str) -> tuple[Optional[int], Optional[int]]:
    """
    Extract a vehicle model-year range from the intent.

    Handles:
      "2016 or newer"          → (2016, None)
      "newer than 2018"        → (2018, None)   inclusive
      "from / since 2015"      → (2015, None)
      "no older than 2017"     → (2017, None)
      "before 2022"            → (None, 2022)
      "2020 or older"          → (None, 2020)

    Returns (min_year, max_year); either may be None.
    Year must be in range 1960–2030 to be valid.
    """
    def _valid(yr: int) -> Optional[int]:
        return yr if 1960 <= yr <= 2030 else None

    min_yr: Optional[int] = None
    max_yr: Optional[int] = None

    m = _YEAR_MIN_RE.search(intent_lower)
    if m:
        raw = m.group(1) or m.group(2) or m.group(3)
        if raw:
            min_yr = _valid(int(raw))

    m = _YEAR_MAX_RE.search(intent_lower)
    if m:
        raw = m.group(1) or m.group(2)
        if raw:
            max_yr = _valid(int(raw))

    return min_yr, max_yr


def _extract_price(intent_lower: str, override: Optional[int]) -> Optional[int]:
    """Extract max price.  Override (from Discord param) takes priority."""
    if override is not None:
        return override
    m = _PRICE_RE.search(intent_lower)
    if m:
        raw = m.group(1) or m.group(2)
        if raw:
            return int(raw.replace(",", ""))
    return None


def _extract_miles(intent_lower: str) -> Optional[int]:
    """
    Extract a maximum mileage constraint.

    Expects k-suffix expansion to have been applied first (so '100k miles'
    becomes '100000 miles' before this runs).
    """
    m = _MILES_RE.search(intent_lower)
    if m:
        raw = m.group(1) or m.group(2)
        if raw:
            return int(raw.replace(",", ""))
    return None


def _extract_ram_type(intent_lower: str) -> Optional[str]:
    """Return 'ddr5', 'ddr4', or 'ddr3' if mentioned; else None."""
    for t in ("ddr5", "ddr4", "ddr3"):
        if t in intent_lower:
            return t
    return None


def _extract_min_gb(intent_lower: str) -> Optional[int]:
    """Extract a minimum GB capacity for RAM hunts."""
    m = _MIN_GB_RE.search(intent_lower)
    if m:
        raw = m.group(1) or m.group(2)
        if raw:
            return int(raw)
    return None


def _extract_min_speed_mhz(intent_lower: str) -> Optional[int]:
    """
    Extract a minimum RAM speed constraint in MHz (or MT/s, treated as equivalent).

    Examples:
      "more than 3000mhz"       → 3000
      "at least 3200mhz"        → 3200
      "3600mhz or faster"       → 3600
      "minimum 4800mt/s"        → 4800
    """
    m = _MIN_SPEED_MHZ_RE.search(intent_lower)
    if m:
        raw = m.group(1) or m.group(2)
        if raw:
            return int(raw)
    return None


def _extract_min_vram_gb(intent_lower: str) -> Optional[int]:
    """
    Extract a minimum VRAM requirement for GPU hunts.

    Conservative: only fires when 'vram' or 'video memory' is explicitly
    present in the intent.  A bare 'at least 8gb' in a GPU context is NOT
    treated as VRAM — the user must say 'vram' to avoid ambiguity with
    system RAM.

    Examples:
      "at least 8gb vram"       → 8
      "16gb vram or more"       → 16
      "vram of at least 12gb"   → 12
    """
    m = _VRAM_INTENT_RE.search(intent_lower)
    if m:
        raw = m.group(1) or m.group(2)
        if raw:
            return int(raw)
    return None


def _extract_gpu_model(intent_lower: str) -> Optional[str]:
    """
    Extract a GPU model identifier and return it in uppercase.

    Handles three cases in priority order:

    1. NVIDIA  : rtx 3080, rtx 4090 ti, gtx 1080 super
    2. AMD (rx prefix): rx 6800 xt, rx 7900 xtx
    3. AMD (bare number): 6700xt, 6700 xt, 7800xt, 7900xtx
       — requires an explicit xt/xtx suffix to avoid false-positive matches
         on prices like "$6700" or years like "2019".

    Unresolved edge case: bare "6800" with no prefix and no xt/xtx suffix
    cannot be detected without false-positive risk.  Users should write "rx 6800".
    """
    # NVIDIA: rtx/gtx + 3-4 digit number + optional tier (ti / super / xt)
    m = re.search(r'\b(rtx|gtx)\s*(\d{3,4})\s*(ti|super|xt)?\b', intent_lower)
    if m:
        prefix = m.group(1)
        number = m.group(2)
        tier   = m.group(3)
        # GTX ended with the 10xx generation; 20xx and above are RTX.
        # Silently correct "gtx 3080" → "RTX 3080" instead of creating an
        # invalid model string that would produce zero search results.
        if prefix == "gtx" and int(number) >= 2000:
            prefix = "rtx"
        parts = [prefix, number]
        if tier:
            parts.append(tier)
        return " ".join(parts).upper()

    # AMD with explicit "rx" prefix: rx 6800, rx 6800 xt, rx 7900 xtx
    m = re.search(r'\b(rx)\s*(\d{3,4})\s*(xtx|xt)?\b', intent_lower)
    if m:
        parts = [m.group(1), m.group(2)]
        if m.group(3):
            parts.append(m.group(3))
        return " ".join(parts).upper()

    # AMD bare number with mandatory xt/xtx suffix.
    # Covers RX 6xxx (6500–6999) and RX 7xxx (7500–7999) series.
    # The mandatory suffix prevents matching prices ("$6700" → no suffix → skip).
    m = re.search(r'\b(6[5-9]\d{2}|7[5-9]\d{2})\s*(xtx|xt)\b', intent_lower)
    if m:
        parts = ["rx", m.group(1), m.group(2)]
        return " ".join(parts).upper()

    return None


def _extract_vehicle_make_model(intent_lower: str) -> Optional[tuple[str, str]]:
    """
    Return (make, model) for the vehicle detected in the intent.

    Checks:
      1. Explicit make + following word as model ("honda civic", "toyota rav4")
      2. Known model with inferred make ("corolla" → ("toyota", "corolla"))
      3. Make alone ("honda")

    Returns a (make, model) tuple, or None if nothing vehicle-specific is found.
    The model is the empty string if only a make was found.
    """
    words = intent_lower.split()

    # Pass 1: look for an explicit make
    for i, word in enumerate(words):
        if word in _VEHICLE_MAKES:
            make = word
            # Accept alphanumeric model names (rav4, 4runner, f150) but reject
            # pure-digit years (2019) and k-suffix values (15k).
            nxt = words[i + 1] if i + 1 < len(words) else ""
            nxt_has_letter = bool(re.search(r'[a-z]', nxt))
            nxt_is_num_k   = bool(re.match(r'^\d+[kK]?$', nxt))
            if (nxt_has_letter
                    and not nxt_is_num_k
                    and nxt not in _VEHICLE_MAKES
                    and nxt not in _NOT_A_MODEL
                    and len(nxt) > 1):
                return make, nxt
            return make, ""

    # Pass 2: look for a known model name (infer make from _MODEL_TO_MAKE)
    for model, make in sorted(_MODEL_TO_MAKE.items(), key=lambda kv: -len(kv[0])):
        if model in intent_lower:
            return make, model

    return None


def _sanitize_craigslist_location(raw: Optional[str]) -> Optional[str]:
    """
    Validate that a location string is safe to use as a Craigslist subdomain.

    Craigslist subdomain rules:
      - Single token, no whitespace (e.g. 'houston', 'austin', 'sfbay')
      - Lowercase alphanumeric characters only
      - Typically 3–20 characters

    Multi-word strings like 'mandeville louisiana' are rejected because
    they produce DNS failures when used as subdomains.

    Returns the lowercased token if valid, or None with a warning if not.
    """
    if not raw:
        return None
    raw = raw.strip()
    if not raw:
        return None

    if re.search(r'\s', raw):
        log.warning(
            "Location %r contains spaces and is not a valid Craigslist subdomain.  "
            "Set location to a single token like 'houston' or leave it blank.  "
            "Ignoring location — runtime will use its default.",
            raw,
        )
        return None

    if len(raw) > 30 or not re.match(r'^[a-z0-9-]+$', raw, re.IGNORECASE):
        log.warning(
            "Location %r does not look like a valid Craigslist subdomain.  "
            "Ignoring — runtime will use its default.",
            raw,
        )
        return None

    return raw.lower()


# ---------------------------------------------------------------------------
# Vertical-specific search term / include_keyword builders
# ---------------------------------------------------------------------------

def _build_tv_translation(
    size: Optional[int],
    resolution: Optional[str],
    brand: Optional[str] = None,
    panel: Optional[str] = None,
) -> tuple[list[str], list[str], list[str]]:
    """
    TV / Home Theater.

    Returns (search_terms, include_kw, require_all_kw).

    Strategy:
      - Build a specific Craigslist phrase from brand + size + panel + resolution,
        e.g. "Samsung 75 inch OLED 4K TV".
      - Single discriminator (size only, or brand only): put it in include_kw so
        it is evaluated with the standard OR/any() rule — backward compatible.
      - Multiple discriminators (size + brand, or size + panel, etc.): put all of
        them in require_all_kw so that ALL must appear in the listing title.
        This prevents "LG 75 OLED" from passing a Samsung-specific hunt just
        because "75" alone matches.
    """
    parts: list[str] = []
    if brand:
        parts.append(brand.title())     # "Samsung"
    if size:
        parts.append(f"{size} inch")
    if panel:
        parts.append(panel.upper())     # "OLED", "QLED"
    if resolution:
        parts.append(resolution.upper())
    parts.append("TV")

    search_terms = [" ".join(parts)]

    # Collect all active discriminators
    strict_kw: list[str] = []
    if size:
        strict_kw.append(str(size))
    if brand:
        strict_kw.append(brand)
    if panel:
        strict_kw.append(panel)

    if len(strict_kw) <= 1:
        # Single discriminator: standard include_keywords (any/OR semantics)
        include_kw     = strict_kw
        require_all_kw = []
    else:
        # Multiple discriminators: strict require_all_keywords (all/AND semantics)
        include_kw     = []
        require_all_kw = strict_kw

    return search_terms, include_kw, require_all_kw


def _build_gpu_translation(
    gpu_model: Optional[str],
    intent: str,
) -> tuple[list[str], list[str], list[str]]:
    """
    GPU hunt.

    Returns (search_terms, include_keywords, model_exclude_keywords).

    Strategy
    --------
    Model-specific:
      - Search for the full model string (e.g. "RTX 3080 TI GPU").
      - include_keywords enforces that the title contains the right model/tier:
          No tier  → require the model number alone ("3080").
          With tier → require BOTH the spaced ("3080 ti") AND the run-together
                       ("3080ti") forms as OR alternatives, so sellers who write
                       "3080Ti" without a space are still matched correctly.
      - model_exclude_keywords: when hunting an XT model (not XTX), add the XTX
        variants to exclude so "RX 7900 XTX" is not matched by an "RX 7900 XT"
        hunt (the substring "7900 xt" is present in "7900 xtx").

    Generic (no model detected):
      - Clean price/noise words from the raw intent and use the remainder.
    """
    if gpu_model:
        search_terms = [f"{gpu_model} GPU"]
        num_m  = re.search(r'(\d{3,4})',              gpu_model)
        tier_m = re.search(r'\b(TI|SUPER|XT|XTX)\b', gpu_model)
        number = num_m.group(1)          if num_m  else None
        tier   = tier_m.group(1).lower() if tier_m else None

        if number and tier:
            # Require both the spaced and run-together forms so titles like
            # "RTX 3080 Ti 12GB" and "RTX 3080Ti" both match.
            include_kw = [f"{number} {tier}", f"{number}{tier}"]
        elif number:
            include_kw = [number]
        else:
            include_kw = []

        # Guard: "XT" is a substring of "XTX".  When hunting an XT model,
        # explicitly exclude XTX listings so "RX 7900 XTX" is not returned
        # by an "RX 7900 XT" hunt.  XTX hunts need no guard (XTX ⊄ XT).
        xtx_guard = [f"{number} xtx", f"{number}xtx"] if (tier == "xt" and number) else []
        # Combine XTX guard with the system-type exclusions that apply to
        # every GPU hunt (standalone-card hunts should never match laptops etc.)
        model_excl = xtx_guard + _GPU_SYSTEM_EXCLUDE
    else:
        # No specific model detected — strip price constraints and query-noise
        # words so Craigslist gets a clean search phrase instead of raw intent.
        clean = re.sub(
            r'(?:under|below|less\s+than|at\s+most|up\s+to|max(?:imum)?)\s*\$?\s*\d[\d,]*'
            r'|\$\s*\d[\d,]*',
            '', intent.lower(), flags=re.IGNORECASE,
        )
        clean = re.sub(
            r'\b(?:dollars?|bucks?|usd|for|with|and|or|the|a|an)\b',
            '', clean, flags=re.IGNORECASE,
        )
        clean = re.sub(r'\s+', ' ', clean).strip()
        search_terms = [(clean.title() if clean else intent)]
        include_kw   = []
        model_excl   = list(_GPU_SYSTEM_EXCLUDE)
    return search_terms, include_kw, model_excl


def _build_ram_translation(
    ram_type: Optional[str],
    min_gb: Optional[int],
) -> tuple[list[str], list[str], list[str]]:
    """
    RAM / memory hunt.

    Returns (search_terms, include_keywords, exclude_keywords).

    Strategy:
      - Add 'desktop' to the search phrase to push Craigslist away from
        laptop (SODIMM) results.
      - include_keywords = [ram_type] so the DDR generation must appear in
        the listing title.
      - Use the comprehensive _RAM_EXCLUDE list.
    """
    phrase_parts = []
    if ram_type:
        phrase_parts.append(ram_type.upper())   # "DDR4"
    phrase_parts.extend(["desktop", "RAM"])

    search_terms = [" ".join(phrase_parts)]     # e.g. ["DDR4 desktop RAM"]
    include_kw   = [ram_type] if ram_type else []
    return search_terms, include_kw, list(_RAM_EXCLUDE)


def _build_vehicle_translation(
    make: str,
    model: str,
    intent: str,
) -> tuple[list[str], list[str]]:
    """
    Vehicle hunt.

    Strategy:
      - If both make and model are known, search for the combined phrase
        ("Honda Civic") and set include_keywords to the phrase itself.
      - If only make: search and include on the make alone.
      - Fallback: use the cleaned intent as the search phrase.
    """
    if make and model:
        phrase       = f"{make} {model}".title()
        search_terms = [phrase]
        include_kw   = [f"{make} {model}"]
    elif make:
        search_terms = [make.title()]
        include_kw   = [make]
    else:
        search_terms = [intent]
        include_kw   = []
    return search_terms, include_kw


# ---------------------------------------------------------------------------
# Name generation
# ---------------------------------------------------------------------------

_NAME_STOPWORDS = {
    "a", "an", "the", "for", "in", "on", "at", "to", "of",
    "and", "or", "is", "are", "with", "under", "over",
    "some", "used", "new", "good", "me", "find",
}


def _generate_name(
    vertical: str,
    size: Optional[int],
    resolution: Optional[str],
    gpu_model: Optional[str],
    make: Optional[str],
    model: Optional[str],
    ram_type: Optional[str],
    search_terms: list[str],
    brand: Optional[str] = None,
    panel: Optional[str] = None,
) -> str:
    """
    Auto-generate a short, slug-style hunt name.

    Examples
    --------
    Samsung 75" OLED 4K TV  → "samsung_75in_oled_4k_tv"
    75" 4K TV               → "75in_4k_tv"
    GPU RTX 3080            → "rtx3080_gpu"
    DDR4 RAM                → "ddr4_desktop_ram"
    Vehicle honda civic     → "honda_civic"
    Vehicle honda only      → "honda_car"
    """
    parts: list[str] = []

    if vertical == "tv_home_theater":
        if brand:
            parts.append(brand)
        if size:
            parts.append(f"{size}in")
        if panel:
            parts.append(panel.replace("-", ""))    # "miniled" not "mini-led"
        if resolution:
            parts.append(resolution)
        parts.append("tv")

    elif vertical == "computer_parts":
        if gpu_model:
            parts.append(re.sub(r'\s+', '', gpu_model.lower()))  # "rtx3080ti"
            parts.append("gpu")
        elif ram_type:
            parts.append(ram_type)
            parts.extend(["desktop", "ram"])
        else:
            words = [w for w in search_terms[0].lower().split()
                     if w not in _NAME_STOPWORDS]
            parts.extend(words[:3])

    elif vertical == "laptops_computers":
        words = [w for w in search_terms[0].lower().split()
                 if w not in _NAME_STOPWORDS]
        parts.extend(words[:3])

    elif vertical == "vehicles":
        if make and model:
            parts.extend([make, model])
        elif make:
            parts.extend([make, "car"])
        else:
            words = [w for w in search_terms[0].lower().split()
                     if w not in _NAME_STOPWORDS]
            parts.extend(words[:3])

    else:
        words = [w for w in search_terms[0].lower().split()
                 if w not in _NAME_STOPWORDS]
        parts.extend(words[:3])

    slug = re.sub(r'[^a-z0-9_]', '', "_".join(parts))
    return slug or "hunt"


# ---------------------------------------------------------------------------
# Rules-based translator — main function
# ---------------------------------------------------------------------------

def _translate_rules_based(
    intent: str,
    *,
    location: Optional[str],
    max_price: Optional[int],
) -> HuntTranslation:
    """
    Deterministic pattern-matching translator.  No external calls.
    """
    # Keep the original lowercased intent for keyword-based detection
    # (resolution "4k", RAM type "ddr4", vertical keywords, etc.).
    intent_lower_orig = intent.lower()

    # Expand k-suffix numbers for numeric extractions ONLY.
    # We do NOT apply this to the original intent so that "4k" and "8k"
    # are still recognised as resolutions, not converted to 4000/8000.
    intent_lower_num = _expand_k_numbers(intent_lower_orig)

    # 1. Identify vertical
    vertical, v_cfg = _detect_vertical(intent_lower_orig)

    # Pre-compute RAM flag once; reused in several branches below.
    _is_ram = _is_ram_hunt(intent_lower_orig)

    # Early GPU detection: bare AMD model numbers (e.g. "6700xt") score zero
    # against the keyword-based vertical scorer, so they fall through to
    # "general".  Detect the GPU model upfront and override the vertical when
    # a model is found but the scorer missed it.
    _pre_gpu = _extract_gpu_model(intent_lower_orig) if not _is_ram else None
    if _pre_gpu and vertical != "computer_parts":
        vertical = "computer_parts"
        v_cfg    = VERTICALS["computer_parts"]

    # 2. Extract structured attributes
    #    Resolution + vertical keywords → original text
    #    Prices, mileage, capacity       → k-expanded text
    size       = _extract_size(intent_lower_orig)       if v_cfg.get("size_pattern") else None
    resolution = _extract_resolution(intent_lower_orig) if vertical == "tv_home_theater" else None
    tv_brand   = _extract_tv_brand(intent_lower_orig)   if vertical == "tv_home_theater" else None
    tv_panel   = _extract_tv_panel(intent_lower_orig)   if vertical == "tv_home_theater" else None

    ram_type    = _extract_ram_type(intent_lower_orig)  if vertical == "computer_parts" else None
    # Reuse _pre_gpu to avoid calling _extract_gpu_model a second time.
    gpu_model   = _pre_gpu if (vertical == "computer_parts" and not _is_ram) else None
    min_gb      = _extract_min_gb(intent_lower_num)     if vertical == "computer_parts" else None
    min_speed_mhz = (
        _extract_min_speed_mhz(intent_lower_num)
        if vertical == "computer_parts" and _is_ram
        else None
    )
    min_vram_gb = (_extract_min_vram_gb(intent_lower_orig)
                   if vertical == "computer_parts" and not _is_ram
                   else None)

    veh_pair   = _extract_vehicle_make_model(intent_lower_orig) if vertical == "vehicles" else None
    make       = veh_pair[0] if veh_pair else None
    model      = veh_pair[1] if veh_pair else None
    miles      = _extract_miles(intent_lower_num)       if vertical == "vehicles" else None
    min_year, max_year = (
        _extract_year_range(intent_lower_orig) if vertical == "vehicles" else (None, None)
    )

    price = _extract_price(intent_lower_num, max_price)
    loc   = _sanitize_craigslist_location(location)

    # 3. Build search terms, include_keywords, and (for RAM) exclude_keywords
    ram_specific_exclude: list[str] = []
    gpu_model_excl:       list[str] = []   # XTX guard; empty for all non-GPU branches
    tv_require_all:       list[str] = []   # populated by TV branch; empty for all others

    if vertical == "tv_home_theater":
        search_terms, include_kw, tv_require_all = _build_tv_translation(
            size, resolution, brand=tv_brand, panel=tv_panel
        )

    elif vertical == "computer_parts" and _is_ram:
        search_terms, include_kw, ram_specific_exclude = _build_ram_translation(ram_type, min_gb)

    elif vertical == "computer_parts":
        search_terms, include_kw, gpu_model_excl = _build_gpu_translation(gpu_model, intent)

    elif vertical == "vehicles":
        search_terms, include_kw = _build_vehicle_translation(make or "", model or "", intent)

    else:
        clean = _PRICE_RE.sub("", intent_lower_num).strip()
        clean = _MILES_RE.sub("", clean).strip()
        clean = re.sub(r'\b(?:dollars?|bucks?|usd|miles?|km)\b', '', clean, flags=re.IGNORECASE)
        clean = re.sub(r'\s+', ' ', clean).strip()
        search_terms = [clean.title() if clean else intent]
        include_kw   = []

    # 4. Exclude keywords
    if ram_specific_exclude:
        exclude_kw: list[str] = ram_specific_exclude
    else:
        exclude_kw = list(v_cfg.get("default_exclude", [])) + gpu_model_excl

    # 5. Category label
    category = vertical.replace("_", " ")

    # 6. adapter_options: structured constraints forwarded to the rule engine
    adapter_opts: dict = {}

    if tv_require_all:
        # Store multi-discriminator TV constraints for AND-semantics title check.
        # Kept in adapter_options so existing Hunt schema requires no changes.
        adapter_opts["require_all_keywords"] = tv_require_all

    if vertical == "vehicles":
        # Filter out $0/$1 placeholder listings automatically.
        adapter_opts["min_price"] = 200
        if miles:
            adapter_opts["max_miles"] = miles
        if min_year:
            adapter_opts["min_year"] = min_year
        if max_year:
            adapter_opts["max_year"] = max_year

    if vertical == "computer_parts":
        if _is_ram:
            if min_gb:
                adapter_opts["min_capacity_gb"] = min_gb
            if min_speed_mhz:
                adapter_opts["min_speed_mhz"] = min_speed_mhz
        else:
            # GPU hunt: min_vram_gb only set when "vram" / "video memory" explicit.
            if min_vram_gb:
                adapter_opts["min_vram_gb"] = min_vram_gb

    # 7. Auto-generate name
    name = _generate_name(
        vertical, size, resolution, gpu_model, make, model, ram_type, search_terms,
        brand=tv_brand, panel=tv_panel,
    )

    # 8. Notes / reasoning summary
    constraint_parts: list[str] = []
    if tv_brand:   constraint_parts.append(f"brand={tv_brand}")
    if tv_panel:   constraint_parts.append(f"panel={tv_panel}")
    if size:       constraint_parts.append(f'size={size}"')
    if resolution: constraint_parts.append(f"resolution={resolution}")
    if gpu_model:  constraint_parts.append(f"model={gpu_model}")
    if ram_type:   constraint_parts.append(f"ram_type={ram_type}")
    if min_gb and _is_ram:
        constraint_parts.append(f"min_capacity={min_gb}GB")
    if min_speed_mhz and _is_ram:
        constraint_parts.append(f"min_speed={min_speed_mhz}MHz")
    if min_vram_gb:
        constraint_parts.append(f"min_vram={min_vram_gb}GB")
    if make:       constraint_parts.append(f"make={make}")
    if model:      constraint_parts.append(f"model={model}")
    if miles:      constraint_parts.append(f"max_miles={miles:,}")
    if min_year:   constraint_parts.append(f"min_year={min_year}")
    if max_year:   constraint_parts.append(f"max_year={max_year}")
    if price:      constraint_parts.append(f"max_price=${price}")
    if loc:        constraint_parts.append(f"location={loc}")
    if adapter_opts.get("min_price"):
        constraint_parts.append(f"min_price=${adapter_opts['min_price']} (filters placeholder $0/$1 ads)")

    constraints_str = (
        "Constraints: " + ", ".join(constraint_parts)
        if constraint_parts
        else "No structured constraints extracted."
    )
    notes = (
        f"[rules-based] From: \"{intent}\".  "
        f"Vertical: {v_cfg['display_name']}.  "
        f"{constraints_str}"
    )

    return HuntTranslation(
        name             = name,
        vertical         = vertical,
        category         = category,
        source_sites     = list(v_cfg["sources"]),
        search_terms     = search_terms,
        include_keywords = include_kw,
        exclude_keywords = exclude_kw,
        max_price        = price,
        location         = loc,
        radius           = None,
        notes            = notes,
        adapter_options  = adapter_opts,
        translated_by    = _BACKEND_RULES,
    )


# ---------------------------------------------------------------------------
# OpenAI backend — scaffold / stub
# ---------------------------------------------------------------------------

def _translate_openai(
    intent: str,
    *,
    location: Optional[str],
    max_price: Optional[int],
) -> HuntTranslation:
    """
    LLM-backed translator using OpenAI structured output.

    Requirements (when implemented):
      - OPENAI_API_KEY set in the environment
      - 'openai' Python package installed: pip install openai

    To activate: set VULTURE_TRANSLATOR=openai in .env.

    Implementation guidance (TODO):
      1. Build a system prompt that describes HuntTranslation fields and
         their constraints (types, allowed values, non-empty requirements).
      2. Call openai.chat.completions.create() with response_format={"type": "json_object"}.
      3. Parse the JSON response into a HuntTranslation dataclass.
      4. Let _validate_translation() catch any schema deviations.
      5. On API error or parse failure, either raise TranslationError or
         fall back to _translate_rules_based() with a logged warning.
    """
    raise TranslationError(
        "The OpenAI translator backend is not yet implemented.  "
        "Set VULTURE_TRANSLATOR=rules (default) to use the deterministic backend."
    )
