"""Discord-oriented formatting for concert results."""

from __future__ import annotations

from engine.concerts.dedupe import MergedConcertEvent
from engine.concerts.models import ConcertWatch
from engine.concerts.search import SearchResult, format_starts_at_display


def format_event_card(event: MergedConcertEvent, *, index: int | None = None) -> str:
    prefix = f"**{index}.** " if index is not None else ""
    city_state = ", ".join(part for part in (event.city, event.state) if part)
    lines = [
        f"{prefix}**{event.artist_or_title}**",
        f"Date: {format_starts_at_display(event.starts_at)}",
        f"Venue: {event.venue or 'TBA'}",
        f"City: {city_state or 'TBA'}",
        f"Ticket: {event.ticket_url or '—'}",
        f"Source: {event.source_label}",
    ]
    return "\n".join(lines)


def format_search_results(result: SearchResult, *, max_cards: int = 15) -> str:
    if not result.events:
        notes = "\n".join(f"• {n}" for n in result.provider_notes) if result.provider_notes else ""
        body = "No concerts found matching your filters."
        if notes:
            body += f"\n\n**Provider notes:**\n{notes}"
        return body

    cards = [
        format_event_card(event, index=i + 1)
        for i, event in enumerate(result.events[:max_cards])
    ]
    header = f"**{len(result.events)} concert(s) found**"
    if len(result.events) > max_cards:
        header += f" (showing first {max_cards})"

    parts = [header, ""]
    parts.append("\n\n".join(cards))

    if result.provider_notes:
        notes = "\n".join(f"• {n}" for n in result.provider_notes)
        parts.append(f"\n**Provider notes:**\n{notes}")

    return "\n".join(parts)


def format_alert_message(event: MergedConcertEvent) -> str:
    city_state = ", ".join(part for part in (event.city, event.state) if part)
    return (
        "🎵 **New Concert Found**\n"
        f"Artist: {event.artist_or_title}\n"
        f"Date: {format_starts_at_display(event.starts_at)}\n"
        f"Venue: {event.venue or 'TBA'}\n"
        f"City: {city_state or 'TBA'}\n"
        f"Ticket: {event.ticket_url or '—'}\n"
        f"Source: {event.source_label}"
    )


def format_watch_summary(watch: ConcertWatch) -> str:
    parts = [f"**Watch #{watch.id}**"]
    if watch.artist_query:
        parts.append(f"artist: {watch.artist_query}")
    if watch.genre:
        parts.append(f"genre: {watch.genre}")
    if watch.area:
        parts.append(f"area: {watch.area}")
    if watch.city:
        loc = watch.city
        if watch.state:
            loc += f", {watch.state}"
        parts.append(f"city: {loc}")
    if watch.radius_miles:
        parts.append(f"radius: {watch.radius_miles}mi")
    parts.append(f"days: {watch.days_forward}")
    parts.append(f"active: {'yes' if watch.active else 'no'}")
    return " | ".join(parts)


def format_watches_list(watches: list[ConcertWatch]) -> str:
    if not watches:
        return "No active concert watches."
    lines = [format_watch_summary(w) for w in watches]
    return f"**Active watches ({len(watches)})**\n\n" + "\n".join(lines)


HELP_TEXT = """**Vulture Concerts — /concert commands**

`/concert search` — Search concerts across Ticketmaster + SeatGeek
`/concert watch` — Save a watch (alerts on new matches)
`/concert watches` — List active watches
`/concert test` — Dry-run sample searches (no API calls for credentials check)
`/concert help` — This help text

**Filter syntax** (combine as needed):
```
artist:"Three Days Grace" city:"Houston" days:180
artist:"Breaking Benjamin" area:"texas" days:365
genre:"rock" area:"houston" days:180
artist:"Disturbed" area:"nationwide" days:365
```

**Area presets:** houston, dallas, austin, san antonio, east texas, louisiana, texas, nationwide

**Explicit geo:**
```
city:"Houston" state:"TX" radius:75
city:"New Orleans" state:"LA" radius:100
```

Broad rock watches include Rock/Metal/Alternative and exclude Sports/Comedy/Theater/Country/R&B/Pop.
Nationwide genre-only watches are blocked unless `force:true` is added.
"""
