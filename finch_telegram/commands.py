"""Explicit Telegram command parsing and reply formatting."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

# Quantity shorthand: "2 eggs", "3x milk", "1/2 lb flank steak"
_QUANTITY_ADD_RE = re.compile(
    r"^\s*"
    r"(?:(?P<qty>\d+/\d+|\d+(?:\.\d+)?)\s*(?:x|×)?\s*)"
    r"(?:(?P<unit>dozen|dz|lb|lbs|oz|g|kg|pack|packs|ct|count|gal|gallon|bottle|bottles|bag|bags)\s+)?"
    r"(?P<name>.+?)\s*$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class AddStaplesCommand:
    kind: str = "add-staples"


@dataclass(frozen=True)
class StaplesListCommand:
    kind: str = "staples-list"


@dataclass(frozen=True)
class StaplesConfirmCommand:
    kind: str = "staples-confirm"


@dataclass(frozen=True)
class StaplesRemoveCommand:
    targets: str
    kind: str = "staples-remove"


@dataclass(frozen=True)
class StaplesCancelCommand:
    kind: str = "staples-cancel"


@dataclass(frozen=True)
class StartCommand:
    kind: str = "start"


@dataclass(frozen=True)
class HelpCommand:
    kind: str = "help"


@dataclass(frozen=True)
class HelpPrefsCommand:
    kind: str = "help-prefs"


@dataclass(frozen=True)
class HistoryCommand:
    kind: str = "history"
    scope: str = "trip"


@dataclass(frozen=True)
class ResetTripCommand:
    kind: str = "reset-trip"


@dataclass(frozen=True)
class UndoLastCommand:
    kind: str = "undo-last"


@dataclass(frozen=True)
class PreviewCommand:
    text: str
    kind: str = "preview"


@dataclass(frozen=True)
class AddCommand:
    item: str
    kind: str = "add"


@dataclass(frozen=True)
class AddListCommand:
    text: str
    kind: str = "add-list"


@dataclass(frozen=True)
class ChooseReplyCommand:
    selection: int
    prefer: bool = False
    kind: str = "choose-reply"


@dataclass(frozen=True)
class CancelPendingCommand:
    kind: str = "cancel-pending"


@dataclass(frozen=True)
class SearchPendingCommand:
    query: str
    kind: str = "search-pending"


@dataclass(frozen=True)
class MorePendingCommand:
    kind: str = "more-pending"


@dataclass(frozen=True)
class BackPendingCommand:
    kind: str = "back-pending"


@dataclass(frozen=True)
class PrefsCommand:
    kind: str = "prefs"


@dataclass(frozen=True)
class PrefCommand:
    item: str
    kind: str = "pref"


@dataclass(frozen=True)
class ForgetPrefCommand:
    item: str
    kind: str = "forget-pref"


@dataclass(frozen=True)
class ChangePrefCommand:
    item: str
    kind: str = "change-pref"


@dataclass(frozen=True)
class AliasPrefCommand:
    new_key: str
    existing_key: str
    kind: str = "alias-pref"


Command = (
    StartCommand
    | HelpCommand
    | HelpPrefsCommand
    | HistoryCommand
    | ResetTripCommand
    | UndoLastCommand
    | PreviewCommand
    | AddCommand
    | AddListCommand
    | AddStaplesCommand
    | StaplesListCommand
    | ChooseReplyCommand
    | CancelPendingCommand
    | SearchPendingCommand
    | MorePendingCommand
    | BackPendingCommand
    | PrefsCommand
    | PrefCommand
    | ForgetPrefCommand
    | ChangePrefCommand
    | AliasPrefCommand
)

StapleReplyCommand = StaplesConfirmCommand | StaplesRemoveCommand | StaplesCancelCommand

_PENDING_PREFER_RE = re.compile(r"^prefer\s+(\d+)\s*$", re.IGNORECASE)
_PENDING_CHOOSE_RE = re.compile(r"^choose\s+(\d+)\s*$", re.IGNORECASE)
_PENDING_DIGIT_RE = re.compile(r"^(\d+)\s*$")
_PENDING_CANCEL_RE = re.compile(r"^(nvm|cancel)\s*$", re.IGNORECASE)
_PENDING_MORE_RE = re.compile(r"^more\s*$", re.IGNORECASE)
_PENDING_BACK_RE = re.compile(r"^back\s*$", re.IGNORECASE)
_PENDING_SEARCH_RE = re.compile(r"^search\s+(.+)$", re.IGNORECASE)
_ALIAS_PREF_RE = re.compile(r"^alias\s+(.+?)\s+to\s+(.+)$", re.IGNORECASE)
_REMOVE_PREF_RE = re.compile(r"^remove\s+preference\s+(.+)$", re.IGNORECASE)
_STAPLES_REMOVE_RE = re.compile(r"^remove\s+(.+)$", re.IGNORECASE)

HELP_TEXT = """Finch grocery commands:
- add eggs
- add-list eggs, milk
- 2 eggs
- history
- prefs
- help preferences

If Finch asks you to choose:
- reply 1-10 to add once (or the shown range after more)
- reply prefer 1 to remember it
- reply more to see more results
- reply back to go to the previous page
- reply search <query> to refine
- reply nvm or cancel to cancel

Finch tracks what it added this trip. Kroger is still the live cart."""

HELP_PREFS_TEXT = """Preference commands:
- prefs — list saved preferences
- pref bagels — show one saved preference
- forget bagels — remove one
- change bagels — pick a different preferred product
- alias plain bagels to bagel — reuse another preference"""

START_TEXT = (
    "Welcome to Finch on Telegram.\n\n"
    + HELP_TEXT
    + "\n\nIf this is your first message, check Raven logs for your Telegram user ID "
    "and add it to FINCH_TELEGRAM_ALLOWED_USER_IDS."
)


def normalize_message(message: str) -> str:
    text = message.strip()
    if text.startswith("/") and "@" in text:
        text = text.split("@", 1)[0]
    return text


def parse_staple_reply(message: str) -> StapleReplyCommand | None:
    text = normalize_message(message)
    if not text:
        return None
    if _REMOVE_PREF_RE.match(text):
        return None
    lower = text.lower()
    if lower == "confirm":
        return StaplesConfirmCommand()
    if lower in ("nvm", "cancel"):
        return StaplesCancelCommand()
    match = _STAPLES_REMOVE_RE.match(text)
    if match:
        targets = match.group(1).strip()
        if targets:
            return StaplesRemoveCommand(targets=targets)
    return None


def parse_pending_reply(
    message: str,
) -> (
    ChooseReplyCommand
    | CancelPendingCommand
    | SearchPendingCommand
    | MorePendingCommand
    | BackPendingCommand
    | None
):
    text = normalize_message(message)
    if not text:
        return None
    match = _PENDING_PREFER_RE.match(text)
    if match:
        return ChooseReplyCommand(selection=int(match.group(1)), prefer=True)
    match = _PENDING_CHOOSE_RE.match(text)
    if match:
        return ChooseReplyCommand(selection=int(match.group(1)), prefer=False)
    match = _PENDING_DIGIT_RE.match(text)
    if match:
        return ChooseReplyCommand(selection=int(match.group(1)), prefer=False)
    if _PENDING_CANCEL_RE.match(text):
        return CancelPendingCommand()
    if _PENDING_MORE_RE.match(text):
        return MorePendingCommand()
    if _PENDING_BACK_RE.match(text):
        return BackPendingCommand()
    match = _PENDING_SEARCH_RE.match(text)
    if match:
        query = match.group(1).strip()
        return SearchPendingCommand(query=query) if query else None
    return None


def parse_command(message: str) -> Command | None:
    text = normalize_message(message)
    if not text:
        return None

    lower = text.lower()
    if lower in ("/start", "start"):
        return StartCommand()
    if lower in ("help preferences", "help prefs"):
        return HelpPrefsCommand()
    if lower in ("help", "/help"):
        return HelpCommand()
    if lower in ("history", "finch history", "what did finch add"):
        return HistoryCommand(scope="trip")
    if lower == "added today":
        return HistoryCommand(scope="today")
    if lower in ("reset trip", "new grocery trip"):
        return ResetTripCommand()
    if lower == "undo last":
        return UndoLastCommand()
    if lower in (
        "prefs",
        "preferences",
        "list prefs",
        "list preferences",
        "show prefs",
        "show preferences",
    ):
        return PrefsCommand()
    match = _ALIAS_PREF_RE.match(text)
    if match:
        new_key = match.group(1).strip()
        existing_key = match.group(2).strip()
        if new_key and existing_key:
            return AliasPrefCommand(new_key=new_key, existing_key=existing_key)
    match = _REMOVE_PREF_RE.match(text)
    if match:
        item = match.group(1).strip()
        return ForgetPrefCommand(item=item) if item else None
    if lower.startswith("forget "):
        payload = text[len("forget ") :].strip()
        return ForgetPrefCommand(item=payload) if payload else None
    if lower.startswith("change "):
        payload = text[len("change ") :].strip()
        return ChangePrefCommand(item=payload) if payload else None
    if lower.startswith("preference "):
        payload = text[len("preference ") :].strip()
        return PrefCommand(item=payload) if payload else None
    if lower.startswith("pref "):
        payload = text[len("pref ") :].strip()
        return PrefCommand(item=payload) if payload else None
    if lower == "preview":
        return None
    if lower.startswith("preview "):
        payload = text[len("preview ") :].strip()
        return PreviewCommand(text=payload) if payload else None
    if lower.startswith("add-list "):
        payload = text[len("add-list ") :].strip()
        return AddListCommand(text=payload) if payload else None
    if lower == "add staples":
        return AddStaplesCommand()
    if lower.startswith("add "):
        payload = text[len("add ") :].strip()
        return AddCommand(item=payload) if payload else None
    if lower == "staples":
        return StaplesListCommand()
    if _QUANTITY_ADD_RE.match(text):
        return AddCommand(item=text.strip())
    return None


def format_staples_response(payload: dict[str, Any]) -> str:
    return str(payload.get("text") or "No staples configured.")


def format_staple_batch_response(payload: dict[str, Any]) -> str:
    if payload.get("message") and not payload.get("text"):
        return str(payload["message"])
    return str(payload.get("text") or payload.get("message") or "Staple batch updated.")


def format_preview_response(payload: dict[str, Any]) -> str:
    lines = payload.get("lines") or []
    matched: list[str] = []
    missing: list[str] = []
    for line in lines:
        name = line.get("normalized_name") or line.get("requested_item") or "item"
        status = line.get("status") or ""
        if status == "missing":
            missing.append(name)
            continue
        alias = line.get("matched_alias") or name
        matched.append(f"{name} ({alias})")

    parts: list[str] = []
    if matched:
        parts.append("Matched:\n" + "\n".join(f"• {item}" for item in matched))
    if missing:
        parts.append("Missing:\n" + "\n".join(f"• {item}" for item in missing))
    if not parts:
        return "Preview: no items found."
    return "Preview:\n\n" + "\n\n".join(parts)


def format_cart_blocked(detail: str | None = None) -> str:
    message = "Cart writes are currently disabled."
    if detail and "FINCH_LIVE_CART" in detail:
        return message
    return message


def _brand_in_text(brand: str, text: str) -> bool:
    return brand.strip().lower() in text.strip().lower()


def _should_show_brand_for_results(results: list[dict[str, Any]]) -> list[bool]:
    brands = [str(item.get("brand") or "").strip() for item in results]
    normalized_brands = {brand.lower() for brand in brands if brand}
    multiple_brands = len(normalized_brands) > 1
    show_brand: list[bool] = []
    for item in results:
        brand = str(item.get("brand") or "").strip()
        description = str(item.get("description") or "item")
        if not brand:
            show_brand.append(False)
            continue
        show_brand.append(multiple_brands or not _brand_in_text(brand, description))
    return show_brand


def format_search_result_line(
    index: int,
    result: dict[str, Any],
    *,
    show_brand: bool = False,
) -> str:
    description = str(result.get("description") or "item").strip()
    brand = str(result.get("brand") or "").strip()
    size = str(result.get("size") or "").strip()
    price = str(result.get("price") or "").strip()

    if show_brand and brand and not _brand_in_text(brand, description):
        title = f"{brand} {description}"
    else:
        title = description

    parts = [title]
    if size and not _brand_in_text(size, title):
        parts.append(size)
    if price:
        parts.append(price)
    return f"{index}. {' — '.join(parts)}"


def format_needs_choice_response(payload: dict[str, Any]) -> str:
    requested = payload.get("requested_item") or payload.get("normalized_name") or "item"
    search_query = payload.get("search_query") or requested
    results = payload.get("results") or []
    page_start = int(payload.get("page_start") or 1)
    page_end = int(payload.get("page_end") or len(results))
    total_count = payload.get("total_count")
    has_more = bool(payload.get("has_more"))
    has_back = bool(payload.get("has_back"))

    if not results:
        lines = [f'Needs choice for "{requested}":', ""]
        lines.append("No Kroger results found. Reply search <query> to try again, or cancel.")
        return "\n".join(lines)

    if total_count is not None:
        lines = [
            f'Found {total_count} matches for "{search_query}":',
            "",
            f"Showing {page_start}-{page_end} of {total_count}.",
            "",
        ]
    else:
        lines = [f'Found multiple matches for "{requested}":', ""]

    show_brand_flags = _should_show_brand_for_results(results)
    for offset, result in enumerate(results):
        index = page_start + offset
        lines.append(
            format_search_result_line(
                index,
                result,
                show_brand=show_brand_flags[offset],
            )
        )

    lines.append("")
    lines.append("Reply with:")
    if page_start == page_end:
        lines.append(f"- {page_start} to select")
    else:
        lines.append(f"- {page_start}-{page_end} to select")
    if has_more:
        lines.append("- more")
    if has_back:
        lines.append("- back")
    lines.append("- search <query> to refine")
    lines.append("- prefer <number> to remember this choice")
    lines.append("- cancel")
    if has_more:
        lines.append("")
        lines.append('Not seeing it? Reply "more" or "search <better query>".')
    return "\n".join(lines)


def format_choose_response(payload: dict[str, Any]) -> str:
    if payload.get("duplicate"):
        return str(payload.get("message") or "Already added this trip.")
    if payload.get("needs_choice"):
        parts: list[str] = []
        attempt = payload.get("attempt") or {}
        name = attempt.get("normalized_name") or attempt.get("requested_item") or "item"
        alias = attempt.get("alias_name")
        if payload.get("preferred"):
            line = f"Saved preference and added: {name}"
        else:
            line = f"Added: {name}"
        if alias:
            line += f" ({alias})"
        parts.append(line)
        continued_added = [
            o.get("attempt")
            for o in (payload.get("partial_outcomes") or [])[1:]
            if o.get("ok") and o.get("attempt")
        ]
        if continued_added:
            lines = []
            for item in continued_added:
                item_name = item.get("normalized_name") or item.get("requested_item") or "item"
                item_alias = item.get("alias_name")
                if item_alias:
                    lines.append(f"• {item_name} ({item_alias})")
                else:
                    lines.append(f"• {item_name}")
            parts.append("Added:\n" + "\n".join(lines))
        parts.append(format_needs_choice_response(payload))
        return "\n\n".join(parts)
    attempt = payload.get("attempt") or {}
    name = attempt.get("normalized_name") or attempt.get("requested_item") or "item"
    alias = attempt.get("alias_name")
    if payload.get("preferred"):
        line = f"Saved preference and added: {name}"
    else:
        line = f"Added: {name}"
    if alias:
        line += f" ({alias})"
    parts = [line]
    continued = payload.get("continued_outcomes") or []
    continued_added = [
        o.get("attempt") for o in continued if o.get("ok") and o.get("attempt")
    ]
    if continued_added:
        lines = []
        for item in continued_added:
            item_name = item.get("normalized_name") or item.get("requested_item") or "item"
            item_alias = item.get("alias_name")
            if item_alias:
                lines.append(f"• {item_name} ({item_alias})")
            else:
                lines.append(f"• {item_name}")
        parts.append("Added:\n" + "\n".join(lines))
    return "\n\n".join(parts)


def format_cancel_pending_response(payload: dict[str, Any]) -> str:
    return str(payload.get("message") or "Cancelled pending product choice.")


def format_add_response(payload: dict[str, Any]) -> str:
    if payload.get("needs_choice"):
        return format_needs_choice_response(payload)
    if payload.get("duplicate"):
        return str(payload.get("message") or "Already added this trip.")
    attempt = payload.get("attempt") or {}
    name = attempt.get("normalized_name") or attempt.get("requested_item") or "item"
    alias = attempt.get("alias_name")
    line = f"Added: {name}"
    if alias:
        line += f" ({alias})"
    return line


def format_add_list_response(payload: dict[str, Any]) -> str:
    if payload.get("needs_choice"):
        parts: list[str] = []
        partial = payload.get("partial_outcomes") or []
        added_attempts = [
            o.get("attempt")
            for o in partial
            if o.get("ok") and o.get("attempt")
        ]
        if added_attempts:
            lines = []
            for attempt in added_attempts:
                name = attempt.get("normalized_name") or attempt.get("requested_item") or "item"
                alias = attempt.get("alias_name")
                if alias:
                    lines.append(f"• {name} ({alias})")
                else:
                    lines.append(f"• {name}")
            parts.append("Added:\n" + "\n".join(lines))
        parts.append(format_needs_choice_response(payload))
        return "\n\n".join(parts)
    added = payload.get("succeeded") or []
    skipped = payload.get("failed") or []
    parts: list[str] = []
    if added:
        lines = []
        for attempt in added:
            name = attempt.get("normalized_name") or attempt.get("requested_item") or "item"
            alias = attempt.get("alias_name")
            if alias:
                lines.append(f"• {name} ({alias})")
            else:
                lines.append(f"• {name}")
        parts.append("Added:\n" + "\n".join(lines))
    if skipped:
        lines = []
        for item in skipped:
            name = item.get("item") or "item"
            error = item.get("error") or "skipped"
            lines.append(f"• {name} — {error}")
        parts.append("Skipped:\n" + "\n".join(lines))
    if not parts:
        return "Add-list: no items processed."
    return "\n\n".join(parts)


def format_history_response(payload: dict[str, Any]) -> str:
    text = payload.get("text")
    if text:
        return str(text)
    items = payload.get("items") or []
    if not items:
        return "Finch added list: empty.\n(Kroger app is the source of truth for your live cart.)"
    lines = [str(payload.get("title") or "Finch added list") + ":"]
    lines.append("(This is what Finch added — not your live Kroger cart.)")
    for item in items[:10]:
        label = item.get("display_name") or item.get("normalized_name") or "item"
        qty = item.get("quantity") or 1
        qty_text = f" x{qty}" if qty != 1 else ""
        lines.append(f"• {label}{qty_text}")
    return "\n".join(lines)


_MISSING_ROUTE_MESSAGE = (
    "That Finch API route is missing or not deployed yet. "
    "Try restarting finch-api or updating Raven."
)


def format_error(message: str) -> str:
    return f"Finch error: {message}"


def format_api_error(
    *,
    status_code: int,
    detail: str,
    method: str | None = None,
    path: str | None = None,
) -> str:
    if status_code == 404 or detail.strip().lower() == "not found":
        return _MISSING_ROUTE_MESSAGE
    if status_code >= 500:
        return f"Finch error: The grocery service had a problem ({status_code}). Try again in a moment."
    return format_error(detail)


def format_preferences_response(payload: dict[str, Any]) -> str:
    return str(payload.get("text") or "No saved preferences.")


def format_preference_get_response(payload: dict[str, Any]) -> str:
    return str(payload.get("text") or "No saved preference.")


def format_preference_delete_response(payload: dict[str, Any]) -> str:
    return str(payload.get("text") or "Preference removed.")


def format_preference_alias_response(payload: dict[str, Any]) -> str:
    return str(payload.get("text") or "Preference alias saved.")
