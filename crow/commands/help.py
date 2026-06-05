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
        "**Raven health (`/check` group)**\n"
        "• `/check raven` — high-level Raven health summary (embed)\n"
        "• `/check services` — SSH, Tailscale, Samba, Docker, Vulture units\n"
        "• `/check storage` — expected mounts and usage\n"
        "• `/check docker` — daemon status and container names\n"
        "• `/check tailscale` — Tailscale connection and IPv4\n"
        "• `/check network` — internet, LAN, and Tailscale IPs\n"
        "• `/check reboot` — post-reboot validation checklist\n"
        "• `/check uptime` — host uptime and last boot\n"
        "• `/check ports` — summarized listening services\n\n"
        "**Legacy v0.1 commands**\n"
        "• `/raven_status` — host summary (hostname, uptime, memory, disk, load)\n"
        "• `/check_disk` — disk usage for `/` and mounted storage\n"
        "• `/check_memory` — memory usage\n"
        "• `/check_services` — bot/scheduler systemd + process visibility\n"
        "• `/check_vulture` — DB, logs, scheduler health\n"
        "• `/crow_help` — this message\n\n"
        "Vulture hunt commands (`/hunt`, `/hunt_list`, …) are unchanged.\n\n"
        "_Read-only: no restarts, Docker control, reboot, or admin shell._"
    )


def register_help_command(tree, *, max_message_len: int = 1900) -> None:
    @tree.command(
        name="crow_help",
        description="Crow read-only command list and notes.",
    )
    async def crow_help(interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        await interaction.followup.send(
            truncate(crow_help_text(), max_message_len),
            ephemeral=True,
        )
