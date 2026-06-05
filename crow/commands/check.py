"""
/check command group — Raven health and reboot awareness (read-only).
"""

from __future__ import annotations

import discord
from discord import app_commands

from crow.embeds import (
    build_docker_embed,
    build_logs_embed,
    build_network_embed,
    build_ports_embed,
    build_raven_health_embed,
    build_reboot_embed,
    build_services_embed,
    build_storage_embed,
    build_tailscale_embed,
    build_uptime_embed,
)
from crow.system._status import overall_from_items
from crow.system.docker import docker_level, get_docker_status
from crow.system.health import (
    get_post_reboot_validation,
    get_raven_health_summary,
    get_uptime_info,
)
from crow.system.network import get_network_summary, get_tailscale_status
from crow.system.logs import get_logs_summary
from crow.system.ports import get_open_services_summary
from crow.system.services import get_critical_service_statuses, service_to_status_item
from crow.system.storage import get_storage_summary, storage_to_status_item


def register_check_commands(tree, *, max_message_len: int = 1900) -> None:
    """Register /check slash command group (read-only Raven ops)."""
    _ = max_message_len  # embeds stay within Discord limits

    check = app_commands.Group(
        name="check",
        description="Raven health and reboot checks (read-only).",
    )

    @check.command(name="raven", description="High-level Raven health summary.")
    async def check_raven(interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        summary = get_raven_health_summary(post_reboot=True)
        embed = build_raven_health_embed(summary)
        await interaction.followup.send(embed=embed, ephemeral=True)

    @check.command(name="services", description="Critical systemd services on Raven.")
    async def check_services(interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        services = get_critical_service_statuses()
        level = overall_from_items([service_to_status_item(s) for s in services])
        embed = build_services_embed(services, level)
        await interaction.followup.send(embed=embed, ephemeral=True)

    @check.command(name="storage", description="Mounted storage on Raven.")
    async def check_storage(interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        mounts = get_storage_summary()
        level = overall_from_items([storage_to_status_item(m) for m in mounts])
        embed = build_storage_embed(mounts, level)
        await interaction.followup.send(embed=embed, ephemeral=True)

    @check.command(name="docker", description="Docker daemon and container status.")
    async def check_docker(interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        status = get_docker_status()
        embed = build_docker_embed(status, docker_level(status))
        await interaction.followup.send(embed=embed, ephemeral=True)

    @check.command(name="tailscale", description="Tailscale connection status.")
    async def check_tailscale(interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        status = get_tailscale_status()
        level = "ok" if status.connected else "warn"
        embed = build_tailscale_embed(status, level)
        await interaction.followup.send(embed=embed, ephemeral=True)

    @check.command(name="network", description="Network diagnostics summary.")
    async def check_network(interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        summary = get_network_summary()
        level = "ok" if summary.internet_reachable else "warn"
        embed = build_network_embed(summary, level)
        await interaction.followup.send(embed=embed, ephemeral=True)

    @check.command(name="reboot", description="Post-reboot validation checklist.")
    async def check_reboot(interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        validation = get_post_reboot_validation()
        embed = build_reboot_embed(validation)
        await interaction.followup.send(embed=embed, ephemeral=True)

    @check.command(name="uptime", description="Host uptime and last boot time.")
    async def check_uptime(interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        info = get_uptime_info()
        embed = build_uptime_embed(info)
        await interaction.followup.send(embed=embed, ephemeral=True)

    @check.command(name="ports", description="Summarized listening services.")
    async def check_ports(interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        services = get_open_services_summary()
        embed = build_ports_embed(services)
        await interaction.followup.send(embed=embed, ephemeral=True)

    @check.command(
        name="logs",
        description="Recent Raven/Vulture/Crow log summary (sanitized, read-only).",
    )
    async def check_logs(interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        summary = get_logs_summary()
        embed = build_logs_embed(summary)
        await interaction.followup.send(embed=embed, ephemeral=True)

    tree.add_command(check)
