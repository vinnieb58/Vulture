import os
import requests
from dotenv import load_dotenv

from models.listing import Listing

load_dotenv()


def send_discord_alert(listing: Listing) -> None:
    webhook_url = os.getenv("DISCORD_WEBHOOK_URL")

    if not webhook_url:
        print("No Discord webhook configured. Skipping alert.")
        return

    price_text = f"${listing.price}" if listing.price is not None else "No price"
    location_text = listing.location if listing.location else "No location"

    content = (
        f"🦅 **Vulture spotted prey**\n"
        f"**Source:** {listing.source}\n"
        f"**Title:** {listing.title}\n"
        f"**Price:** {price_text}\n"
        f"**Location:** {location_text}\n"
        f"**Link:** {listing.link}"
    )

    response = requests.post(
        webhook_url,
        json={"content": content},
        timeout=15,
    )
    response.raise_for_status()