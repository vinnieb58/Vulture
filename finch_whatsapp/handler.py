"""Handle a single inbound WhatsApp text message."""

from __future__ import annotations

import logging

from finch_whatsapp import commands, finch_client, whatsapp_client
from finch_whatsapp.webhook import InboundTextMessage

logger = logging.getLogger(__name__)


def handle_message(message: InboundTextMessage) -> str | None:
    command = commands.parse_command(message.text)
    if command is None:
        return "Unknown command. Send 'help' for available commands."

    if isinstance(command, commands.HelpCommand):
        return commands.HELP_TEXT

    if isinstance(command, commands.PreviewCommand):
        try:
            payload = finch_client.preview(command.text)
        except finch_client.FinchApiError as exc:
            return commands.format_error(exc.detail)
        return commands.format_preview_response(payload)

    if isinstance(command, commands.AddCommand):
        try:
            payload = finch_client.cart_add(command.item)
        except finch_client.FinchApiError as exc:
            if exc.status_code == 403:
                return commands.format_cart_blocked(exc.detail)
            return commands.format_error(exc.detail)
        return commands.format_add_response(payload)

    if isinstance(command, commands.AddListCommand):
        try:
            payload = finch_client.cart_add_list(command.text)
        except finch_client.FinchApiError as exc:
            if exc.status_code == 403:
                return commands.format_cart_blocked(exc.detail)
            return commands.format_error(exc.detail)
        return commands.format_add_list_response(payload)

    if isinstance(command, commands.HistoryCommand):
        try:
            payload = finch_client.cart_history(limit=10)
        except finch_client.FinchApiError as exc:
            return commands.format_error(exc.detail)
        return commands.format_history_response(payload)

    return None


def process_inbound(message: InboundTextMessage) -> None:
    reply = handle_message(message)
    if reply is None:
        return
    try:
        whatsapp_client.send_text_message(message.sender, reply)
    except whatsapp_client.WhatsAppSendError as exc:
        logger.error("WhatsApp reply failed (status=%s)", exc.status_code)
