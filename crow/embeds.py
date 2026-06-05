"""
Discord embed builders for Crow status commands.
"""

from __future__ import annotations

import discord

from crow.system._status import StatusItem, StatusLevel, format_item_line, status_icon
from crow.system.docker import DockerStatus
from crow.system.health import PostRebootValidation, RavenHealthSummary, UptimeInfo
from crow.system.network import NetworkSummary, TailscaleStatus
from crow.system.ports import OpenService
from crow.system.services import ServiceCheck, format_service_line
from crow.system.storage import StorageMount, format_storage_line

LEVEL_COLORS: dict[StatusLevel, discord.Color] = {
    "ok": discord.Color.green(),
    "warn": discord.Color.gold(),
    "fail": discord.Color.red(),
}


def _base_embed(title: str, level: StatusLevel, *, footer: str | None = None) -> discord.Embed:
    embed = discord.Embed(title=title, color=LEVEL_COLORS.get(level, discord.Color.blurple()))
    if footer:
        embed.set_footer(text=footer)
    return embed


def _join_field_lines(lines: list[str]) -> str:
    text = "\n".join(lines)
    return text[:1024] if len(text) > 1024 else text


def build_raven_health_embed(summary: RavenHealthSummary) -> discord.Embed:
    embed = _base_embed("Raven Status", summary.overall, footer="Read-only · Crow v0.2")
    embed.add_field(name="Hostname", value=summary.hostname, inline=True)
    embed.add_field(name="Uptime", value=summary.uptime, inline=True)
    embed.add_field(name="\u200b", value="\u200b", inline=True)

    embed.add_field(
        name="Network",
        value=_join_field_lines([format_item_line(i) for i in summary.network]),
        inline=False,
    )
    embed.add_field(
        name="Storage",
        value=_join_field_lines([format_item_line(i) for i in summary.storage]),
        inline=False,
    )
    embed.add_field(
        name="Services",
        value=_join_field_lines([format_item_line(i) for i in summary.services]),
        inline=False,
    )
    embed.add_field(
        name="Vulture",
        value=_join_field_lines([format_item_line(i) for i in summary.vulture]),
        inline=False,
    )

    docker_lines = [format_item_line(summary.docker)]
    count = len(summary.docker_detail.running)
    docker_lines.append(f"{count} container{'s' if count != 1 else ''} running")
    embed.add_field(name="Docker", value=_join_field_lines(docker_lines), inline=False)

    embed.add_field(
        name="Overall",
        value=f"**{summary.overall.upper()}**",
        inline=False,
    )
    return embed


def build_services_embed(services: list[ServiceCheck], level: StatusLevel) -> discord.Embed:
    embed = _base_embed("Services", level, footer="Read-only · Crow v0.2")
    embed.add_field(
        name="Critical systemd units",
        value=_join_field_lines([format_service_line(s) for s in services]),
        inline=False,
    )
    return embed


def build_storage_embed(mounts: list[StorageMount], level: StatusLevel) -> discord.Embed:
    embed = _base_embed("Storage", level, footer="Read-only · Crow v0.2")
    embed.add_field(
        name="Mounted storage",
        value=_join_field_lines([format_storage_line(m) for m in mounts]),
        inline=False,
    )
    return embed


def build_docker_embed(status: DockerStatus, level: StatusLevel) -> discord.Embed:
    embed = _base_embed("Docker", level, footer="Read-only · Crow v0.2")
    state_label = "ACTIVE" if status.active else status.state.upper()
    embed.add_field(name="Status", value=state_label, inline=False)

    running_lines = [f"- {name}" for name in status.running] or ["- none"]
    stopped_lines = [f"- {name}" for name in status.stopped] or ["- none"]
    embed.add_field(name="Running", value=_join_field_lines(running_lines), inline=False)
    embed.add_field(name="Stopped", value=_join_field_lines(stopped_lines), inline=False)
    return embed


def build_tailscale_embed(status: TailscaleStatus, level: StatusLevel) -> discord.Embed:
    embed = _base_embed("Tailscale", level, footer="Read-only · Crow v0.2")
    embed.add_field(
        name="Connected",
        value="YES" if status.connected else "NO",
        inline=True,
    )
    embed.add_field(name="IPv4", value=status.ipv4 or "n/a", inline=True)
    embed.add_field(name="Hostname", value=status.hostname or "n/a", inline=True)
    return embed


def build_network_embed(summary: NetworkSummary, level: StatusLevel) -> discord.Embed:
    embed = _base_embed("Network", level, footer="Read-only · Crow v0.2")
    internet_icon = status_icon("ok" if summary.internet_reachable else "warn")
    lines = [
        f"Internet:\n{internet_icon} {'Reachable' if summary.internet_reachable else 'Unreachable'}",
        f"LAN:\n{summary.lan_ipv4 or 'n/a'}",
        f"Tailscale:\n{summary.tailscale_ipv4 or 'n/a'}",
    ]
    embed.add_field(name="Summary", value=_join_field_lines(lines), inline=False)
    return embed


def build_reboot_embed(validation: PostRebootValidation) -> discord.Embed:
    embed = _base_embed(
        "Post-Reboot Validation",
        validation.overall,
        footer="Read-only · Crow v0.2",
    )
    embed.add_field(
        name="Checks",
        value=_join_field_lines([format_item_line(c) for c in validation.checks]),
        inline=False,
    )
    embed.add_field(name="Overall", value=f"**{validation.overall.upper()}**", inline=False)
    return embed


def build_uptime_embed(info: UptimeInfo) -> discord.Embed:
    embed = _base_embed("Uptime", "ok", footer="Read-only · Crow v0.2")
    embed.add_field(name="Host", value=info.host_uptime, inline=False)
    embed.add_field(name="Last Boot", value=info.last_boot or "unknown", inline=False)
    return embed


def build_ports_embed(services: list[OpenService]) -> discord.Embed:
    any_listening = any(s.listening for s in services)
    level: StatusLevel = "ok" if any_listening else "warn"
    embed = _base_embed("Open Services", level, footer="Read-only · Crow v0.2")
    lines = [f"{s.port:<4} {s.label}" for s in services if s.listening]
    if not lines:
        lines = ["(no expected ports listening)"]
    embed.add_field(name="Listening", value=_join_field_lines(lines), inline=False)
    return embed
