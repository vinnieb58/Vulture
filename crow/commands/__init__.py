"""Crow slash command registration."""

from crow.commands import check, raven, vulture
from crow.commands.help import register_help_command


def register_crow_commands(tree, *, max_message_len: int = 1900) -> None:
    """Register all Crow slash commands on the given app_commands tree."""
    check.register_check_commands(tree, max_message_len=max_message_len)
    raven.register_raven_commands(tree, max_message_len=max_message_len)
    vulture.register_vulture_commands(tree, max_message_len=max_message_len)
    register_help_command(tree, max_message_len=max_message_len)
