"""Preference management helpers — list, get, delete, change, and key redirects."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from finch.aliases import (
    delete_aliases_matching_normalized,
    get_all_aliases,
    lookup_alias,
    set_preference_redirect,
)
from finch.cart_choice import (
    NeedsChoiceOutcome,
    build_needs_choice_outcome,
    search_products_for_choice,
)
from finch.kroger_client import KrogerClient
from finch.models import AliasEntry
from finch.parser import parse_grocery_text
from finch.preference_norm import normalize_preference_key


def format_preferences_list(*, db_path: Path | None = None) -> str:
    """Return saved preferences as readable lines: key -> display_name."""
    entries = get_all_aliases(db_path)
    pinned = [
        entry
        for entry in entries
        if entry.notes and "Pinned via" in entry.notes
    ]
    if not pinned:
        return "No saved preferences yet.\nUse add <item>, then prefer 1 after a search."

    lines = ["Saved preferences:"]
    for entry in sorted(pinned, key=lambda item: item.alias_key):
        lines.append(f"{entry.alias_key} -> {entry.display_name}")
    return "\n".join(lines)


def get_preference_text(item: str, *, db_path: Path | None = None) -> str:
    """Return a user-facing message for a single preference lookup."""
    key = normalize_preference_key(item)
    if not key:
        return "Usage: pref <item>"

    entry = lookup_alias(key, db_path=db_path)
    if entry is None:
        return (
            f"No saved preference for {item!r}.\n"
            f"Try add {item} to search Kroger and prefer 1 to save one."
        )
    return f"{entry.alias_key} -> {entry.display_name}"


def forget_preference(item: str, *, db_path: Path | None = None) -> str:
    """Delete a saved preference by normalized item key."""
    removed = delete_aliases_matching_normalized(item, db_path=db_path)
    return format_forget_message(item, removed)


def format_forget_message(item: str, removed: list[AliasEntry]) -> str:
    if not removed:
        return f"No saved preference for {item!r} to remove."
    display_names = ", ".join(entry.display_name for entry in removed)
    keys = ", ".join(entry.alias_key for entry in removed)
    return f"Removed preference for {keys}: {display_names}."


def alias_preference_key(
    new_key: str,
    existing_key: str,
    *,
    db_path: Path | None = None,
) -> str:
    """Map new_key lookups to existing_key's preference."""
    from_key = normalize_preference_key(new_key)
    to_key = normalize_preference_key(existing_key)
    if not from_key or not to_key:
        return "Usage: alias NEW to EXISTING"

    target = lookup_alias(to_key, db_path=db_path)
    if target is None:
        return (
            f"No saved preference for {existing_key!r}.\n"
            f"Save one first with add {existing_key} and prefer 1."
        )

    set_preference_redirect(from_key, target.alias_key, db_path=db_path)
    return f"Alias {from_key!r} -> {target.alias_key!r} ({target.display_name})."


def prepare_change_preference(
    item_text: str,
    *,
    chat_key: str,
    client: KrogerClient,
    db_path: Path | None = None,
    pending_db_path: Path | None = None,
) -> NeedsChoiceOutcome:
    """Bypass existing preference and start a fresh Kroger search for replacement."""
    intents = parse_grocery_text(item_text)
    if not intents:
        from finch.cart_ops import CartResolveError

        raise CartResolveError(f"Could not parse item: {item_text!r}")
    if len(intents) > 1:
        from finch.cart_ops import CartResolveError

        raise CartResolveError(
            f"Multiple items parsed from {item_text!r}; change one item at a time."
        )

    intent = intents[0]
    qty = max(1, int(intent.quantity))
    search_query = intent.normalized_name
    results = search_products_for_choice(search_query, client=client)
    return build_needs_choice_outcome(
        requested_item=intent.raw_text,
        normalized_name=intent.normalized_name,
        search_query=search_query,
        quantity=qty,
        results=results,
        chat_key=chat_key,
        pending_db_path=pending_db_path,
    )


def preference_to_dict(entry: AliasEntry) -> dict[str, Any]:
    return {
        "alias_key": entry.alias_key,
        "display_name": entry.display_name,
        "kroger_product_id": entry.kroger_product_id,
        "upc": entry.upc,
        "search_term": entry.search_term,
        "notes": entry.notes,
    }
