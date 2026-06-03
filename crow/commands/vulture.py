"""
Vulture health slash command.
"""

from __future__ import annotations

import discord

from crow.checks import vulture as vulture_checks
from crow.formatting import truncate


def register_vulture_commands(tree, *, max_message_len: int = 1900) -> None:
    @tree.command(
        name="check_vulture",
        description="Vulture DB, logs, and scheduler health (read-only).",
    )
    async def check_vulture(interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        health = vulture_checks.get_vulture_health()
        text = truncate(
            vulture_checks.format_vulture_health_message(health),
            max_message_len,
        )
        await interaction.followup.send(text, ephemeral=True)

    @tree.command(
        name="check_services",
        description="Process/tmux visibility for bot and scheduler (read-only).",
    )
    async def check_services(interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        from crow.checks import services

        statuses = services.get_all_service_statuses()
        text = truncate(services.format_services_message(statuses), max_message_len)
        await interaction.followup.send(text, ephemeral=True)
