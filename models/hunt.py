from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional
from uuid import uuid4


def _now_iso() -> str:
    """Return the current UTC time as an ISO 8601 string."""
    return datetime.now(timezone.utc).isoformat()


@dataclass
class Hunt:
    """
    Persistent hunt definition stored in the database.

    List and dict fields (source_sites, search_terms, include_keywords,
    exclude_keywords, adapter_options) are serialized as JSON text in SQLite.
    """

    # Required — the only field with no default
    name: str

    # Identity
    hunt_id: str = field(default_factory=lambda: str(uuid4()))

    # Classification
    category: Optional[str] = None

    # What to search
    source_sites: list = field(default_factory=list)       # e.g. ["craigslist"]
    search_terms: list = field(default_factory=list)        # primary query terms

    # Rule filters (mirrors the v1.0 YAML rule engine)
    include_keywords: list = field(default_factory=list)
    exclude_keywords: list = field(default_factory=list)
    max_price: Optional[int] = None

    # Geography
    location: Optional[str] = None
    radius: Optional[int] = None                           # miles

    # Lifecycle — valid values: "active" | "paused" | "ended"
    status: str = "active"

    # Metadata
    created_by: Optional[str] = None
    created_at: str = field(default_factory=_now_iso)
    updated_at: str = field(default_factory=_now_iso)
    notes: Optional[str] = None

    # Adapter-specific overrides stored as an arbitrary dict
    adapter_options: dict = field(default_factory=dict)
