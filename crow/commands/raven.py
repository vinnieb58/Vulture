"""
Raven host slash commands — status, disk, memory.
"""

from __future__ import annotations

import discord
from discord import app_commands

from crow.checks import system
from crow.formatting import truncate


def register_raven_commands(tree, *, max_message_len: int = 1900) -> None:
    @tree.command(
        name="raven_status",
        description="Concise Raven host status (read-only).",
    )
    async def raven_status(interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        summary = system.get_raven_status_summary()
        text = truncate(system.format_raven_status_message(summary), max_message_len)
        await interaction.followup.send(text, ephemeral=True)

    @tree.command(
        name="check_disk",
        description="Disk usage for / and mounted paths (read-only).",
    )
    async def check_disk(interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        entries = system.get_disk_summary()
        text = truncate(system.format_disk_check_message(entries), max_message_len)
        await interaction.followup.send(text, ephemeral=True)

    @tree.command(
        name="check_memory",
        description="Memory usage summary (read-only).",
    )
    async def check_memory(interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        mem = system.get_memory_info()
        text = truncate(system.format_memory_check_message(mem), max_message_len)
        await interaction.followup.send(text, ephemeral=True)
