"""Parse WhatsApp Cloud API webhook payloads without logging secrets."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class InboundTextMessage:
    sender: str
    text: str
    message_id: str | None = None


def extract_text_messages(payload: dict[str, Any]) -> list[InboundTextMessage]:
    if payload.get("object") != "whatsapp_business_account":
        return []

    messages: list[InboundTextMessage] = []
    for entry in payload.get("entry") or []:
        for change in entry.get("changes") or []:
            value = change.get("value") or {}
            for message in value.get("messages") or []:
                if message.get("type") != "text":
                    continue
                text_body = (message.get("text") or {}).get("body")
                sender = message.get("from")
                if not sender or not text_body:
                    continue
                messages.append(
                    InboundTextMessage(
                        sender=str(sender),
                        text=str(text_body),
                        message_id=message.get("id"),
                    )
                )
    return messages
