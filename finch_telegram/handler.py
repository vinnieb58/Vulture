"""Handle a single inbound Telegram text message."""

from __future__ import annotations

import logging

from finch_telegram import commands, config, finch_client, telegram_client

logger = logging.getLogger(__name__)


def _chat_key(message: telegram_client.InboundTextMessage) -> str:
    return finch_client.telegram_chat_key(message.chat_id)


def _format_finch_api_error(exc: finch_client.FinchApiError) -> str:
    logger.error(
        "Finch API error: %s %s -> %s %s",
        exc.method or "?",
        exc.path or "?",
        exc.status_code,
        exc.detail,
    )
    return commands.format_api_error(
        status_code=exc.status_code,
        detail=exc.detail,
        method=exc.method,
        path=exc.path,
    )


def _handle_pending_reply(
    message: telegram_client.InboundTextMessage,
    command: commands.ChooseReplyCommand
    | commands.CancelPendingCommand
    | commands.SearchPendingCommand
    | commands.MorePendingCommand
    | commands.BackPendingCommand,
) -> str:
    chat_key = _chat_key(message)
    try:
        if isinstance(command, commands.CancelPendingCommand):
            payload = finch_client.pending_cancel(chat_key)
            return commands.format_cancel_pending_response(payload)
        if isinstance(command, commands.SearchPendingCommand):
            payload = finch_client.pending_search(chat_key, command.query)
            if payload.get("needs_choice"):
                return commands.format_needs_choice_response(payload)
            return commands.format_error("Unexpected search response.")
        if isinstance(command, commands.MorePendingCommand):
            payload = finch_client.pending_more(chat_key)
            if payload.get("needs_choice"):
                return commands.format_needs_choice_response(payload)
            return commands.format_error(str(payload.get("message") or "No more results."))
        if isinstance(command, commands.BackPendingCommand):
            payload = finch_client.pending_back(chat_key)
            if payload.get("needs_choice"):
                return commands.format_needs_choice_response(payload)
            return commands.format_error(str(payload.get("message") or "Cannot go back."))
        payload = finch_client.cart_choose(
            chat_key,
            command.selection,
            prefer=command.prefer,
            source="telegram",
        )
    except finch_client.FinchApiError as exc:
        if exc.status_code == 403:
            return commands.format_cart_blocked(exc.detail)
        return _format_finch_api_error(exc)
    return commands.format_choose_response(payload)


def handle_message(message: telegram_client.InboundTextMessage) -> str | None:
    pending = commands.parse_pending_reply(message.text)
    if pending is not None:
        return _handle_pending_reply(message, pending)

    command = commands.parse_command(message.text)
    if command is None:
        normalized = commands.normalize_message(message.text)
        if normalized.lower() == "preview":
            return "Usage: preview eggs, milk"
        return "Unknown command. Send 'help' for available commands."

    chat_key = _chat_key(message)

    if isinstance(command, commands.StartCommand):
        return commands.START_TEXT

    if isinstance(command, commands.HelpCommand):
        return commands.HELP_TEXT

    if isinstance(command, commands.HelpPrefsCommand):
        return commands.HELP_PREFS_TEXT

    if isinstance(command, commands.PreviewCommand):
        try:
            payload = finch_client.preview(command.text)
        except finch_client.FinchApiError as exc:
            return _format_finch_api_error(exc)
        return commands.format_preview_response(payload)

    if isinstance(command, commands.AddCommand):
        try:
            payload = finch_client.cart_add(
                command.item,
                source="telegram",
                chat_key=chat_key,
            )
        except finch_client.FinchApiError as exc:
            if exc.status_code == 403:
                return commands.format_cart_blocked(exc.detail)
            return _format_finch_api_error(exc)
        return commands.format_add_response(payload)

    if isinstance(command, commands.AddListCommand):
        try:
            payload = finch_client.cart_add_list(
                command.text,
                source="telegram",
                chat_key=chat_key,
            )
        except finch_client.FinchApiError as exc:
            if exc.status_code == 403:
                return commands.format_cart_blocked(exc.detail)
            return _format_finch_api_error(exc)
        return commands.format_add_list_response(payload)

    if isinstance(command, commands.HistoryCommand):
        try:
            payload = finch_client.cart_history(limit=10, scope=command.scope)
        except finch_client.FinchApiError as exc:
            return _format_finch_api_error(exc)
        return commands.format_history_response(payload)

    if isinstance(command, commands.ResetTripCommand):
        try:
            payload = finch_client.trip_reset()
        except finch_client.FinchApiError as exc:
            return _format_finch_api_error(exc)
        return str(payload.get("message") or "Started new grocery trip.")

    if isinstance(command, commands.UndoLastCommand):
        try:
            payload = finch_client.trip_undo_last()
        except finch_client.FinchApiError as exc:
            return _format_finch_api_error(exc)
        return str(payload.get("message") or "Undo complete.")

    if isinstance(command, commands.PrefsCommand):
        try:
            payload = finch_client.preferences_list()
        except finch_client.FinchApiError as exc:
            return _format_finch_api_error(exc)
        return commands.format_preferences_response(payload)

    if isinstance(command, commands.PrefCommand):
        try:
            payload = finch_client.preference_get(command.item)
        except finch_client.FinchApiError as exc:
            return _format_finch_api_error(exc)
        return commands.format_preference_get_response(payload)

    if isinstance(command, commands.ForgetPrefCommand):
        try:
            payload = finch_client.preference_delete(command.item)
        except finch_client.FinchApiError as exc:
            return _format_finch_api_error(exc)
        return commands.format_preference_delete_response(payload)

    if isinstance(command, commands.ChangePrefCommand):
        try:
            payload = finch_client.preference_change(
                command.item,
                chat_key=chat_key,
                source="telegram",
            )
        except finch_client.FinchApiError as exc:
            if exc.status_code == 403:
                return commands.format_cart_blocked(exc.detail)
            return _format_finch_api_error(exc)
        if payload.get("needs_choice"):
            return commands.format_needs_choice_response(payload)
        return commands.format_error("Unexpected change response.")

    if isinstance(command, commands.AliasPrefCommand):
        try:
            payload = finch_client.preference_alias(command.new_key, command.existing_key)
        except finch_client.FinchApiError as exc:
            return _format_finch_api_error(exc)
        return commands.format_preference_alias_response(payload)

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
