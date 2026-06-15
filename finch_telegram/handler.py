"""Handle a single inbound Telegram text message."""

from __future__ import annotations

import logging

from finch_telegram import commands, config, finch_client, telegram_client

logger = logging.getLogger(__name__)


def handle_message(message: telegram_client.InboundTextMessage) -> str | None:
    command = commands.parse_command(message.text)
    if command is None:
        normalized = commands.normalize_message(message.text)
        if normalized.lower() == "preview":
            return "Usage: preview eggs, milk"
        return "Unknown command. Send 'help' for available commands."

    if isinstance(command, commands.StartCommand):
        return commands.START_TEXT

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

    if isinstance(command, commands.CartCommand):
        try:
            payload = finch_client.cart_current()
        except finch_client.FinchApiError as exc:
            return commands.format_error(exc.detail)
        return commands.format_cart_response(payload)

    return None


def process_inbound(message: telegram_client.InboundTextMessage) -> None:
    if not config.whitelist_configured():
        logger.info(
            "Telegram message from user_id=%s (whitelist empty — add to FINCH_TELEGRAM_ALLOWED_USER_IDS)",
            message.user_id,
        )
    elif not config.is_allowed_user(message.user_id):
        logger.warning(
            "Rejected Telegram message from non-whitelisted user_id=%s",
            message.user_id,
        )
        return

    reply = handle_message(message)
    if reply is None:
        return
    try:
        telegram_client.send_text_message(message.chat_id, reply)
    except telegram_client.TelegramSendError:
        return
