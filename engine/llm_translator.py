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
#
# Each vertical defines:
#   display_name    — shown in notes and Discord replies
#   sources         — default source_sites for this vertical
#   keywords        — tokens whose presence in the intent signals this vertical
#   size_pattern    — whether to try extracting an inch-size measurement
#   default_exclude — noise terms filtered by the rule engine at scan time
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
            "highlander", "rav4",
            # Other vehicle types
            "motorcycle", "scooter", "moped",
            "rv", "motorhome", "camper",
        ],
        "size_pattern": False,
        # Comprehensive exclusions based on live test results:
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
# Note: k-suffix expansion is applied before this regex runs, so plain
# \d patterns are sufficient.
_PRICE_RE = re.compile(
    r'(?:under|below|max(?:imum)?|less\s+than|up\s+to|at\s+most|<)\s*\$?\s*(\d{1,7}(?:,\d{3})*)'
    r'|\$\s*(\d{1,7}(?:,\d{3})*)\s*(?:or\s+(?:less|under|below)|max(?:imum)?)?',
    re.IGNORECASE,
)

# Mileage: "less than 100000 miles", "under 80000 miles", "100000 miles or less"
_MILES_RE = re.compile(
    r'(?:less\s+than|under|below|no\s+more\s+than|at\s+most|<)\s*(\d{1,7}(?:,\d{3})*)\s*miles?'
    r'|(\d{1,7}(?:,\d{3})*)\s*miles?\s*(?:or\s+(?:less|under|below)|max(?:imum)?)?',
    re.IGNORECASE,
)

# Minimum GB: "more than 8gb", "at least 16gb", "minimum 16gb", ">8gb"
_MIN_GB_RE = re.compile(
    r'(?:more\s+than|at\s+least|minimum|min|over|>\s*|>=\s*)\s*(\d+)\s*gb'
    r'|(\d+)\s*gb\s+(?:or\s+more|minimum|min)',
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

# Words that must NOT be treated as a vehicle model name even if they appear
# immediately after a make and pass the isalpha() check.  Common constraint
# words and stopwords that look like model names in naive word-by-word parsing.
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

# Known model → canonical make.  Used so "find me a corolla" resolves
# to "toyota corolla" without the user naming the make explicitly.
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
    Return True when a computer_parts intent is specifically about RAM / memory.
    Used to branch into RAM-specific include/exclude logic instead of the GPU path.
    """
    return any(kw in intent_lower for kw in ["ram", "memory", "ddr4", "ddr5", "ddr3", "dimm"])


def _extract_size(intent_lower: str) -> Optional[int]:
    m = _SIZE_RE.search(intent_lower)
    return int(m.group(1)) if m else None


def _extract_resolution(intent_lower: str) -> Optional[str]:
    for label, aliases in _RESOLUTION_MAP:
        if any(alias in intent_lower for alias in aliases):
            return label
    return None


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

    'less than 100000 miles'  → 100000
    '80000 miles or less'     → 80000

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
    """
    Extract a minimum capacity in GB.

    'more than 8gb'   → 8
    'at least 16gb'   → 16
    '8gb or more'     → 8
    """
    m = _MIN_GB_RE.search(intent_lower)
    if m:
        raw = m.group(1) or m.group(2)
        if raw:
            return int(raw)
    return None


def _extract_gpu_model(intent_lower: str) -> Optional[str]:
    """
    Extract a GPU model identifier like 'RTX 3080', 'RTX 4090 Ti', 'RX 6800 XT'.
    Returns the model in uppercase, e.g. 'RTX 3080 TI'.
    """
    m = re.search(
        r'\b(rtx|gtx)\s*(\d{3,4})\s*(ti|super|xt)?\b'
        r'|\b(rx)\s*(\d{3,4})\s*(xt|xtx)?\b',
        intent_lower,
    )
    if not m:
        return None
    if m.group(1):
        parts = [m.group(1), m.group(2)]
        if m.group(3):
            parts.append(m.group(3))
    else:
        parts = [m.group(4), m.group(5)]
        if m.group(6):
            parts.append(m.group(6))
    return " ".join(parts).upper()


def _extract_vehicle_make_model(intent_lower: str) -> Optional[tuple[str, str]]:
    """
    Return (make, model) for the vehicle detected in the intent.

    Checks:
      1. Explicit make + following word as model ("honda civic")
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
            # Try to get the next word as a model name.
            #
            # Accept: "civic" (alpha), "rav4" (alphanum), "4runner" (alphanum),
            #         "f150" (alphanum), "f-150" (has letter + hyphen)
            # Reject: "2019" (pure digit year), "15k" (number+k), "less" (stopword)
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
    # Check multi-word models first (e.g. "4runner", "cr-v")
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

    # Reject anything containing whitespace
    if re.search(r'\s', raw):
        log.warning(
            "Location %r contains spaces and is not a valid Craigslist subdomain.  "
            "Set location to a single token like 'houston' or leave it blank.  "
            "Ignoring location — runtime will use its default.",
            raw,
        )
        return None

    # Must be reasonable length and all alphanumeric (hyphens allowed for e.g. 'new-york')
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
) -> tuple[list[str], list[str]]:
    """
    TV / Home Theater.

    Strategy:
      - Tight Craigslist phrase ("75 inch 4K TV") for better raw results.
      - include_keywords = [str(size)]: the size digit must appear in the
        listing title ("75" matches "75\"", "75 inch", "75-inch", "75in").
    """
    parts: list[str] = []
    if size:
        parts.append(f"{size} inch")
    if resolution:
        parts.append(resolution.upper())   # "4K", "8K", "1080P"
    parts.append("TV")

    search_terms = [" ".join(parts)]       # e.g. ["75 inch 4K TV"]
    include_kw   = [str(size)] if size else []
    return search_terms, include_kw


def _build_gpu_translation(
    gpu_model: Optional[str],
    intent: str,
) -> tuple[list[str], list[str]]:
    """
    GPU hunt.

    Strategy:
      - Model-specific: search for model directly; require numeric part in title.
      - Generic: use intent text as search phrase.
    """
    if gpu_model:
        search_terms = [f"{gpu_model} GPU"]
        num_m = re.search(r'(\d{3,4})', gpu_model)
        include_kw = [num_m.group(1)] if num_m else []
    else:
        search_terms = [intent]
        include_kw   = []
    return search_terms, include_kw


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
        phrase_parts.append(ram_type.upper())  # "DDR4"
    phrase_parts.extend(["desktop", "RAM"])

    search_terms = [" ".join(phrase_parts)]  # e.g. ["DDR4 desktop RAM"]
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
        Using the full "honda civic" phrase as the include keyword enforces
        that BOTH tokens appear together in the listing title, which filters
        out loose make-only or unrelated model listings.
      - If only make: search and include on the make alone.
      - Fallback: use the cleaned intent as the search phrase.
    """
    if make and model:
        phrase      = f"{make} {model}".title()   # "Honda Civic"
        search_terms = [phrase]
        # Phrase-as-substring check enforces make+model co-occurrence in title
        include_kw   = [f"{make} {model}"]        # "honda civic" — both must appear together
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
) -> str:
    """
    Auto-generate a short, slug-style hunt name.

    Examples
    --------
    TV 75" 4K             → "75in_4k_tv"
    GPU RTX 3080          → "rtx3080_gpu"
    GPU generic           → first 3 search-term words
    DDR4 RAM              → "ddr4_desktop_ram"
    DDR4 RAM 8GB+         → "ddr4_desktop_ram"   (min_gb in notes)
    Vehicle honda civic   → "honda_civic"
    Vehicle honda only    → "honda_car"
    """
    parts: list[str] = []

    if vertical == "tv_home_theater":
        if size:
            parts.append(f"{size}in")
        if resolution:
            parts.append(resolution)
        parts.append("tv")

    elif vertical == "computer_parts":
        if gpu_model:
            parts.append(re.sub(r'\s+', '', gpu_model.lower()))  # "rtx3080ti"
            parts.append("gpu")
        elif ram_type:
            parts.append(ram_type)       # "ddr4"
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
    # "under 10k dollars" → "under 10000 dollars"
    # "less than 100k miles" → "less than 100000 miles"
    # We do NOT apply this to the original intent so that "4k" and "8k"
    # are still recognised as resolutions, not converted to 4000/8000.
    intent_lower_num = _expand_k_numbers(intent_lower_orig)

    # 1. Identify vertical (use original — vertical keywords are not numbers)
    vertical, v_cfg = _detect_vertical(intent_lower_orig)

    # 2. Extract structured attributes
    #    Resolution + vertical keywords → original text
    #    Prices, mileage, capacity       → k-expanded text
    size       = _extract_size(intent_lower_orig)       if v_cfg.get("size_pattern") else None
    resolution = _extract_resolution(intent_lower_orig) if vertical == "tv_home_theater" else None
    ram_type   = _extract_ram_type(intent_lower_orig)   if vertical == "computer_parts" else None
    gpu_model  = (_extract_gpu_model(intent_lower_orig)
                  if vertical == "computer_parts" and not _is_ram_hunt(intent_lower_orig)
                  else None)
    min_gb     = _extract_min_gb(intent_lower_num)      if vertical == "computer_parts" else None

    veh_pair   = _extract_vehicle_make_model(intent_lower_orig) if vertical == "vehicles" else None
    make       = veh_pair[0] if veh_pair else None
    model      = veh_pair[1] if veh_pair else None

    miles      = _extract_miles(intent_lower_num)  if vertical == "vehicles" else None
    price      = _extract_price(intent_lower_num, max_price)

    # Location: validate any override; never extract from free text
    loc = _sanitize_craigslist_location(location)

    # 3. Build search terms, include_keywords, and (for RAM) exclude_keywords
    ram_specific_exclude: list[str] = []

    if vertical == "tv_home_theater":
        search_terms, include_kw = _build_tv_translation(size, resolution)

    elif vertical == "computer_parts" and _is_ram_hunt(intent_lower_orig):
        search_terms, include_kw, ram_specific_exclude = _build_ram_translation(ram_type, min_gb)

    elif vertical == "computer_parts":
        search_terms, include_kw = _build_gpu_translation(gpu_model, intent)

    elif vertical == "vehicles":
        search_terms, include_kw = _build_vehicle_translation(make or "", model or "", intent)

    else:
        # Generic: clean up the k-expanded text so we don't leave "k" or
        # unit-word fragments behind.
        # Example: "road bike under 500 dollars"
        #   _PRICE_RE strips "under 500" → "road bike  dollars"
        #   unit-word strip removes "dollars" → "road bike"
        clean = _PRICE_RE.sub("", intent_lower_num).strip()
        clean = _MILES_RE.sub("", clean).strip()
        # Remove orphaned unit words left after numeric extraction
        clean = re.sub(r'\b(?:dollars?|bucks?|usd|miles?|km)\b', '', clean, flags=re.IGNORECASE)
        clean = re.sub(r'\s+', ' ', clean).strip()
        search_terms = [clean.title() if clean else intent]
        include_kw   = []

    # 4. Exclude keywords: vertical defaults + any vertical-specific overrides
    exclude_kw: list[str] = (
        ram_specific_exclude
        if ram_specific_exclude
        else list(v_cfg.get("default_exclude", []))
    )

    # 5. Category label
    category = vertical.replace("_", " ")

    # 6. adapter_options: carry non-rule structured constraints forward
    adapter_opts: dict = {}

    if vertical == "vehicles":
        # Filter out $0/$1 placeholder listings automatically.
        # A real vehicle listing should cost at least $200.
        adapter_opts["min_price"] = 200
        if miles:
            # Cannot enforce mileage via the current rule engine (mileage isn't
            # in the Listing model).  Store it so the operator can see it in
            # /hunt_show and future versions can enforce it.
            adapter_opts["max_miles"] = miles

    if vertical == "computer_parts" and min_gb:
        # min_gb likewise cannot be enforced by the rule engine today — stored
        # for operator reference only.
        adapter_opts["min_capacity_gb"] = min_gb

    # 7. Auto-generate name
    name = _generate_name(vertical, size, resolution, gpu_model, make, model, ram_type, search_terms)

    # 8. Notes / reasoning summary
    constraint_parts: list[str] = []
    if size:       constraint_parts.append(f'size={size}"')
    if resolution: constraint_parts.append(f"resolution={resolution}")
    if gpu_model:  constraint_parts.append(f"model={gpu_model}")
    if ram_type:   constraint_parts.append(f"ram_type={ram_type}")
    if min_gb:     constraint_parts.append(f"min_capacity={min_gb}GB (stored; not yet enforced by rule engine)")
    if make:       constraint_parts.append(f"make={make}")
    if model:      constraint_parts.append(f"model={model}")
    if miles:      constraint_parts.append(f"max_miles={miles:,} (stored; not yet enforced by rule engine)")
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
