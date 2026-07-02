"""Discord slash command registration for Vulture Concerts."""

from __future__ import annotations

from typing import Optional

import discord
from discord import app_commands

from discord_messages import paginate_command_message, send_paginated_followup
from engine.concerts.areas import SUPPORTED_AREAS
from engine.concerts.command_router import ConcertCommandResult, dispatch_concert

AREA_CHOICES = [
    app_commands.Choice(name="Houston", value="houston"),
    app_commands.Choice(name="Dallas", value="dallas"),
    app_commands.Choice(name="Austin", value="austin"),
    app_commands.Choice(name="San Antonio", value="san antonio"),
    app_commands.Choice(name="East Texas", value="east texas"),
    app_commands.Choice(name="Louisiana", value="louisiana"),
    app_commands.Choice(name="Texas", value="texas"),
    app_commands.Choice(name="Nationwide", value="nationwide"),
]

FILTER_OPTION_NAMES = (
    "query",
    "artist",
    "genre",
    "area",
    "city",
    "state",
    "radius",
    "days",
    "force",
)


async def _send_result(
    interaction: discord.Interaction,
    result: ConcertCommandResult,
    *,
    command: str,
) -> None:
    pages = paginate_command_message(result, command=f"concert {command}")
    await send_paginated_followup(interaction, pages, ephemeral=True)


def _filter_args(
    *,
    query: Optional[str] = None,
    artist: Optional[str] = None,
    genre: Optional[str] = None,
    area: Optional[str] = None,
    city: Optional[str] = None,
    state: Optional[str] = None,
    radius: Optional[int] = None,
    days: Optional[int] = None,
    force: bool = False,
) -> dict:
    return {
        "query": query,
        "artist": artist,
        "genre": genre,
        "area": area,
        "city": city,
        "state": state,
        "radius": radius,
        "days": days,
        "force": force,
    }


def register_concert_commands(tree: app_commands.CommandTree) -> None:
    """Register /concert command group on the bot command tree."""
    concert = app_commands.Group(
        name="concert",
        description="Vulture Concerts — search and watch concert alerts",
    )

    filter_describe = app_commands.describe(
        query=(
            "Optional freeform filters, e.g. "
            'artist:"Three Days Grace" area:"houston" days:180'
        ),
        artist="Artist or band name",
        genre='Genre for broad watches (e.g. "rock")',
        area="Area preset (typed options override freeform query)",
        city="City name for explicit geo search",
        state="US state code (e.g. TX, LA)",
        radius="Search radius in miles",
        days="Days forward to search (default 180)",
        force="Allow noisy broad nationwide genre searches",
    )
    area_choices = app_commands.choices(area=AREA_CHOICES)

    @concert.command(
        name="search",
        description="Search concerts across Ticketmaster and SeatGeek.",
    )
    @filter_describe
    @area_choices
    async def concert_search(
        interaction: discord.Interaction,
        artist: Optional[str] = None,
        genre: Optional[str] = None,
        area: Optional[str] = None,
        city: Optional[str] = None,
        state: Optional[str] = None,
        radius: Optional[int] = None,
        days: Optional[int] = None,
        force: bool = False,
        query: Optional[str] = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        result = dispatch_concert(
            "search",
            _filter_args(
                query=query,
                artist=artist,
                genre=genre,
                area=area,
                city=city,
                state=state,
                radius=radius,
                days=days,
                force=force,
            ),
        )
        await _send_result(interaction, result, command="search")

    @concert.command(
        name="watch",
        description="Create a concert watch for new-show alerts.",
    )
    @filter_describe
    @area_choices
    async def concert_watch(
        interaction: discord.Interaction,
        artist: Optional[str] = None,
        genre: Optional[str] = None,
        area: Optional[str] = None,
        city: Optional[str] = None,
        state: Optional[str] = None,
        radius: Optional[int] = None,
        days: Optional[int] = None,
        force: bool = False,
        query: Optional[str] = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        result = dispatch_concert(
            "watch",
            _filter_args(
                query=query,
                artist=artist,
                genre=genre,
                area=area,
                city=city,
                state=state,
                radius=radius,
                days=days,
                force=force,
            ),
        )
        await _send_result(interaction, result, command="watch")

    @concert.command(name="watches", description="List active concert watches.")
    async def concert_watches(interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        result = dispatch_concert("watches", {})
        await _send_result(interaction, result, command="watches")

    @concert.command(
        name="test",
        description="Validate concert config and sample query parsing.",
    )
    @app_commands.describe(
        live="Run one live sample search when API credentials are configured.",
    )
    async def concert_test(
        interaction: discord.Interaction,
        live: bool = False,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        result = dispatch_concert("test", {"live": live})
        await _send_result(interaction, result, command="test")

    @concert.command(name="help", description="Show Vulture Concerts command help.")
    async def concert_help(interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        result = dispatch_concert("help", {})
        await _send_result(interaction, result, command="help")

    tree.add_command(concert)


def area_choice_values() -> frozenset[str]:
    """Supported area preset values exposed as Discord choices."""
    return frozenset(choice.value for choice in AREA_CHOICES)


def assert_supported_area_choices() -> None:
    """Keep Discord area choices in sync with engine presets."""
    missing = SUPPORTED_AREAS - area_choice_values()
    extra = area_choice_values() - SUPPORTED_AREAS
    if missing or extra:
        raise RuntimeError(
            f"Discord area choices out of sync: missing={sorted(missing)} extra={sorted(extra)}"
        )
