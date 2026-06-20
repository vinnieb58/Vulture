"""Preference management helpers — list, get, delete, change, and key redirects."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from finch.aliases import (
    delete_aliases_matching_normalized,
    get_all_aliases,
    get_all_preference_redirects,
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

_EMPTY_PREFS_TEXT = (
    'No saved Finch preferences yet.\n'
    'Use "add bagels", then reply "prefer 1" to save one.'
)


def _is_user_pinned(entry: AliasEntry) -> bool:
    return bool(entry.notes and "Pinned via" in entry.notes)


def _format_product_detail(entry: AliasEntry) -> str:
    detail = entry.display_name
    extras: list[str] = []
    if entry.product_size:
        extras.append(entry.product_size)
    if entry.product_price:
        extras.append(entry.product_price)
    if extras:
        detail = f"{detail} ({', '.join(extras)})"
    return detail


def format_preference_line(entry: AliasEntry) -> str:
    return f"- {entry.alias_key} → {_format_product_detail(entry)}"


def format_alias_line(from_key: str, to_key: str) -> str:
    return f"- {from_key} → {to_key}"


def _collect_preferences_data(
    db_path: Path | None = None,
) -> tuple[list[AliasEntry], list[tuple[str, str]]]:
    entries = get_all_aliases(db_path)
    pinned = sorted(
        (entry for entry in entries if _is_user_pinned(entry)),
        key=lambda item: item.alias_key,
    )
    redirects = get_all_preference_redirects(db_path)
    return pinned, redirects


def build_preferences_list(*, db_path: Path | None = None) -> dict[str, Any]:
    """Build structured preference list data for API and Telegram formatting."""
    pinned, redirects = _collect_preferences_data(db_path)
    return {
        "preferences": [preference_to_dict(entry) for entry in pinned],
        "aliases": [{"from_key": src, "to_key": dst} for src, dst in redirects],
        "text": _format_preferences_text(pinned, redirects),
    }


def format_preferences_list(*, db_path: Path | None = None) -> str:
    """Return saved preferences in a family-friendly readable format."""
    pinned, redirects = _collect_preferences_data(db_path)
    return _format_preferences_text(pinned, redirects)


def _format_preferences_text(
    pinned: list[AliasEntry],
    redirects: list[tuple[str, str]],
) -> str:
    if not pinned and not redirects:
        return _EMPTY_PREFS_TEXT

    lines: list[str] = []
    if pinned:
        lines.append("Saved Finch preferences:")
        for entry in pinned:
            lines.append(format_preference_line(entry))

    if redirects:
        if lines:
            lines.append("")
        lines.append("Aliases:")
        for from_key, to_key in redirects:
            lines.append(format_alias_line(from_key, to_key))

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
            f'Try add {item} to search Kroger and reply "prefer 1" to save one.'
        )
    return format_preference_line(entry).lstrip("- ")


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
            f'Save one first with add {existing_key} and reply "prefer 1".'
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
    results, total_count = search_products_for_choice(search_query, client=client)
    return build_needs_choice_outcome(
        requested_item=intent.raw_text,
        normalized_name=intent.normalized_name,
        search_query=search_query,
        quantity=qty,
        cached_results=results,
        total_count=total_count,
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
        "product_size": entry.product_size,
        "product_price": entry.product_price,
    }
