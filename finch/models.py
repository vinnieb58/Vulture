"""Shared Finch data structures."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class MatchStatus(str, Enum):
    """How confidently Finch resolved a grocery intent."""

    EXACT_DEFAULT = "exact_default"
    NEEDS_SEARCH = "needs_search"
    AMBIGUOUS = "ambiguous"
    MISSING = "missing"


@dataclass(frozen=True)
class GroceryIntent:
    """One normalized item parsed from raw grocery text."""

    raw_text: str
    normalized_name: str
    quantity: float
    unit: str | None = None


@dataclass(frozen=True)
class AliasEntry:
    """Preferred Kroger mapping for a common grocery term."""

    alias_key: str
    display_name: str
    kroger_product_id: str | None = None
    upc: str | None = None
    search_term: str | None = None
    notes: str | None = None


@dataclass(frozen=True)
class PreviewLine:
    """Dry-run output for one requested grocery item."""

    requested_item: str
    normalized_name: str
    matched_alias: str | None
    kroger_product_id: str | None
    upc: str | None
    quantity: float
    status: MatchStatus
    search_term: str | None = None
    notes: str | None = None

    def to_dict(self) -> dict:
        return {
            "requested_item": self.requested_item,
            "normalized_name": self.normalized_name,
            "matched_alias": self.matched_alias,
            "kroger_product_id": self.kroger_product_id,
            "upc": self.upc,
            "quantity": self.quantity,
            "status": self.status.value,
            "search_term": self.search_term,
            "notes": self.notes,
        }
