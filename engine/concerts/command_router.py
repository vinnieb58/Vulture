"""Discord-agnostic command handlers for Vulture Concerts."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Optional

from engine.concerts.formatter import (
    HELP_TEXT,
    format_search_results,
    format_watches_list,
)
from engine.concerts.nightingale import export_handoffs
from engine.concerts.query_parser import FilterValidationError, parse_and_validate
from engine.concerts.repository import (
    create_watch,
    list_watches,
    seed_bootstrap_alerts,
    upsert_provider_events,
)
from engine.concerts.search import SearchCriteria, search_concerts

log = logging.getLogger(__name__)


@dataclass
class ConcertCommandResult:
    success: bool
    message: str
    data: Optional[dict] = field(default=None)


SAMPLE_QUERIES = [
    'artist:"Three Days Grace" city:"Houston" days:180',
    'genre:"rock" area:"houston" days:365',
    'artist:"Disturbed" area:"nationwide" days:365',
    'genre:"rock" area:"louisiana" days:365',
    'genre:"rock" area:"east texas" days:365',
]


def cmd_search(args: dict) -> ConcertCommandResult:
    query = (args.get("query") or "").strip()
    if not query:
        return ConcertCommandResult(
            success=False,
            message="Provide filters, e.g. `artist:\"Three Days Grace\" area:\"houston\" days:180`",
        )
    try:
        criteria = parse_and_validate(query)
    except (FilterValidationError, ValueError) as exc:
        return ConcertCommandResult(success=False, message=str(exc))

    result = search_concerts(criteria)
    upsert_provider_events(result.events)
    handoffs = export_handoffs(result.events)

    return ConcertCommandResult(
        success=True,
        message=format_search_results(result),
        data={
            "event_count": len(result.events),
            "queries_run": result.queries_run,
            "nightingale_handoffs": handoffs,
        },
    )


def cmd_watch(args: dict) -> ConcertCommandResult:
    query = (args.get("query") or "").strip()
    if not query:
        return ConcertCommandResult(
            success=False,
            message="Provide watch filters, e.g. `artist:\"Shinedown\" area:\"houston\" days:365`",
        )
    try:
        criteria = parse_and_validate(query)
    except (FilterValidationError, ValueError) as exc:
        return ConcertCommandResult(success=False, message=str(exc))

    watch = create_watch(criteria)
    result = search_concerts(criteria)
    upsert_provider_events(result.events)
    seeded = seed_bootstrap_alerts(watch.id, result.events)

    from engine.concerts.formatter import format_watch_summary

    msg = (
        f"Watch saved.\n\n{format_watch_summary(watch)}\n\n"
        f"Initial search: {len(result.events)} matching event(s) found.\n"
        f"Seeded {seeded} existing event(s) in alert ledger — "
        f"only newly discovered shows will alert."
    )
    return ConcertCommandResult(
        success=True,
        message=msg,
        data={
            "watch_id": watch.id,
            "initial_event_count": len(result.events),
            "bootstrap_seeded": seeded,
        },
    )


def cmd_watches(args: dict) -> ConcertCommandResult:
    watches = list_watches(active_only=True)
    return ConcertCommandResult(
        success=True,
        message=format_watches_list(watches),
        data={"watches": [w.__dict__ for w in watches]},
    )


def cmd_test(args: dict) -> ConcertCommandResult:
    """Validate credentials and run sample query parsing (optional live search)."""
    tm_key = bool(os.getenv("TICKETMASTER_API_KEY", "").strip())
    sg_key = bool(os.getenv("SEATGEEK_CLIENT_ID", "").strip())

    lines = [
        "**Concert provider credentials**",
        f"Ticketmaster (TICKETMASTER_API_KEY): {'configured' if tm_key else 'NOT SET'}",
        f"SeatGeek (SEATGEEK_CLIENT_ID): {'configured' if sg_key else 'NOT SET'}",
        "",
        "**Sample query validation**",
    ]

    parsed_ok = 0
    for sample in SAMPLE_QUERIES:
        try:
            parse_and_validate(sample)
            lines.append(f"✓ {sample}")
            parsed_ok += 1
        except (FilterValidationError, ValueError) as exc:
            lines.append(f"✗ {sample} — {exc}")

    lines.append(f"\n{parsed_ok}/{len(SAMPLE_QUERIES)} sample queries valid.")

    if args.get("live") and (tm_key or sg_key):
        lines.append("\n**Live sample search** (first valid query with credentials):")
        for sample in SAMPLE_QUERIES:
            try:
                criteria = parse_and_validate(sample)
                result = search_concerts(criteria)
                lines.append(
                    f"{sample}\n→ {len(result.events)} event(s), "
                    f"{result.queries_run} queries"
                )
                if result.provider_notes:
                    for note in result.provider_notes[:3]:
                        lines.append(f"  • {note}")
                break
            except (FilterValidationError, ValueError):
                continue

    return ConcertCommandResult(
        success=True,
        message="\n".join(lines),
        data={"ticketmaster": tm_key, "seatgeek": sg_key},
    )


def cmd_help(args: dict) -> ConcertCommandResult:
    return ConcertCommandResult(success=True, message=HELP_TEXT)


_COMMANDS = {
    "search": cmd_search,
    "watch": cmd_watch,
    "watches": cmd_watches,
    "test": cmd_test,
    "help": cmd_help,
}

KNOWN_CONCERT_COMMANDS = sorted(_COMMANDS)


def dispatch_concert(command: str, args: Optional[dict] = None) -> ConcertCommandResult:
    command = (command or "").strip().lower()
    args = args or {}
    handler = _COMMANDS.get(command)
    if handler is None:
        return ConcertCommandResult(
            success=False,
            message=(
                f"Unknown concert command '{command}'. "
                f"Valid: {', '.join(KNOWN_CONCERT_COMMANDS)}"
            ),
        )
    try:
        return handler(args)
    except Exception:
        log.exception("Unhandled error in concert command '%s'", command)
        return ConcertCommandResult(
            success=False,
            message="An unexpected error occurred. Check the logs for details.",
        )
