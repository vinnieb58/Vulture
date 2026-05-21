import re
import requests
from bs4 import BeautifulSoup

url = "https://houston.craigslist.org/search/sss?query=graphics+card"

headers = {
    "User-Agent": "Mozilla/5.0"
}

response = requests.get(url, headers=headers, timeout=20)
response.raise_for_status()

soup = BeautifulSoup(response.text, "lxml")
results = soup.select("li.cl-static-search-result")

print(f"Found {len(results)} listings\n")

for item in results[:10]:
    # Main clickable title/link
    title_el = item.select_one("a")

    # Price element
    price_el = item.select_one(".price")

    # Location is often in a separate span
    location_el = item.select_one(".location")

    raw_title = title_el.get_text(" ", strip=True) if title_el else "No title"
    link = title_el.get("href") if title_el else "No link"
    raw_price = price_el.get_text(strip=True) if price_el else None
    raw_location = location_el.get_text(" ", strip=True) if location_el else None

    # Clean location like "(Spring)" -> "Spring"
    location = None
    if raw_location:
        location = raw_location.strip("() ").strip()

    # Convert "$140" -> 140
    price = None
    if raw_price:
        match = re.search(r"\$?([\d,]+)", raw_price)
        if match:
            price = int(match.group(1).replace(",", ""))

    # Remove trailing price/location text accidentally glued into title
    title = raw_title

    if raw_price:
        title = title.replace(raw_price, "").strip()

    if raw_location:
        title = title.replace(raw_location, "").strip()

    # Collapse weird extra whitespace
    title = " ".join(title.split())

    print("Title:", title)
    print("Price:", price)
    print("Location:", location)
    print("Link:", link)
    print()