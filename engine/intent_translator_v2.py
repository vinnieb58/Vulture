"""
engine/intent_translator_v2.py

Intent Translator v2 — structured, unit-aware, deterministic pipeline.

Design rule
-----------
An LLM may one day assist with intent classification or entity extraction,
but numeric constraints (price, mileage, year) are ALWAYS extracted by
deterministic Python before any hunt is saved.  Runtime listing filtering
is always deterministic (rules.py), never LLM-driven.

Pipeline
--------
    1. classify_vertical(intent)            -> vertical key (str)
    2. extract_entities(intent, vertical)   -> entity dict  (make/model/year)
    3. extract_constraints(intent, vertical) -> constraint dict (price/miles/year)
    4. build_hunt(intent, vertical, entities, constraints, ...) -> hunt dict
    5. validate_hunt(hunt, vertical)        -> validated hunt dict (or raises)
    6. log_interpreted_hunt(...)            -> None  (structured INFO logs)

Public API
----------
    from engine.intent_translator_v2 import translate_v2
    result: HuntTranslation = translate_v2("toyota sequoia under 50k miles under $30k")

Where max_miles lives
---------------------
    hunt["adapter_options"]["max_miles"]

    The field is read by hunt_service.hunt_to_execution_dict() and forwarded
    to the rules engine.  It is *never* placed in hunt["max_price"].

Vertical name mapping (v2 → internal VERTICALS key)
----------------------------------------------------
    "vehicles"           -> "vehicles"
    "computer_parts"     -> "computer_parts"
    "home_theater"       -> "tv_home_theater"
    "general_marketplace"-> "general"
    "unknown"            -> "general"
"""

from __future__ import annotations

import logging
import re
from typing import Optional

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Forward reference — HuntTranslation is defined in llm_translator to keep
# the public model in one place; we import it lazily inside translate_v2()
# to avoid circular imports at module load.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Vertical registry (v2 names → v1 VERTICALS key)
# ---------------------------------------------------------------------------

_V2_TO_V1: dict[str, str] = {
    "vehicles":            "vehicles",
    "computer_parts":      "computer_parts",
    "home_theater":        "tv_home_theater",
    "general_marketplace": "general",
    "unknown":             "general",
}

# Keyword sets used to score each vertical
_VERTICAL_KEYWORDS: dict[str, list[str]] = {
    "vehicles": [
        "car", "truck", "suv", "van", "pickup", "sedan", "coupe",
        "honda", "toyota", "ford", "chevy", "chevrolet", "nissan",
        "jeep", "dodge", "hyundai", "kia", "subaru", "mazda",
        "bmw", "mercedes", "audi", "volkswagen", "vw",
        "porsche", "lexus", "acura", "infiniti", "volvo",
        "cadillac", "gmc", "buick", "mitsubishi", "chrysler",
        "civic", "accord", "corolla", "camry", "tacoma", "tundra",
        "mustang", "f150", "f-150", "silverado", "4runner",
        "highlander", "rav4", "miata", "mx-5", "prius", "supra",
        "sienna", "venza", "sequoia", "ridgeline", "passport",
        "ranger", "explorer", "expedition", "escape", "edge",
        "maverick", "equinox", "malibu", "traverse",
        "wrx", "crosstrek", "legacy", "rogue", "murano", "armada",
        "sorento", "telluride", "stinger", "palisade", "ioniq",
        "durango", "gladiator", "sierra", "yukon", "acadia", "escalade",
        "motorcycle", "scooter", "moped", "rv", "motorhome", "camper",
        "miles", "mileage", "odometer",
    ],
    "computer_parts": [
        "gpu", "graphics card", "video card",
        "rtx", "gtx", "rx ", "radeon", "nvidia", "geforce",
        "cpu", "processor", "ryzen", "intel core", "xeon",
        "i3 ", "i5 ", "i7 ", "i9 ",
        "ram", "memory", "ddr4", "ddr5", "ddr3", "dimm",
        "ssd", "nvme", "m.2", "hard drive", "hdd",
        "motherboard", "mobo", "psu", "power supply",
        "steam deck", "nintendo switch", "playstation", "xbox",
        "ps5", "ps4", "xbox series",
        "gaming handheld", "handheld console", "portable console",
        "rog ally", "legion go", "playstation portal",
    ],
    "home_theater": [
        "tv", "television", "oled", "qled", "qned",
        "4k", "8k", "uhd", "hdtv", "smart tv",
        "flat screen", "flat panel", "plasma",
    ],
    "general_marketplace": [],
}


# ---------------------------------------------------------------------------
# Vehicle entity constants (shared with v1 for consistency)
# ---------------------------------------------------------------------------

_VEHICLE_MAKES: frozenset[str] = frozenset({
    "honda", "toyota", "ford", "chevy", "chevrolet", "nissan",
    "bmw", "mercedes", "jeep", "dodge", "hyundai", "kia",
    "subaru", "mazda", "volkswagen", "vw", "ram", "gmc",
    "cadillac", "lexus", "acura", "infiniti", "volvo",
    "audi", "porsche", "mitsubishi", "chrysler", "buick",
})

_MODEL_TO_MAKE: dict[str, str] = {
    "civic": "honda", "accord": "honda", "cr-v": "honda", "crv": "honda",
    "pilot": "honda", "odyssey": "honda", "ridgeline": "honda",
    "passport": "honda", "hr-v": "honda", "hrv": "honda",
    "corolla": "toyota", "camry": "toyota", "tacoma": "toyota",
    "tundra": "toyota", "4runner": "toyota", "highlander": "toyota",
    "rav4": "toyota", "prius": "toyota", "supra": "toyota",
    "sienna": "toyota", "venza": "toyota", "sequoia": "toyota",
    "mustang": "ford", "f150": "ford", "f-150": "ford", "bronco": "ford",
    "ranger": "ford", "explorer": "ford", "expedition": "ford",
    "escape": "ford", "edge": "ford", "maverick": "ford",
    "f-250": "ford", "f250": "ford",
    "silverado": "chevrolet", "tahoe": "chevrolet", "suburban": "chevrolet",
    "colorado": "chevrolet", "equinox": "chevrolet", "malibu": "chevrolet",
    "traverse": "chevrolet",
    "altima": "nissan", "maxima": "nissan", "sentra": "nissan",
    "pathfinder": "nissan", "frontier": "nissan",
    "rogue": "nissan", "murano": "nissan", "armada": "nissan",
    "xterra": "nissan",
    "wrangler": "jeep", "cherokee": "jeep", "gladiator": "jeep",
    "grand cherokee": "jeep",
    "challenger": "dodge", "charger": "dodge", "durango": "dodge",
    "ram 1500": "ram", "ram 2500": "ram", "ram 3500": "ram",
    "sonata": "hyundai", "elantra": "hyundai", "tucson": "hyundai",
    "palisade": "hyundai", "ioniq": "hyundai", "santa fe": "hyundai",
    "soul": "kia", "optima": "kia", "sportage": "kia",
    "sorento": "kia", "telluride": "kia", "stinger": "kia",
    "outback": "subaru", "forester": "subaru", "impreza": "subaru",
    "wrx": "subaru", "crosstrek": "subaru", "legacy": "subaru",
    "cx-5": "mazda", "cx5": "mazda", "miata": "mazda", "mx-5": "mazda",
    "golf": "volkswagen", "jetta": "volkswagen", "passat": "volkswagen",
    "sierra": "gmc", "yukon": "gmc", "acadia": "gmc",
    "escalade": "cadillac",
}

_NOT_A_MODEL: frozenset[str] = frozenset({
    "less", "more", "than", "under", "over", "about", "around",
    "with", "without", "and", "or", "for", "the", "a", "an",
    "in", "on", "at", "to", "from", "up", "down", "not",
    "is", "are", "was", "were", "have", "has",
    "dollars", "miles", "mi", "km", "years", "year",
    "old", "new", "used", "clean", "good", "low", "high",
    "very", "just", "only", "all", "find", "me", "want",
})

# Common make misspellings → canonical
_MAKE_ALIASES: dict[str, str] = {
    "hyndai": "hyundai", "hundai": "hyundai", "hyunday": "hyundai",
    "hunday": "hyundai",
    "volkswagon": "volkswagen", "vokswagen": "volkswagen",
    "mitsubichi": "mitsubishi", "mitzubishi": "mitsubishi",
    "suburu": "subaru", "suabru": "subaru",
    "porshe": "porsche", "porche": "porsche",
    "mercades": "mercedes",
    "nisaan": "nissan",
    "accura": "acura",
    "cheverlet": "chevrolet", "chevorlet": "chevrolet",
}

# ---------------------------------------------------------------------------
# Vehicle parts exclusions (required for every vehicle hunt)
# ---------------------------------------------------------------------------

VEHICLE_PARTS_EXCLUDE: list[str] = [
    # Parts listings
    "part out", "partout", "parts out", "parts only", "for parts",
    "parting out", "parts car", "salvage", "parts",
    "OEM",
    # Body / mechanical components
    "roof rack",
    "wheel", "wheels", "rim", "rims",
    "tire", "tires",
    "engine", "transmission",
    "headlight", "taillight",
    "bumper", "fender", "door", "hood",
    # Existing additional exclusions
    "rebuilt title", "flood damage",
    "headlamp", "tail light", "taillamp",
    "tailgate", "tail gate", "liftgate", "lift gate",
    "catalytic converter", "alternator", "radiator",
    "control arm", "cv axle", "cv joint", "brake caliper",
    # Collectibles / clutter
    "sculpture", "figurine", "collectable", "collectible",
    "miniature", "diecast", "die cast", "die-cast",
    "poster", "memorabilia", "keychain", "toy car", "replica",
    # Classified clutter
    "wanted", "looking for", "iso", "will trade",
]

_VEHICLE_PARTS_EXCLUDE_SET: frozenset[str] = frozenset(
    kw.lower() for kw in VEHICLE_PARTS_EXCLUDE
)

# Minimum vehicle-parts exclusions that validate_hunt enforces
_REQUIRED_VEHICLE_EXCL: frozenset[str] = frozenset({
    "part out", "engine", "transmission", "wheel", "tire",
})


# ---------------------------------------------------------------------------
# Numeric helpers
# ---------------------------------------------------------------------------

_K_RE = re.compile(r"\b(\d+(?:\.\d+)?)\s*[kK]\b")


def _expand_k(text: str) -> str:
    """Expand shorthand: '50k' → '50000', '1.5k' → '1500'.

    Applied to the lowercased intent before all numeric pattern matching.
    Does NOT apply to words like 'ok' or 'kayak' (no preceding digit).
    """
    return _K_RE.sub(lambda m: str(int(float(m.group(1)) * 1000)), text)


def _parse_int(raw: str) -> int:
    return int(raw.replace(",", "").replace(".", ""))


def _spans_overlap(span: tuple[int, int], spans: list[tuple[int, int]]) -> bool:
    return any(span[0] < s[1] and span[1] > s[0] for s in spans)


def _intent_has_explicit_mileage_and_price(intent: str) -> bool:
    """
    True when the intent text marks separate mileage and price constraints.

    Used to avoid treating equal numeric values (e.g. 50k miles + 50k dollars)
    as extraction confusion.
    """
    if not intent:
        return False
    lower = intent.lower()
    expanded = _expand_k(lower)
    has_mileage = bool(_MILES_RE.search(expanded))
    has_price = (
        bool(_EXPLICIT_PRICE_RE.search(expanded))
        or "$" in lower
        or bool(re.search(r"\bdollars?\b", lower))
    )
    return has_mileage and has_price


# ---------------------------------------------------------------------------
# Constraint extraction regexes
# ---------------------------------------------------------------------------

# --- Mileage (explicit unit required) ---
# Alt 1: prefix + number + miles/mi
# Alt 2: number + miles/mi (bare, no prefix — captures "X miles or less" etc.)
_MILES_RE = re.compile(
    r"(?:less\s+than|under|below|no\s+more\s+than|at\s+most|<)\s*"
    r"(\d[\d,]*)\s*(?:miles?|mi)\b"
    r"|"
    r"(\d[\d,]*)\s*(?:miles?|mi)\b",
    re.IGNORECASE,
)

# --- Explicit price: $ prefix OR "dollars" suffix OR "under $N" form ---
_EXPLICIT_PRICE_RE = re.compile(
    r"(?:less\s+than|under|below|at\s+most|up\s+to|<)\s*\$\s*(\d[\d,]*)"  # under $30k
    r"|\$\s*(\d[\d,]*)"                                                      # $30k
    r"|(\d[\d,]*)\s*(?:dollars?)\b",                                         # 30k dollars
    re.IGNORECASE,
)

# --- Ambiguous: prefix + number, no unit (vehicle context → price default) ---
# Negative lookahead prevents matching when followed by miles/mi.
_AMBIGUOUS_PRICE_RE = re.compile(
    r"(?:less\s+than|under|below|at\s+most|up\s+to|<)\s*(\d[\d,]*)"
    r"(?!\s*(?:miles?|mi)\b)",
    re.IGNORECASE,
)

# --- Year range ---
_YEAR_MIN_RE = re.compile(
    r"((?:19|20)\d{2})\s+(?:or\s+)?(?:newer|later|above)\b"
    r"|(?:newer|later|more\s+recent)\s+than\s+((?:19|20)\d{2})"
    r"|(?:from|since|after|no\s+older\s+than)\s+((?:19|20)\d{2})",
    re.IGNORECASE,
)
_YEAR_MAX_RE = re.compile(
    r"(?:before|older\s+than|prior\s+to|no\s+newer\s+than)\s+((?:19|20)\d{2})"
    r"|((?:19|20)\d{2})\s+(?:or\s+)?(?:older|earlier)\b",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Step 1: classify_vertical
# ---------------------------------------------------------------------------

# Vehicle keywords that are substrings of common non-vehicle words and must be
# matched on word boundaries only.  Example: "car" matches inside "card only".
_BOUNDARY_VEHICLE_KEYWORDS: frozenset[str] = frozenset({"car", "van"})


def _keyword_in_intent(intent_lower: str, kw: str) -> bool:
    """Return True when kw is present in intent_lower (boundary-safe when needed)."""
    if kw in _BOUNDARY_VEHICLE_KEYWORDS:
        return bool(re.search(r"\b" + re.escape(kw) + r"\b", intent_lower))
    return kw in intent_lower


def classify_vertical(intent: str) -> str:
    """
    Map a natural-language intent to a vertical key.

    Returns one of:
        "vehicles" | "computer_parts" | "home_theater" | "general_marketplace"

    Scoring: each vertical accumulates 1 point per keyword found.
    Short vehicle tokens like "car" use word-boundary matching to avoid false
    positives ("card only" must not score as a vehicle).

    Strong GPU signals (rtx/gtx/gpu) short-circuit to computer_parts so they
    are never routed to translate_v2() when vehicle keywords tie on score.
    """
    intent_lower = intent.lower()

    # Strong GPU signals — route to v1 computer_parts path, not vehicle v2.
    if re.search(r"\b(rtx|gtx)\b", intent_lower) or re.search(
        r"\b(gpu|graphics card|video card)\b", intent_lower
    ):
        return "computer_parts"

    best_key = "general_marketplace"
    best_score = 0

    for v2_key, keywords in _VERTICAL_KEYWORDS.items():
        if not keywords:
            continue
        score = sum(1 for kw in keywords if _keyword_in_intent(intent_lower, kw))
        if score > best_score:
            best_score = score
            best_key = v2_key

    log.debug("classify_vertical: %r → %s (score=%d)", intent[:60], best_key, best_score)
    return best_key


# ---------------------------------------------------------------------------
# Step 2: extract_entities
# ---------------------------------------------------------------------------

def _normalize_makes(text: str) -> str:
    """Correct common make misspellings in lowercased text."""
    for misspelled, canonical in _MAKE_ALIASES.items():
        if misspelled in text:
            text = re.sub(r"\b" + re.escape(misspelled) + r"\b", canonical, text)
    return text


def _extract_vehicle_make_model(intent_lower: str) -> tuple[str, str]:
    """
    Return (make, model).  Model is '' when only a make was found.

    Priority:
      1. Explicit make followed by a model word
      2. Known model name → inferred make
      3. Make alone
    """
    words = intent_lower.split()

    for i, word in enumerate(words):
        if word in _VEHICLE_MAKES:
            make = word
            nxt = words[i + 1] if i + 1 < len(words) else ""
            nxt_has_letter = bool(re.search(r"[a-z]", nxt))
            nxt_is_numeric = bool(re.match(r"^\d+[kK]?$", nxt))
            if (
                nxt_has_letter
                and not nxt_is_numeric
                and nxt not in _VEHICLE_MAKES
                and nxt not in _NOT_A_MODEL
                and len(nxt) > 1
            ):
                return make, nxt
            return make, ""

    for model, make in sorted(_MODEL_TO_MAKE.items(), key=lambda kv: -len(kv[0])):
        if model in intent_lower:
            return make, model

    return "", ""


def extract_entities(intent: str, vertical: str) -> dict:
    """
    Extract named entities for the given vertical.

    For "vehicles":
        make, model, trim (best-effort), min_year, max_year

    Other verticals return an empty dict (entity extraction is handled
    by the legacy v1 helpers wired in build_hunt).

    Returns
    -------
    dict — may be empty; all keys are optional.
    """
    entities: dict = {}

    if vertical != "vehicles":
        return entities

    intent_lower = _normalize_makes(intent.lower())

    make, model = _extract_vehicle_make_model(intent_lower)
    if make:
        entities["make"] = make
    if model:
        entities["model"] = model

    # Year range (use original intent for year detection — no k-expansion needed)
    def _valid_year(yr: int) -> Optional[int]:
        return yr if 1960 <= yr <= 2030 else None

    m = _YEAR_MIN_RE.search(intent_lower)
    if m:
        raw = m.group(1) or m.group(2) or m.group(3)
        yr = _valid_year(int(raw)) if raw else None
        if yr:
            entities["min_year"] = yr

    m = _YEAR_MAX_RE.search(intent_lower)
    if m:
        raw = m.group(1) or m.group(2)
        yr = _valid_year(int(raw)) if raw else None
        if yr:
            entities["max_year"] = yr

    return entities


# ---------------------------------------------------------------------------
# Step 3: extract_constraints
# ---------------------------------------------------------------------------

def extract_constraints(intent: str, vertical: str) -> dict:
    """
    Deterministic, unit-aware constraint extraction.

    For "vehicles" the three-pass algorithm guarantees:
      - Numbers followed by miles/mi are always mileage, never price.
      - Numbers prefixed with $ or followed by "dollars" are always price.
      - Bare "under N" in vehicle context defaults to price.

    For all verticals, max_price is extracted from the same passes 2 & 3
    (pass 1 is vehicles-only).

    Returns
    -------
    dict with a subset of:
        max_price   : int   — price ceiling
        max_miles   : int   — mileage ceiling (vehicles only)
        min_year    : int   — model-year lower bound (vehicles only)
        max_year    : int   — model-year upper bound (vehicles only)
    """
    intent_lower = intent.lower()
    text = _expand_k(intent_lower)
    result: dict = {}
    mileage_spans: list[tuple[int, int]] = []

    # --- Pass 1: Mileage (vehicles only) — explicit unit required --------
    if vertical == "vehicles":
        for m in _MILES_RE.finditer(text):
            raw = m.group(1) or m.group(2)
            if raw:
                val = _parse_int(raw)
                # Accept first found mileage; if multiple, keep the smallest
                if "max_miles" not in result or val < result["max_miles"]:
                    result["max_miles"] = val
                mileage_spans.append(m.span())

    # --- Pass 2: Explicit price ($N or N dollars) -------------------------
    for m in _EXPLICIT_PRICE_RE.finditer(text):
        if _spans_overlap(m.span(), mileage_spans):
            continue
        raw = m.group(1) or m.group(2) or m.group(3)
        if raw:
            result["max_price"] = _parse_int(raw)
            break

    # --- Pass 3: Ambiguous prefix ("under N") → price default -------------
    if "max_price" not in result:
        for m in _AMBIGUOUS_PRICE_RE.finditer(text):
            if _spans_overlap(m.span(), mileage_spans):
                continue
            raw = m.group(1)
            if raw:
                result["max_price"] = _parse_int(raw)
                break

    # --- Year range (vehicles only) ----------------------------------------
    if vertical == "vehicles":
        # Year patterns work on the non-k-expanded text (years are 4-digit, no k)
        def _valid_year(yr: int) -> Optional[int]:
            return yr if 1960 <= yr <= 2030 else None

        m = _YEAR_MIN_RE.search(intent_lower)
        if m:
            raw = m.group(1) or m.group(2) or m.group(3)
            yr = _valid_year(int(raw)) if raw else None
            if yr:
                result["min_year"] = yr

        m = _YEAR_MAX_RE.search(intent_lower)
        if m:
            raw = m.group(1) or m.group(2)
            yr = _valid_year(int(raw)) if raw else None
            if yr:
                result["max_year"] = yr

    return result


# ---------------------------------------------------------------------------
# Step 4: build_hunt
# ---------------------------------------------------------------------------

def _sanitize_location(raw: Optional[str]) -> Optional[str]:
    if not raw:
        return None
    raw = raw.strip()
    if not raw:
        return None
    if re.search(r"\s", raw):
        log.warning(
            "Location %r contains spaces — not a valid Craigslist subdomain. Ignoring.",
            raw,
        )
        return None
    if len(raw) > 30 or not re.match(r"^[a-z0-9-]+$", raw, re.IGNORECASE):
        log.warning("Location %r is not a valid subdomain. Ignoring.", raw)
        return None
    return raw.lower()


def build_hunt(
    intent: str,
    vertical: str,
    entities: dict,
    constraints: dict,
    *,
    location: Optional[str] = None,
    max_price_override: Optional[int] = None,
) -> dict:
    """
    Assemble the final hunt dict from classified vertical, extracted entities,
    and validated constraints.

    The returned dict maps 1-to-1 to HuntTranslation fields:
        category, vertical_key, source_sites, search_terms,
        include_keywords, exclude_keywords, max_price,
        location, radius, adapter_options, name, notes

    max_miles is placed in adapter_options["max_miles"] — never in max_price.
    """
    # Import VERTICALS lazily to avoid circular import (this module is imported
    # by llm_translator which defines VERTICALS).
    from engine.llm_translator import VERTICALS  # noqa: PLC0415
    from engine.source_selection import resolve_source_sites

    v1_key = _V2_TO_V1.get(vertical, "general")
    v_cfg = VERTICALS.get(v1_key, VERTICALS["general"])

    make = entities.get("make", "")
    model = entities.get("model", "")

    # --- Search terms & include keywords ---
    if vertical == "vehicles":
        if make and model:
            phrase = f"{make} {model}".title()
            search_terms = [phrase]
            include_kw = [f"{make} {model}"]
        elif make:
            search_terms = [make.title()]
            include_kw = [make]
        else:
            search_terms = [intent]
            include_kw = []
    else:
        # For non-vehicle verticals the legacy helper is used by
        # llm_translator._translate_rules_based; here we provide a simple
        # fallback so build_hunt is usable standalone.
        search_terms = [intent]
        include_kw = []

    # --- Exclude keywords ---
    if vertical == "vehicles":
        exclude_kw = list(VEHICLE_PARTS_EXCLUDE)
    else:
        exclude_kw = list(v_cfg.get("default_exclude", []))

    # --- max_price (override wins) ---
    max_price: Optional[int] = max_price_override if max_price_override is not None else constraints.get("max_price")

    # --- adapter_options ---
    adapter_opts: dict = {}
    if vertical == "vehicles":
        adapter_opts["min_price"] = 200  # filter $0/$1 placeholder listings
        if "max_miles" in constraints:
            adapter_opts["max_miles"] = constraints["max_miles"]
        if "min_year" in constraints:
            adapter_opts["min_year"] = constraints["min_year"]
        if "max_year" in constraints:
            adapter_opts["max_year"] = constraints["max_year"]
        # Also carry over year from entities (extract_entities may have found them
        # even if extract_constraints didn't reach them — they're now de-duped)
        if "min_year" in entities and "min_year" not in adapter_opts:
            adapter_opts["min_year"] = entities["min_year"]
        if "max_year" in entities and "max_year" not in adapter_opts:
            adapter_opts["max_year"] = entities["max_year"]

    # --- Hunt name (slug) ---
    if vertical == "vehicles" and make and model:
        raw_name = f"{make}_{model}"
    elif vertical == "vehicles" and make:
        raw_name = f"{make}_car"
    else:
        words = [
            w for w in search_terms[0].lower().split()
            if w not in {"a", "an", "the", "for", "in", "on", "at", "to", "of",
                         "and", "or", "is", "are", "with", "under", "over"}
        ]
        raw_name = "_".join(words[:3])

    name = re.sub(r"[^a-z0-9_]", "", raw_name) or "hunt"

    # --- Location ---
    loc = _sanitize_location(location)

    # --- Category label ---
    category = v1_key.replace("_", " ")

    # --- Notes ---
    parts = [f'[v2] From: "{intent}"', f"Vertical: {v_cfg['display_name']}"]
    if make:
        parts.append(f"make={make}")
    if model:
        parts.append(f"model={model}")
    if max_price is not None:
        parts.append(f"max_price=${max_price}")
    if "max_miles" in adapter_opts:
        parts.append(f"max_miles={adapter_opts['max_miles']:,}")
    if "min_year" in adapter_opts:
        parts.append(f"min_year={adapter_opts['min_year']}")
    if "max_year" in adapter_opts:
        parts.append(f"max_year={adapter_opts['max_year']}")
    if loc:
        parts.append(f"location={loc}")
    notes = ".  ".join(parts)

    return {
        "name":             name,
        "vertical":         vertical,        # v2 name
        "vertical_key":     v1_key,          # VERTICALS dict key
        "category":         category,
        "source_sites":     resolve_source_sites(v1_key),
        "search_terms":     search_terms,
        "include_keywords": include_kw,
        "exclude_keywords": exclude_kw,
        "max_price":        max_price,
        "location":         loc,
        "radius":           None,
        "adapter_options":  adapter_opts,
        "notes":            notes,
        # Snapshot for logging / validation
        "_entities":        entities,
        "_constraints":     constraints,
        "_intent":          intent,
    }


# ---------------------------------------------------------------------------
# Step 5: validate_hunt
# ---------------------------------------------------------------------------

def validate_hunt(hunt: dict, vertical: str) -> dict:
    """
    Deterministic post-build validation.

    Checks
    ------
    1. max_price must not equal max_miles (likely unit confusion).
       Conservative correction: null out max_price and log a warning.
    2. max_price implausibly large for a vehicle (> $500k suggests mileage
       leaked into price).  Conservative correction: null out max_price.
    3. Vehicle hunts must include the required parts/accessory exclusions.
    4. max_miles lives in adapter_options, never in max_price.

    Raises
    ------
    TranslationError — on hard failures (missing required exclusions).
    Logs warnings    — on suspicious values that are corrected in place.
    """
    from engine.llm_translator import TranslationError  # noqa: PLC0415

    max_price = hunt.get("max_price")
    adapter_opts = hunt.get("adapter_options", {})
    max_miles = adapter_opts.get("max_miles")
    errors: list[str] = []

    # Check 1: equal values only when units are ambiguous (no explicit $/miles/dollars)
    intent_text = hunt.get("_intent", "")
    if (
        max_price is not None
        and max_miles is not None
        and max_price == max_miles
        and not _intent_has_explicit_mileage_and_price(intent_text)
    ):
        log.warning(
            "validate_hunt: max_price (%s) equals max_miles (%s) with no distinct "
            "unit markers — likely unit confusion.  Nulling max_price.  Intent: %r",
            max_price, max_miles, intent_text,
        )
        hunt["max_price"] = None
        max_price = None

    # Check 2: max_price unreasonably high for a vehicle purchase
    if vertical == "vehicles" and max_price is not None and max_price > 500_000:
        log.warning(
            "validate_hunt: max_price=%s is implausibly high for a vehicle — "
            "possible mileage value in price field.  Nulling conservatively.  "
            "Intent: %r",
            max_price, hunt.get("_intent", ""),
        )
        hunt["max_price"] = None
        max_price = None

    # Check 3: vehicle hunts must carry required parts exclusions
    if vertical == "vehicles":
        excl_lower = {kw.lower() for kw in hunt.get("exclude_keywords", [])}
        missing = _REQUIRED_VEHICLE_EXCL - excl_lower
        if missing:
            errors.append(
                f"Vehicle hunt is missing required parts exclusions: {sorted(missing)}"
            )

    # Check 4: equal price/miles forbidden only when intent lacks distinct units
    if (
        vertical == "vehicles"
        and max_miles is not None
        and hunt.get("max_price") == max_miles
        and not _intent_has_explicit_mileage_and_price(intent_text)
    ):
        errors.append(
            f"max_price ({hunt.get('max_price')}) equals max_miles — "
            "mileage was placed in the price field, which is forbidden."
        )

    if errors:
        raise TranslationError(
            "Intent Translator v2 validation failed — hunt was NOT saved.  "
            "Issues: " + "; ".join(errors)
        )

    return hunt


# ---------------------------------------------------------------------------
# Step 6: log_interpreted_hunt
# ---------------------------------------------------------------------------

def log_interpreted_hunt(
    intent: str,
    hunt: dict,
    entities: dict,
    constraints: dict,
    vertical: str,
) -> None:
    """
    Emit a structured INFO log block showing every decision made.

    Output format
    -------------
    [hunt-create] Raw intent    : "..."
                  Vertical      : vehicles
                  Source sites  : craigslist
                  Search terms  : Toyota Sequoia
                  Include kw    : toyota sequoia
                  Exclude kw    : part out, engine, ...
                  Max price     : $30,000
                  Max miles     : 50,000  (adapter_options["max_miles"])
                  Make/model    : toyota / sequoia
                  Min year      : 2018
                  Max year      : —
                  Adapter opts  : {"min_price": 200, "max_miles": 50000}
    """
    ao = hunt.get("adapter_options", {})
    make = entities.get("make") or "—"
    model = entities.get("model") or "—"
    min_year = ao.get("min_year") or entities.get("min_year") or "—"
    max_year = ao.get("max_year") or entities.get("max_year") or "—"
    max_miles = ao.get("max_miles")
    max_price = hunt.get("max_price")

    miles_str = f"{max_miles:,}" if max_miles is not None else "—"
    price_str = f"${max_price:,}" if max_price is not None else "—"

    sites_str = ", ".join(hunt.get("source_sites", []))
    terms_str = ", ".join(hunt.get("search_terms", []))
    incl_str = ", ".join(hunt.get("include_keywords", [])) or "—"
    excl_preview = hunt.get("exclude_keywords", [])[:8]
    excl_str = ", ".join(excl_preview) + (
        f", ... (+{len(hunt.get('exclude_keywords', [])) - 8} more)"
        if len(hunt.get("exclude_keywords", [])) > 8 else ""
    )

    log.info(
        "[hunt-create]\n"
        "  Raw intent   : %r\n"
        "  Vertical     : %s\n"
        "  Source sites : %s\n"
        "  Search terms : %s\n"
        "  Include kw   : %s\n"
        "  Exclude kw   : %s\n"
        "  Max price    : %s\n"
        "  Max miles    : %s  (adapter_options['max_miles'])\n"
        "  Make / model : %s / %s\n"
        "  Min year     : %s\n"
        "  Max year     : %s\n"
        "  Adapter opts : %s",
        intent,
        vertical,
        sites_str,
        terms_str,
        incl_str,
        excl_str,
        price_str,
        miles_str,
        make,
        model,
        min_year,
        max_year,
        ao,
    )


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def translate_v2(
    intent: str,
    *,
    location: Optional[str] = None,
    max_price: Optional[int] = None,
) -> "HuntTranslation":  # type: ignore[name-defined]
    """
    Translate a natural-language hunt intent using the v2 pipeline.

    Parameters
    ----------
    intent    : The user's search description.
    location  : Optional Craigslist subdomain override (single token).
    max_price : Optional price ceiling override (takes priority over extracted price).

    Returns
    -------
    HuntTranslation — same type as llm_translator.translate(), fully validated.

    Raises
    ------
    TranslationError — on empty intent, validation failure, or impossible values.
    """
    from engine.llm_translator import HuntTranslation, TranslationError, _BACKEND_RULES  # noqa: PLC0415

    intent = (intent or "").strip()
    if not intent:
        raise TranslationError("Intent must not be empty")

    log.info("[v2] Translating: %r", intent)

    # Step 1 — classify
    vertical = classify_vertical(intent)

    # Step 2 — entities
    entities = extract_entities(intent, vertical)

    # Step 3 — constraints
    constraints = extract_constraints(intent, vertical)

    # Step 4 — build
    hunt = build_hunt(
        intent,
        vertical,
        entities,
        constraints,
        location=location,
        max_price_override=max_price,
    )

    # Step 5 — validate
    hunt = validate_hunt(hunt, vertical)

    # Step 6 — log
    log_interpreted_hunt(intent, hunt, entities, constraints, vertical)

    # Map to HuntTranslation
    return HuntTranslation(
        name             = hunt["name"],
        vertical         = hunt["vertical_key"],
        category         = hunt["category"],
        source_sites     = hunt["source_sites"],
        search_terms     = hunt["search_terms"],
        include_keywords = hunt["include_keywords"],
        exclude_keywords = hunt["exclude_keywords"],
        max_price        = hunt["max_price"],
        location         = hunt["location"],
        radius           = hunt["radius"],
        notes            = hunt["notes"],
        adapter_options  = hunt["adapter_options"],
        translated_by    = "rules-v2",
    )
