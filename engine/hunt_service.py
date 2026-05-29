"""
engine/hunt_service.py

Business-logic layer for Hunt lifecycle management.

Responsibilities:
- Input validation and normalization
- Status transition enforcement
- Timestamp management
- Delegation to hunt_repository for persistence

This layer does not touch the database directly; all persistence goes through
hunt_repository. It does not contain Discord or LLM logic.

Valid status values: "active" | "paused" | "ended"
  active  — hunt runs on each scheduled cycle
  paused  — hunt is temporarily skipped but retains its history
  ended   — hunt is permanently disabled; editing is blocked
"""

import logging
from typing import Optional

import engine.hunt_repository as repo
from models.hunt import Hunt, _now_iso

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Valid statuses
# ---------------------------------------------------------------------------

VALID_STATUSES = frozenset({"active", "paused", "ended"})

# Status transitions allowed by the service.
# Keys are current status; values are the statuses reachable from it.
_ALLOWED_TRANSITIONS: dict[str, frozenset] = {
    "active": frozenset({"paused", "ended"}),
    "paused": frozenset({"active", "ended"}),
    "ended":  frozenset(),  # terminal — no transitions out
}


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class HuntNotFoundError(Exception):
    """Raised when a hunt_id does not match any stored hunt."""


class HuntValidationError(ValueError):
    """Raised when required fields are missing or invalid."""


class HuntStateError(Exception):
    """Raised when a requested status transition is not allowed."""


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _normalize_keywords(keywords: Optional[list]) -> list:
    """
    Coerce each element to str, strip whitespace, and drop empty strings.
    Order is preserved; duplicates are kept (dedup is the caller's concern).
    """
    if not keywords:
        return []
    return [s for s in (str(kw).strip() for kw in keywords) if s]


def _require_hunt(hunt_id: str) -> Hunt:
    """Fetch a hunt by ID or raise HuntNotFoundError."""
    hunt = repo.get_hunt_by_id(hunt_id)
    if hunt is None:
        raise HuntNotFoundError(f"Hunt not found: {hunt_id}")
    return hunt


def _is_vehicle_hunt(hunt: Hunt) -> bool:
    category = (hunt.category or "").lower().replace(" ", "_")
    return category == "vehicles" or "vehicle" in category


def _resolve_vehicle_make_model(hunt: Hunt) -> tuple[Optional[str], Optional[str]]:
    """Return (make, model) for vehicle title matching at runtime."""
    ao = hunt.adapter_options or {}
    make = ao.get("make")
    model = ao.get("model")
    if make and model:
        return str(make).lower(), str(model).lower()
    for kw in hunt.include_keywords or []:
        tokens = str(kw).lower().split()
        if len(tokens) >= 2:
            return tokens[0], tokens[1]
    name_parts = (hunt.name or "").lower().replace("-", "_").split("_")
    if len(name_parts) >= 2 and name_parts[0] and name_parts[1]:
        return name_parts[0], name_parts[1]
    if make:
        return str(make).lower(), str(model).lower() if model else None
    if model:
        return None, str(model).lower()
    return None, None


def _apply_transition(hunt: Hunt, target_status: str) -> Hunt:
    """
    Validate and apply a status transition.

    Raises HuntStateError if the transition is not permitted or if the hunt
    is already in the target state.
    Returns the updated Hunt.
    """
    if hunt.status == target_status:
        raise HuntStateError(
            f"Hunt '{hunt.name}' is already {target_status}"
        )
    allowed = _ALLOWED_TRANSITIONS.get(hunt.status, frozenset())
    if target_status not in allowed:
        raise HuntStateError(
            f"Cannot transition hunt '{hunt.name}' "
            f"from '{hunt.status}' to '{target_status}'"
        )
    success = repo.update_hunt_status(hunt.hunt_id, target_status)
    if not success:
        raise HuntNotFoundError(f"Hunt not found during status update: {hunt.hunt_id}")
    hunt.status = target_status
    hunt.updated_at = _now_iso()
    log.info("Hunt '%s' (%s): %s -> %s", hunt.name, hunt.hunt_id, hunt.status, target_status)
    return hunt


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def create_hunt(
    name: str,
    search_terms: list,
    source_sites: list,
    *,
    category: Optional[str] = None,
    include_keywords: Optional[list] = None,
    exclude_keywords: Optional[list] = None,
    max_price: Optional[int] = None,
    location: Optional[str] = None,
    radius: Optional[int] = None,
    created_by: Optional[str] = None,
    notes: Optional[str] = None,
    adapter_options: Optional[dict] = None,
) -> Hunt:
    """
    Validate inputs, build a Hunt, and persist it.

    Required:
      name         — non-empty string
      search_terms — at least one non-empty term
      source_sites — at least one non-empty site name

    Returns the persisted Hunt.
    Raises HuntValidationError on invalid input.
    """
    # --- validate required fields ---
    name = (name or "").strip()
    if not name:
        raise HuntValidationError("'name' is required and must not be empty")

    clean_terms = _normalize_keywords(search_terms)
    if not clean_terms:
        raise HuntValidationError("'search_terms' must contain at least one non-empty term")

    clean_sites = _normalize_keywords(source_sites)
    if not clean_sites:
        raise HuntValidationError("'source_sites' must contain at least one non-empty site name")

    # --- validate max_price ---
    if max_price is not None and max_price < 0:
        raise HuntValidationError("'max_price' must be a non-negative integer")

    # --- build the Hunt ---
    now = _now_iso()
    hunt = Hunt(
        name=name,
        category=(category or "").strip() or None,
        source_sites=clean_sites,
        search_terms=clean_terms,
        include_keywords=_normalize_keywords(include_keywords),
        exclude_keywords=_normalize_keywords(exclude_keywords),
        max_price=max_price,
        location=(location or "").strip() or None,
        radius=radius,
        status="active",
        created_by=(created_by or "").strip() or None,
        created_at=now,
        updated_at=now,
        notes=(notes or "").strip() or None,
        adapter_options=adapter_options or {},
    )

    repo.create_hunt(hunt)
    log.info("Created hunt '%s' (%s)", hunt.name, hunt.hunt_id)
    return hunt


def get_hunt(hunt_id: str) -> Hunt:
    """
    Return a Hunt by ID.
    Raises HuntNotFoundError if not found.
    """
    return _require_hunt(hunt_id)


def list_hunts(status: Optional[str] = None) -> list[Hunt]:
    """
    Return all hunts, optionally filtered by status.

    status=None returns every hunt regardless of status.
    Raises HuntValidationError if an unrecognised status is requested.
    """
    if status is not None and status not in VALID_STATUSES:
        raise HuntValidationError(
            f"Unknown status '{status}'. Valid values: {sorted(VALID_STATUSES)}"
        )
    return repo.list_hunts(status=status)


def end_hunt(hunt_id: str) -> Hunt:
    """
    Permanently disable a hunt.

    Allowed from: active, paused
    Terminal state — cannot be undone through the service.
    Raises HuntNotFoundError, HuntStateError.
    """
    hunt = _require_hunt(hunt_id)
    return _apply_transition(hunt, "ended")


def pause_hunt(hunt_id: str) -> Hunt:
    """
    Temporarily disable a hunt.

    Allowed from: active
    Raises HuntNotFoundError, HuntStateError.
    """
    hunt = _require_hunt(hunt_id)
    return _apply_transition(hunt, "paused")


def resume_hunt(hunt_id: str) -> Hunt:
    """
    Re-enable a paused hunt.

    Allowed from: paused
    Raises HuntNotFoundError, HuntStateError.
    """
    hunt = _require_hunt(hunt_id)
    return _apply_transition(hunt, "active")


def edit_hunt(
    hunt_id: str,
    *,
    name: Optional[str] = None,
    category: Optional[str] = None,
    search_terms: Optional[list] = None,
    source_sites: Optional[list] = None,
    include_keywords: Optional[list] = None,
    exclude_keywords: Optional[list] = None,
    max_price: Optional[int] = None,
    location: Optional[str] = None,
    radius: Optional[int] = None,
    notes: Optional[str] = None,
    adapter_options: Optional[dict] = None,
) -> Hunt:
    """
    Update one or more mutable fields on an existing hunt.

    Only fields explicitly passed (not None) are applied; others are unchanged.
    Editing an ended hunt raises HuntStateError — create a new hunt instead.

    Raises HuntNotFoundError, HuntStateError, HuntValidationError.
    """
    hunt = _require_hunt(hunt_id)

    if hunt.status == "ended":
        raise HuntStateError(
            f"Hunt '{hunt.name}' has ended and cannot be edited. "
            "Create a new hunt instead."
        )

    # --- apply each provided field ---
    if name is not None:
        name = name.strip()
        if not name:
            raise HuntValidationError("'name' must not be empty")
        hunt.name = name

    if category is not None:
        hunt.category = category.strip() or None

    if search_terms is not None:
        clean = _normalize_keywords(search_terms)
        if not clean:
            raise HuntValidationError("'search_terms' must contain at least one non-empty term")
        hunt.search_terms = clean

    if source_sites is not None:
        clean = _normalize_keywords(source_sites)
        if not clean:
            raise HuntValidationError("'source_sites' must contain at least one non-empty site name")
        hunt.source_sites = clean

    if include_keywords is not None:
        hunt.include_keywords = _normalize_keywords(include_keywords)

    if exclude_keywords is not None:
        hunt.exclude_keywords = _normalize_keywords(exclude_keywords)

    if max_price is not None:
        if max_price < 0:
            raise HuntValidationError("'max_price' must be a non-negative integer")
        hunt.max_price = max_price

    if location is not None:
        hunt.location = location.strip() or None

    if radius is not None:
        hunt.radius = radius

    if notes is not None:
        hunt.notes = notes.strip() or None

    if adapter_options is not None:
        hunt.adapter_options = adapter_options

    success = repo.update_hunt(hunt)
    if not success:
        raise HuntNotFoundError(f"Hunt not found during update: {hunt_id}")

    log.info("Edited hunt '%s' (%s)", hunt.name, hunt.hunt_id)
    return hunt


# ---------------------------------------------------------------------------
# v1.0 execution engine compatibility shim
# ---------------------------------------------------------------------------

def hunt_to_execution_dict(hunt: Hunt) -> dict:
    """
    Convert a v2.0 Hunt into the dict shape expected by main.py's run_hunt().

    The returned dict contains:
      {
        "hunt_id":     str,        # DB hunt UUID (for log traceability)
        "name":        str,
        "source_sites": list[str], # all configured sources; used by
                                   # _expand_hunt_sources() in main.py
        "source":      str,        # source_sites[0] — kept for YAML-consumer compat
        "query":       str,        # search_terms joined by space
        "city":        str,        # location, or "houston" fallback
        "limit":       int,        # adapter_options["limit"] or 10
        "rules": {
          "min_price":        Optional[int],   # filters placeholder $0/$1 listings
          "max_price":        Optional[int],
          "include_keywords": list,
          "exclude_keywords": list,
          ...                                  # structured constraints from adapter_options
        }
      }

    Multi-source execution is handled in main.py by _expand_hunt_sources(),
    which fans out the source_sites list into one execution dict per source.
    This function's responsibility is only to build the shared dict; it does
    not loop over sources.

    Notes:
    - Rules dict omits keys whose values are empty/None so the rule engine's
      existing early-exit logic (if not rules: return True) still works cleanly.
    - min_price is read from adapter_options (set by the translator for vehicles
      to filter placeholder $0/$1 ads).
    - Raises HuntValidationError if source_sites or search_terms are empty,
      since run_hunt() cannot function without them.
    """
    if not hunt.source_sites:
        raise HuntValidationError(
            f"Hunt '{hunt.name}' has no source_sites and cannot be executed"
        )
    if not hunt.search_terms:
        raise HuntValidationError(
            f"Hunt '{hunt.name}' has no search_terms and cannot be executed"
        )

    rules: dict = {}

    # Vertical label — metadata forwarded to rules.py for contextual log messages.
    # Derived from category (which persists the vertical display name) or from
    # adapter_options if explicitly stored there.
    _vertical = (hunt.category or "").replace(" ", "_").strip("_")
    if _vertical:
        rules["vertical"] = _vertical

    # min_price from adapter_options — translator sets this for verticals where
    # placeholder/junk listings are common (e.g. vehicles with $0 / $1 price).
    min_price = hunt.adapter_options.get("min_price")
    if min_price is not None:
        rules["min_price"] = int(min_price)
    if hunt.max_price is not None:
        rules["max_price"] = hunt.max_price
    if hunt.include_keywords:
        rules["include_keywords"] = hunt.include_keywords
    if hunt.exclude_keywords:
        rules["exclude_keywords"] = hunt.exclude_keywords

    # Structured constraints stored by the translator — enforced by rules.py
    # via title-text extraction (conservative: missing value = allow through).
    max_miles = hunt.adapter_options.get("max_miles")
    if max_miles is not None:
        rules["max_miles"] = int(max_miles)
    min_capacity_gb = hunt.adapter_options.get("min_capacity_gb")
    if min_capacity_gb is not None:
        rules["min_capacity_gb"] = int(min_capacity_gb)
    min_year = hunt.adapter_options.get("min_year")
    if min_year is not None:
        rules["min_year"] = int(min_year)
    max_year = hunt.adapter_options.get("max_year")
    if max_year is not None:
        rules["max_year"] = int(max_year)
    min_vram_gb = hunt.adapter_options.get("min_vram_gb")
    if min_vram_gb is not None:
        rules["min_vram_gb"] = int(min_vram_gb)
    min_speed_mhz = hunt.adapter_options.get("min_speed_mhz")
    if min_speed_mhz is not None:
        rules["min_speed_mhz"] = int(min_speed_mhz)
    require_all_keywords = hunt.adapter_options.get("require_all_keywords")
    if require_all_keywords:
        rules["require_all_keywords"] = list(require_all_keywords)

    # TV: structured size constraint (more precise than a bare-number substring).
    min_size_inches = hunt.adapter_options.get("min_size_inches")
    if min_size_inches is not None:
        rules["min_size_inches"] = int(min_size_inches)
    max_size_inches = hunt.adapter_options.get("max_size_inches")
    if max_size_inches is not None:
        rules["max_size_inches"] = int(max_size_inches)

    # GPU: tier-based minimum class ("or better" hunts).
    min_gpu_class = hunt.adapter_options.get("min_gpu_class")
    if min_gpu_class:
        rules["min_gpu_class"] = str(min_gpu_class)

    vehicle_make = hunt.adapter_options.get("make")
    vehicle_model = hunt.adapter_options.get("model")
    if _is_vehicle_hunt(hunt) and (not vehicle_make or not vehicle_model):
        inferred_make, inferred_model = _resolve_vehicle_make_model(hunt)
        vehicle_make = vehicle_make or inferred_make
        vehicle_model = vehicle_model or inferred_model
    if vehicle_make:
        rules["vehicle_make"] = str(vehicle_make)
    if vehicle_model:
        rules["vehicle_model"] = str(vehicle_model)

    hunt_subtype = hunt.adapter_options.get("hunt_subtype")
    if hunt_subtype:
        rules["hunt_subtype"] = str(hunt_subtype)

    # city is used as the Craigslist subdomain; multi-word values cause DNS
    # failures (e.g. "mandeville louisiana").  For non-Craigslist adapters
    # (e.g. OfferUp) city is advisory only — the adapter logs it but does
    # not use it to control location (GeoIP drives results instead).
    raw_city = hunt.location or "houston"
    import re as _re
    if _re.search(r'\s', raw_city):
        log.warning(
            "Hunt '%s' has location %r which contains spaces — not a valid "
            "Craigslist subdomain.  Falling back to 'houston'.",
            hunt.name, raw_city,
        )
        raw_city = "houston"

    return {
        "hunt_id":      hunt.hunt_id,          # present only on DB-backed hunts; used for log traceability
        "name":         hunt.name,
        "source_sites": hunt.source_sites,     # full list; _expand_hunt_sources() fans these out
        "source":       hunt.source_sites[0],  # first site kept for YAML-consumer compat
        "query":        " ".join(hunt.search_terms),
        "city":         raw_city,
        "limit":        hunt.adapter_options.get("limit", 10),
        "rules":        rules,
    }
