"""Explicit WhatsApp command parsing and reply formatting."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class HelpCommand:
    kind: str = "help"


@dataclass(frozen=True)
class HistoryCommand:
    kind: str = "history"


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


Command = HelpCommand | HistoryCommand | PreviewCommand | AddCommand | AddListCommand

HELP_TEXT = """Finch grocery commands:
• help
• preview eggs, milk
• add eggs
• add-list eggs, milk
• history

Cart writes stay disabled until FINCH_LIVE_CART=true on Raven."""


def parse_command(message: str) -> Command | None:
    text = message.strip()
    if not text:
        return None

    lower = text.lower()
    if lower == "help":
        return HelpCommand()
    if lower == "history":
        return HistoryCommand()
    if lower.startswith("preview "):
        payload = text[len("preview ") :].strip()
        return PreviewCommand(text=payload) if payload else None
    if lower.startswith("add-list "):
        payload = text[len("add-list ") :].strip()
        return AddListCommand(text=payload) if payload else None
    if lower.startswith("add "):
        payload = text[len("add ") :].strip()
        return AddCommand(item=payload) if payload else None
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


def format_add_response(payload: dict[str, Any]) -> str:
    attempt = payload.get("attempt") or {}
    name = attempt.get("normalized_name") or attempt.get("requested_item") or "item"
    alias = attempt.get("alias_name")
    line = f"Added: {name}"
    if alias:
        line += f" ({alias})"
    return line


def format_add_list_response(payload: dict[str, Any]) -> str:
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
    entries = payload.get("entries") or []
    if not entries:
        return "No recent Finch cart activity."
    lines = []
    for entry in entries[:10]:
        requested = entry.get("requested_text") or "item"
        action = entry.get("action") or "unknown"
        result = entry.get("result") or ""
        lines.append(f"• {requested} — {action} — {result}")
    return "Recent Finch cart activity:\n" + "\n".join(lines)


def format_error(message: str) -> str:
    return f"Finch error: {message}"
