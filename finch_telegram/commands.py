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
class StartCommand:
    kind: str = "start"


@dataclass(frozen=True)
class HelpCommand:
    kind: str = "help"


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


Command = (
    StartCommand
    | HelpCommand
    | HistoryCommand
    | ResetTripCommand
    | UndoLastCommand
    | PreviewCommand
    | AddCommand
    | AddListCommand
    | ChooseReplyCommand
    | CancelPendingCommand
    | SearchPendingCommand
)

_PENDING_PREFER_RE = re.compile(r"^prefer\s+(\d+)\s*$", re.IGNORECASE)
_PENDING_CHOOSE_RE = re.compile(r"^choose\s+(\d+)\s*$", re.IGNORECASE)
_PENDING_DIGIT_RE = re.compile(r"^(\d+)\s*$")
_PENDING_CANCEL_RE = re.compile(r"^(nvm|cancel)\s*$", re.IGNORECASE)
_PENDING_SEARCH_RE = re.compile(r"^search\s+(.+)$", re.IGNORECASE)

HELP_TEXT = """Finch grocery commands:
• help
• preview eggs, milk
• add eggs
• add eggs again / force add eggs
• add-list eggs, milk
• 2 eggs
• history / added today / what did finch add
• reset trip / new grocery trip
• undo last

If Finch asks you to choose, reply 1 to add once, prefer 1 to remember it, or nvm to cancel.

Finch tracks a local "added list" per trip (not your live Kroger cart).
Cart writes stay disabled until FINCH_LIVE_CART=true on Raven."""

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


def parse_pending_reply(message: str) -> ChooseReplyCommand | CancelPendingCommand | SearchPendingCommand | None:
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
    if lower == "preview":
        return None
    if lower.startswith("preview "):
        payload = text[len("preview ") :].strip()
        return PreviewCommand(text=payload) if payload else None
    if lower.startswith("add-list "):
        payload = text[len("add-list ") :].strip()
        return AddListCommand(text=payload) if payload else None
    if lower.startswith("add "):
        payload = text[len("add ") :].strip()
        return AddCommand(item=payload) if payload else None
    if _QUANTITY_ADD_RE.match(text):
        return AddCommand(item=text.strip())
    return None


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


def format_needs_choice_response(payload: dict[str, Any]) -> str:
    requested = payload.get("requested_item") or payload.get("normalized_name") or "item"
    results = payload.get("results") or []
    lines = [f'Needs choice for "{requested}":', ""]
    if not results:
        lines.append("No Kroger results found. Reply search <query> to try again, or nvm to cancel.")
        return "\n".join(lines)

    for index, result in enumerate(results, start=1):
        name = result.get("description") or "item"
        size = result.get("size")
        price = result.get("price")
        detail_parts = [name]
        if size:
            detail_parts.append(str(size))
        line = f"{index}. {' — '.join(detail_parts)}"
        if price:
            line += f" — {price}"
        lines.append(line)

    lines.append("")
    lines.append("Reply 1 to add once, prefer 1 to remember it, search <query> to refine, or nvm to cancel.")
    return "\n".join(lines)


def format_choose_response(payload: dict[str, Any]) -> str:
    if payload.get("duplicate"):
        return str(payload.get("message") or "Already added this trip.")
    attempt = payload.get("attempt") or {}
    name = attempt.get("normalized_name") or attempt.get("requested_item") or "item"
    alias = attempt.get("alias_name")
    if payload.get("preferred"):
        line = f"Saved preference and added: {name}"
    else:
        line = f"Added: {name}"
    if alias:
        line += f" ({alias})"
    return line


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


def format_error(message: str) -> str:
    return f"Finch error: {message}"
