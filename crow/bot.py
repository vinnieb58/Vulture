"""
Crow integration helpers for the Vulture Discord bot.
"""

from __future__ import annotations

from crow.commands import register_crow_commands


def setup_crow(bot, *, max_message_len: int = 1900) -> None:
    """
    Register Crow v0.1 slash commands on an existing discord.Client / CommandTree.

    Called from discord_bot.py so hunt commands and Crow share one runtime.
    """
    register_crow_commands(bot.tree, max_message_len=max_message_len)
