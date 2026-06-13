"""Parse raw multiline grocery text into normalized intents."""

from __future__ import annotations

import re

from finch.models import GroceryIntent

# Quantity prefix patterns: "2 eggs", "3x milk", "2 dozen eggs", "1/2 lb flank steak"
_QUANTITY_RE = re.compile(
    r"^\s*"
    r"(?:(?P<qty>\d+/\d+|\d+(?:\.\d+)?)\s*(?:x|×)?\s*)?"
    r"(?:(?P<unit>dozen|dz|lb|lbs|oz|g|kg|pack|packs|ct|count|gal|gallon|bottle|bottles|bag|bags)\s+)?"
    r"(?P<name>.+?)\s*$",
    re.IGNORECASE,
)

# Strip bullets; numbered lists like "1. eggs" are handled separately (not bare digits).
_LINE_CLEAN_RE = re.compile(r"^[\s\-*•]+\s*")
_NUMBERED_LIST_RE = re.compile(r"^\d+[.)]\s+")

# Split on commas, semicolons, or newlines outside of simple contexts.
_SPLIT_RE = re.compile(r"[,;\n]+")


def _normalize_name(name: str) -> str:
    cleaned = name.strip().lower()
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned


def _parse_quantity(raw: str | None) -> float:
    if not raw:
        return 1.0
    if "/" in raw:
        num, den = raw.split("/", 1)
        try:
            return float(num) / float(den)
        except (ValueError, ZeroDivisionError):
            return 1.0
    try:
        return float(raw)
    except ValueError:
        return 1.0


def _parse_line(line: str) -> GroceryIntent | None:
    stripped = line.strip()
    if not stripped:
        return None
    if stripped.startswith("#"):
        return None

    if "#" in stripped:
        stripped = stripped.split("#", 1)[0].strip()
        if not stripped:
            return None

    stripped = _LINE_CLEAN_RE.sub("", stripped).strip()
    stripped = _NUMBERED_LIST_RE.sub("", stripped).strip()
    if not stripped:
        return None

    match = _QUANTITY_RE.match(stripped)
    if not match:
        return GroceryIntent(
            raw_text=stripped,
            normalized_name=_normalize_name(stripped),
            quantity=1.0,
        )

    qty = _parse_quantity(match.group("qty"))
    unit = match.group("unit")
    name = match.group("name") or stripped

    # "2 dozen eggs" — treat dozen as multiplier on quantity when name follows.
    if unit and unit.lower() in ("dozen", "dz"):
        qty *= 12.0
        unit = None

    return GroceryIntent(
        raw_text=stripped,
        normalized_name=_normalize_name(name),
        quantity=qty,
        unit=unit.lower() if unit else None,
    )


def parse_grocery_text(text: str) -> list[GroceryIntent]:
    """Parse messy grocery list text into normalized intents."""
    if not text or not text.strip():
        return []

    intents: list[GroceryIntent] = []
    seen: set[str] = set()

    for chunk in _SPLIT_RE.split(text):
        for line in chunk.splitlines() if "\n" in chunk else [chunk]:
            intent = _parse_line(line)
            if intent is None:
                continue
            dedupe_key = f"{intent.normalized_name}|{intent.quantity}|{intent.unit}"
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            intents.append(intent)

    return intents
