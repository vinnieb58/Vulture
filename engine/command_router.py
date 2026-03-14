"""
engine/command_router.py

Structured command routing layer for Vulture v2.0.

Responsibilities:
- Accept structured command inputs (command name + args dict)
- Dispatch to the appropriate hunt_service method
- Translate service exceptions into user-friendly error messages
- Return CommandResult objects that a Discord bot (or any caller) can consume

This module contains NO Discord-specific code. The Discord adapter will call
dispatch() and format the CommandResult into Discord messages/embeds.
"""

import logging
from dataclasses import dataclass, field
from typing import Optional

from engine.hunt_service import (
    HuntNotFoundError,
    HuntStateError,
    HuntValidationError,
    create_hunt,
    edit_hunt,
    end_hunt,
    get_hunt,
    list_hunts,
    pause_hunt,
    resume_hunt,
)
from models.hunt import Hunt

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------

@dataclass
class CommandResult:
    """
    Returned by every command handler.

    success  — whether the operation succeeded
    message  — human-readable summary suitable for a Discord text reply
    data     — structured payload for callers that want to build rich embeds;
               None on failure
    """
    success: bool
    message: str
    data: Optional[dict] = field(default=None)


# ---------------------------------------------------------------------------
# Internal formatting helpers
# ---------------------------------------------------------------------------

def _hunt_to_dict(hunt: Hunt) -> dict:
    """Serialize a Hunt dataclass to a plain dict for CommandResult.data."""
    return {
        "hunt_id":          hunt.hunt_id,
        "name":             hunt.name,
        "status":           hunt.status,
        "category":         hunt.category,
        "source_sites":     hunt.source_sites,
        "search_terms":     hunt.search_terms,
        "include_keywords": hunt.include_keywords,
        "exclude_keywords": hunt.exclude_keywords,
        "max_price":        hunt.max_price,
        "location":         hunt.location,
        "radius":           hunt.radius,
        "created_by":       hunt.created_by,
        "created_at":       hunt.created_at,
        "updated_at":       hunt.updated_at,
        "notes":            hunt.notes,
        "adapter_options":  hunt.adapter_options,
    }


def _fmt_summary(hunt: Hunt) -> str:
    """One-line summary for list output."""
    price = f" | max ${hunt.max_price}" if hunt.max_price is not None else ""
    loc   = f" | {hunt.location}" if hunt.location else ""
    terms = " ".join(hunt.search_terms)
    sites = ", ".join(hunt.source_sites) if hunt.source_sites else "—"
    return (
        f"**{hunt.name}** [{hunt.status}] — {sites} | \"{terms}\""
        f"{price}{loc}\n`{hunt.hunt_id}`"
    )


def _fmt_detail(hunt: Hunt) -> str:
    """Multi-line detail block for show output."""
    price    = f"${hunt.max_price}" if hunt.max_price is not None else "—"
    loc      = hunt.location or "—"
    radius   = f"{hunt.radius} mi" if hunt.radius is not None else "—"
    include  = ", ".join(hunt.include_keywords) if hunt.include_keywords else "—"
    exclude  = ", ".join(hunt.exclude_keywords) if hunt.exclude_keywords else "—"
    sites    = ", ".join(hunt.source_sites) if hunt.source_sites else "—"
    terms    = " ".join(hunt.search_terms) if hunt.search_terms else "—"
    category = hunt.category or "—"
    notes    = hunt.notes or "—"

    return (
        f"**{hunt.name}**\n"
        f"ID: `{hunt.hunt_id}`\n"
        f"Status: {hunt.status}\n"
        f"Category: {category}\n"
        f"Source: {sites}\n"
        f"Search: {terms}\n"
        f"Include: {include}\n"
        f"Exclude: {exclude}\n"
        f"Max price: {price}\n"
        f"Location: {loc}  |  Radius: {radius}\n"
        f"Notes: {notes}\n"
        f"Created: {hunt.created_at[:19]}  |  Updated: {hunt.updated_at[:19]}"
    )


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------

def cmd_list(args: dict) -> CommandResult:
    """
    List hunts, optionally filtered by status.

    args:
      status (optional) — "active" | "paused" | "ended"
                          omit to return all hunts
    """
    status = args.get("status")
    try:
        hunts = list_hunts(status=status)
    except HuntValidationError as exc:
        return CommandResult(success=False, message=str(exc))

    if not hunts:
        label = f" with status '{status}'" if status else ""
        return CommandResult(
            success=True,
            message=f"No hunts found{label}.",
            data={"hunts": []},
        )

    lines = [_fmt_summary(h) for h in hunts]
    label = f" ({status})" if status else ""
    return CommandResult(
        success=True,
        message=f"**Hunts{label} — {len(hunts)} found**\n\n" + "\n\n".join(lines),
        data={"hunts": [_hunt_to_dict(h) for h in hunts]},
    )


def cmd_show(args: dict) -> CommandResult:
    """
    Show full details for a single hunt.

    args:
      hunt_id (required)
    """
    hunt_id = (args.get("hunt_id") or "").strip()
    if not hunt_id:
        return CommandResult(success=False, message="'hunt_id' is required for /hunt show")

    try:
        hunt = get_hunt(hunt_id)
    except HuntNotFoundError:
        return CommandResult(success=False, message=f"Hunt not found: `{hunt_id}`")

    log.info("Resolved hunt '%s' [%s] status=%s", hunt.name, hunt_id[:8], hunt.status)
    return CommandResult(
        success=True,
        message=_fmt_detail(hunt),
        data={"hunt": _hunt_to_dict(hunt)},
    )


def cmd_create(args: dict) -> CommandResult:
    """
    Create a new hunt.

    args:
      name            (required)
      search_terms    (required) — list of strings
      source_sites    (required) — list of strings, e.g. ["craigslist"]
      category        (optional)
      include_keywords(optional) — list of strings
      exclude_keywords(optional) — list of strings
      max_price       (optional) — integer
      location        (optional) — string
      radius          (optional) — integer (miles)
      created_by      (optional) — Discord user tag or ID
      notes           (optional)
      adapter_options (optional) — dict, e.g. {"limit": 20}
    """
    try:
        hunt = create_hunt(
            name         = args.get("name", ""),
            search_terms = args.get("search_terms") or [],
            source_sites = args.get("source_sites") or [],
            category        = args.get("category"),
            include_keywords= args.get("include_keywords"),
            exclude_keywords= args.get("exclude_keywords"),
            max_price       = args.get("max_price"),
            location        = args.get("location"),
            radius          = args.get("radius"),
            created_by      = args.get("created_by"),
            notes           = args.get("notes"),
            adapter_options = args.get("adapter_options"),
        )
    except HuntValidationError as exc:
        return CommandResult(success=False, message=f"Cannot create hunt: {exc}")

    return CommandResult(
        success=True,
        message=f"Hunt created.\n\n{_fmt_detail(hunt)}",
        data={"hunt": _hunt_to_dict(hunt)},
    )


def cmd_pause(args: dict) -> CommandResult:
    """
    Pause an active hunt.

    args:
      hunt_id (required)
    """
    hunt_id = (args.get("hunt_id") or "").strip()
    if not hunt_id:
        return CommandResult(success=False, message="'hunt_id' is required for /hunt pause")

    try:
        hunt = pause_hunt(hunt_id)
    except HuntNotFoundError:
        return CommandResult(success=False, message=f"Hunt not found: `{hunt_id}`")
    except HuntStateError as exc:
        return CommandResult(success=False, message=str(exc))

    log.info("Paused hunt '%s' [%s]", hunt.name, hunt_id[:8])
    return CommandResult(
        success=True,
        message=f"Hunt **{hunt.name}** paused.",
        data={"hunt": _hunt_to_dict(hunt)},
    )


def cmd_resume(args: dict) -> CommandResult:
    """
    Resume a paused hunt.

    args:
      hunt_id (required)
    """
    hunt_id = (args.get("hunt_id") or "").strip()
    if not hunt_id:
        return CommandResult(success=False, message="'hunt_id' is required for /hunt resume")

    try:
        hunt = resume_hunt(hunt_id)
    except HuntNotFoundError:
        return CommandResult(success=False, message=f"Hunt not found: `{hunt_id}`")
    except HuntStateError as exc:
        return CommandResult(success=False, message=str(exc))

    log.info("Resumed hunt '%s' [%s]", hunt.name, hunt_id[:8])
    return CommandResult(
        success=True,
        message=f"Hunt **{hunt.name}** resumed.",
        data={"hunt": _hunt_to_dict(hunt)},
    )


def cmd_end(args: dict) -> CommandResult:
    """
    Permanently end a hunt.

    args:
      hunt_id (required)
    """
    hunt_id = (args.get("hunt_id") or "").strip()
    if not hunt_id:
        return CommandResult(success=False, message="'hunt_id' is required for /hunt end")

    try:
        hunt = end_hunt(hunt_id)
    except HuntNotFoundError:
        return CommandResult(success=False, message=f"Hunt not found: `{hunt_id}`")
    except HuntStateError as exc:
        return CommandResult(success=False, message=str(exc))

    log.info("Ended hunt '%s' [%s]", hunt.name, hunt_id[:8])
    return CommandResult(
        success=True,
        message=f"Hunt **{hunt.name}** has ended.",
        data={"hunt": _hunt_to_dict(hunt)},
    )


# ---------------------------------------------------------------------------
# Dispatch table
# ---------------------------------------------------------------------------

_COMMANDS: dict = {
    "list":   cmd_list,
    "show":   cmd_show,
    "create": cmd_create,
    "pause":  cmd_pause,
    "resume": cmd_resume,
    "end":    cmd_end,
}

KNOWN_COMMANDS = sorted(_COMMANDS)


def dispatch(command: str, args: Optional[dict] = None) -> CommandResult:
    """
    Route a command to the appropriate handler.

    Parameters
    ----------
    command : str
        Subcommand name, e.g. "list", "create", "pause".
        Case-insensitive; leading/trailing whitespace stripped.
    args : dict, optional
        Keyword arguments for the command. Pass {} or None for commands
        that take no arguments (e.g. "list" with no status filter).

    Returns
    -------
    CommandResult
        Always returns a CommandResult — never raises.

    Example
    -------
    >>> dispatch("list", {"status": "active"})
    >>> dispatch("create", {"name": "gpu_hunt", "search_terms": ["gpu"],
    ...                     "source_sites": ["craigslist"], "max_price": 400})
    >>> dispatch("pause", {"hunt_id": "<uuid>"})
    """
    command = (command or "").strip().lower()
    args = args or {}

    log.info("Command received: %s | args: %s", command, list(args.keys()))

    handler = _COMMANDS.get(command)
    if handler is None:
        msg = (
            f"Unknown command '{command}'. "
            f"Valid commands: {', '.join(KNOWN_COMMANDS)}"
        )
        log.warning("Command failed: %s", msg)
        return CommandResult(success=False, message=msg)

    try:
        result = handler(args)
    except Exception:
        log.exception("Unhandled error in command '%s'", command)
        return CommandResult(
            success=False,
            message="An unexpected error occurred. Check the logs for details.",
        )

    if result.success:
        log.info("Command '%s' succeeded", command)
    else:
        log.warning("Command '%s' failed: %s", command, result.message)
    return result
