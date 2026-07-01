"""Discord webhook alerts for Vulture Concerts."""

from __future__ import annotations

import logging
import os

import requests
from dotenv import load_dotenv

from engine.concerts.dedupe import MergedConcertEvent
from engine.concerts.formatter import format_alert_message

load_dotenv()

log = logging.getLogger(__name__)


def send_concert_alert(event: MergedConcertEvent) -> bool:
    """Send a new-concert alert via DISCORD_WEBHOOK_URL. Returns True on success."""
    webhook_url = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
    if not webhook_url:
        log.warning("No Discord webhook configured. Skipping concert alert.")
        return False

    content = format_alert_message(event)
    try:
        response = requests.post(
            webhook_url,
            json={"content": content},
            timeout=15,
        )
        response.raise_for_status()
        return True
    except requests.RequestException:
        log.exception(
            "Failed to send concert alert for %s",
            event.event_dedupe_key,
        )
        return False
