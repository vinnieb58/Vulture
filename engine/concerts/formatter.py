"""Discord-oriented formatting for concert results."""

from __future__ import annotations

from engine.concerts.dedupe import MergedConcertEvent
from engine.concerts.models import ConcertWatch
from engine.concerts.search import DEFAULT_DISPLAY_LIMIT, SearchResult, format_starts_at_display
from engine.concerts.stats import SearchStats


def format_event_card(event: MergedConcertEvent, *, index: int | None = None) -> str:
    """Compact single-line card with ticket URL on second line."""
    prefix = f"{index}. " if index is not None else ""
    city_state = ", ".join(part for part in (event.city, event.state) if part) or "TBA"
    venue = event.venue or "TBA"
    date = format_starts_at_display(event.starts_at)
    url = event.ticket_url or "—"
    return (
        f"{prefix}**{event.artist_or_title}** · {date} · {venue}, {city_state} · {event.source_label}\n"
        f"   {url}"
    )


def format_provider_summary(stats: SearchStats, *, total_events: int, displayed: int) -> str:
    lines = [
        f"Ticketmaster returned **{stats.ticketmaster_returned}**",
        f"SeatGeek returned **{stats.seatgeek_returned}**",
        f"Displayed **{displayed}** of **{total_events}** after filtering/dedupe",
    ]
    if stats.noise_hidden:
        lines.append(
            f"_{stats.noise_hidden} noisy SeatGeek result(s) hidden (generic concert taxonomy)_"
        )
    return "\n".join(lines)


def format_search_results(
    result: SearchResult,
    *,
    max_cards: int = DEFAULT_DISPLAY_LIMIT,
) -> str:
    stats = result.stats
    if not result.events:
        parts = ["No concerts found matching your filters."]
        parts.append("")
        parts.append(format_provider_summary(stats, total_events=0, displayed=0))
        if result.provider_notes:
            notes = "\n".join(f"• {n}" for n in result.provider_notes)
            parts.append(f"\n**Provider notes:**\n{notes}")
        return "\n".join(parts)

    shown = result.events[:max_cards]
    cards = [format_event_card(event, index=i + 1) for i, event in enumerate(shown)]

    header = f"**{len(result.events)} concert(s) found**"
    if len(result.events) > max_cards:
        header += f" (showing top {max_cards})"

    parts = [
        header,
        "",
        format_provider_summary(stats, total_events=len(result.events), displayed=len(shown)),
        "",
        "\n".join(cards),
    ]

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
    label_parts: list[str] = []
    if watch.artist_query:
        label_parts.append(watch.artist_query)
    elif watch.genre:
        label_parts.append(watch.genre)
    geo = watch.area or watch.city or "anywhere"
    return (
        f"#{watch.id} **{' · '.join(label_parts) or 'watch'}** — {geo} · {watch.days_forward}d"
    )


def format_watches_list(watches: list[ConcertWatch]) -> str:
    if not watches:
        return "No active concert watches."
    lines = [format_watch_summary(w) for w in watches]
    return f"**Active watches ({len(watches)})**\n" + "\n".join(lines)


HELP_TEXT = """**Vulture Concerts — /concert commands**

`/concert search` — Search concerts across Ticketmaster + SeatGeek
`/concert watch` — Save a watch (alerts on new matches)
`/concert watches` — List active watches
`/concert pause` — Pause a watch by ID (stops alerts, keeps history)
`/concert unwatch` — Remove a watch by ID
`/concert test` — Validate config and sample queries
`/concert help` — This help text

**Typed options** (recommended):
`artist`, `genre`, `area`, `city`, `state`, `radius`, `days`, `force`
Area presets: houston, dallas, austin, san antonio, east texas, louisiana, texas, nationwide

**Examples:**
```
/concert search artist:Three Days Grace area:houston days:180
/concert watch genre:rock area:louisiana days:365
/concert pause watch_id:3
/concert unwatch watch_id:3
```

Search shows top 10 results with provider summary. Noisy SeatGeek generic rows are hidden on broad genre searches.
Broad rock watches include Rock/Metal/Alternative; nationwide genre-only blocked unless `force:true`.
"""
