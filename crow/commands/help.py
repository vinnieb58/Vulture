"""
/crow_help — Crow v0.1 command list.
"""

from __future__ import annotations

import discord
from discord import app_commands

from crow import __version__
from crow.formatting import truncate


def crow_help_text() -> str:
    return (
        f"**Crow v{__version__}** — read-only Aviary ops console\n\n"
        "**Commands**\n"
        "• `/raven_status` — host summary (hostname, uptime, memory, disk, load)\n"
        "• `/check_disk` — disk usage for `/` and mounted storage\n"
        "• `/check_memory` — memory usage\n"
        "• `/check_services` — bot/scheduler systemd + process visibility (no restarts)\n"
        "• `/check_vulture` — DB, logs, scheduler health (no hunts / DB writes)\n"
        "• `/crow_help` — this message\n\n"
        "Vulture hunt commands (`/hunt`, `/hunt_list`, …) are unchanged.\n\n"
        "_v0.1 is read-only: no restarts, log tail, Docker control, or admin shell._"
    )


def register_help_command(tree, *, max_message_len: int = 1900) -> None:
    @tree.command(
        name="crow_help",
        description="Crow v0.1 read-only command list and notes.",
    )
    async def crow_help(interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        await interaction.followup.send(
            truncate(crow_help_text(), max_message_len),
            ephemeral=True,
        )
