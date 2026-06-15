"""Telegram Bot API client for long polling and outbound replies."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import httpx

from finch_telegram import config

logger = logging.getLogger(__name__)

TELEGRAM_API_BASE = "https://api.telegram.org"


class TelegramApiError(Exception):
    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


class TelegramSendError(TelegramApiError):
    pass


@dataclass(frozen=True)
class InboundTextMessage:
    chat_id: str
    user_id: str
    text: str
    update_id: int
    message_id: int | None = None


def _api_url(method: str) -> str:
    return f"{TELEGRAM_API_BASE}/bot{config.bot_token()}/{method}"


def _request(method: str, *, params: dict[str, Any] | None = None, json: dict[str, Any] | None = None) -> dict[str, Any]:
    with httpx.Client(timeout=max(config.poll_timeout_seconds() + 10, 40)) as client:
        response = client.request(
            "GET" if json is None else "POST",
            _api_url(method),
            params=params,
            json=json,
        )
    if response.status_code >= 400:
        detail = response.text
        try:
            payload = response.json()
            if isinstance(payload, dict):
                description = payload.get("description")
                if description:
                    detail = str(description)
        except ValueError:
            pass
        raise TelegramApiError(response.status_code, detail)
    payload = response.json()
    if not isinstance(payload, dict) or not payload.get("ok"):
        description = "Telegram API request failed"
        if isinstance(payload, dict) and payload.get("description"):
            description = str(payload["description"])
        raise TelegramApiError(response.status_code, description)
    result = payload.get("result")
    if not isinstance(result, dict | list):
        raise TelegramApiError(response.status_code, "Telegram API returned invalid result")
    return payload


def get_updates(offset: int | None = None) -> list[dict[str, Any]]:
    params: dict[str, Any] = {"timeout": config.poll_timeout_seconds()}
    if offset is not None:
        params["offset"] = offset
    payload = _request("getUpdates", params=params)
    result = payload.get("result")
    if not isinstance(result, list):
        return []
    return result


def send_text_message(chat_id: str, text: str) -> None:
    try:
        _request(
            "sendMessage",
            json={
                "chat_id": chat_id,
                "text": text,
            },
        )
    except TelegramApiError as exc:
        logger.error("Telegram reply failed (status=%s)", exc.status_code)
        raise TelegramSendError(exc.status_code, exc.detail) from exc


def extract_text_messages(updates: list[dict[str, Any]]) -> list[InboundTextMessage]:
    messages: list[InboundTextMessage] = []
    for update in updates:
        update_id = update.get("update_id")
        message = update.get("message") or update.get("edited_message")
        if not isinstance(message, dict):
            continue
        text = message.get("text")
        chat = message.get("chat") or {}
        user = message.get("from") or {}
        chat_id = chat.get("id")
        user_id = user.get("id")
        if update_id is None or not text or chat_id is None or user_id is None:
            continue
        messages.append(
            InboundTextMessage(
                chat_id=str(chat_id),
                user_id=str(user_id),
                text=str(text),
                update_id=int(update_id),
                message_id=message.get("id"),
            )
        )
    return messages
