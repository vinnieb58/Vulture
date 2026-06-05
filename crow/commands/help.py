"""
/crow_help — Crow command list (v0.2 /check group + legacy v0.1).
"""

from __future__ import annotations

import discord
from discord import app_commands

from crow import __version__
from crow.formatting import truncate

CHECK_SUBCOMMANDS: tuple[str, ...] = (
    "raven",
    "services",
    "storage",
    "docker",
    "tailscale",
    "network",
    "reboot",
    "uptime",
    "ports",
    "logs",
)


def crow_help_text() -> str:
    check_lines = "\n".join(
        f"• `/check {name}` — {_check_blurb(name)}"
        for name in CHECK_SUBCOMMANDS
    )
    return (
        f"**Crow v{__version__}** — read-only Aviary ops console\n\n"
        "**Raven / system checks (`/check` group)**\n"
        f"{check_lines}\n\n"
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


def _check_blurb(name: str) -> str:
    blurbs = {
        "raven": "high-level Raven health summary (embed)",
        "services": "SSH, Tailscale, Samba, Docker, Vulture units",
        "storage": "expected mounts and usage",
        "docker": "daemon status and container names",
        "tailscale": "Tailscale connection and IPv4",
        "network": "internet, LAN, and Tailscale IPs",
        "reboot": "post-reboot validation checklist",
        "uptime": "host uptime and last boot",
        "ports": "summarized listening services",
        "logs": "recent warning/error summary from known log sources (sanitized)",
    }
    return blurbs.get(name, name)


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
