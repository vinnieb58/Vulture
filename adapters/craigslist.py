import re
import requests
from bs4 import BeautifulSoup

from models.listing import Listing


def search_craigslist(query: str, city: str = "houston", limit: int = 10) -> list[Listing]:
    url = f"https://{city}.craigslist.org/search/sss?query={query.replace(' ', '+')}"

    headers = {
        "User-Agent": "Mozilla/5.0"
    }

    response = requests.get(url, headers=headers, timeout=20)
    response.raise_for_status()

    soup = BeautifulSoup(response.text, "lxml")
    results = soup.select("li.cl-static-search-result")

    listings: list[Listing] = []

    for item in results[:limit]:
        title_el = item.select_one("a")
        price_el = item.select_one(".price")
        location_el = item.select_one(".location")

        raw_title = title_el.get_text(" ", strip=True) if title_el else "No title"
        link = title_el.get("href") if title_el else ""
        raw_price = price_el.get_text(strip=True) if price_el else None
        raw_location = location_el.get_text(" ", strip=True) if location_el else None

        location = raw_location.strip("() ").strip() if raw_location else None

        price = None
        if raw_price:
            match = re.search(r"\$?([\d,]+)", raw_price)
            if match:
                price = int(match.group(1).replace(",", ""))

        title = raw_title
        if raw_price:
            title = title.replace(raw_price, "").strip()
        if raw_location:
            title = title.replace(raw_location, "").strip()
        title = " ".join(title.split())

        listings.append(
            Listing(
                source="craigslist",
                title=title,
                price=price,
                location=location,
                link=link,
            )
        )

    return listings