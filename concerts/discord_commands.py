"""Discord slash command registration for Vulture Concerts."""

from __future__ import annotations

import discord
from discord import app_commands

from discord_messages import paginate_command_message, send_paginated_followup
from engine.concerts.command_router import ConcertCommandResult, dispatch_concert


async def _send_result(
    interaction: discord.Interaction,
    result: ConcertCommandResult,
    *,
    command: str,
) -> None:
    pages = paginate_command_message(result, command=f"concert {command}")
    await send_paginated_followup(interaction, pages, ephemeral=True)


def register_concert_commands(tree: app_commands.CommandTree) -> None:
    """Register /concert command group on the bot command tree."""
    concert = app_commands.Group(
        name="concert",
        description="Vulture Concerts — search and watch concert alerts",
    )

    @concert.command(name="search", description="Search concerts across Ticketmaster and SeatGeek.")
    @app_commands.describe(
        query=(
            'Filters, e.g. artist:"Three Days Grace" area:"houston" days:180 '
            'or genre:"rock" area:"louisiana" days:365'
        ),
    )
    async def concert_search(interaction: discord.Interaction, query: str) -> None:
        await interaction.response.defer(ephemeral=True)
        result = dispatch_concert("search", {"query": query})
        await _send_result(interaction, result, command="search")

    @concert.command(name="watch", description="Create a concert watch for new-show alerts.")
    @app_commands.describe(
        query=(
            'Watch filters, e.g. artist:"Shinedown" area:"houston" days:365 '
            'or genre:"rock" area:"louisiana" days:365'
        ),
    )
    async def concert_watch(interaction: discord.Interaction, query: str) -> None:
        await interaction.response.defer(ephemeral=True)
        result = dispatch_concert("watch", {"query": query})
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
