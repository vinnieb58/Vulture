"""Send outbound WhatsApp Cloud API messages."""

from __future__ import annotations

import httpx

from finch_whatsapp import config


class WhatsAppSendError(Exception):
    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def send_text_message(to: str, body: str) -> dict:
    phone_id = config.phone_number_id()
    version = config.graph_api_version()
    url = f"https://graph.facebook.com/{version}/{phone_id}/messages"
    headers = {
        "Authorization": f"Bearer {config.access_token()}",
        "Content-Type": "application/json",
    }
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"body": body},
    }
    with httpx.Client(timeout=30.0) as client:
        response = client.post(url, headers=headers, json=payload)
    if response.status_code >= 400:
        detail = "WhatsApp send failed"
        try:
            body_json = response.json()
            if isinstance(body_json, dict):
                err = body_json.get("error")
                if isinstance(err, dict) and err.get("message"):
                    detail = str(err["message"])
        except ValueError:
            pass
        raise WhatsAppSendError(response.status_code, detail)
    return response.json()
