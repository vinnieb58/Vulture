"""
Swappa feasibility probe — experiments/adapters/swappa_probe.py

NOT a production adapter. Standalone reconnaissance script.
Does not touch: main.py, adapter registry, database, Discord, .env.

Usage:
    python experiments/adapters/swappa_probe.py [search term]

    # Examples:
    python experiments/adapters/swappa_probe.py "iphone 13"
    python experiments/adapters/swappa_probe.py "macbook air"
    python experiments/adapters/swappa_probe.py "samsung galaxy s23"

Swappa URL flow discovered during probe (2026-05-22):
    1. /search?q={term}          — model-card page; contains /listings/{slug} hrefs
    2. /listings/{slug}          — server-rendered page; 50 individual listing cards
    3. /listing/view/{CODE}      — individual listing detail (not fetched here)

All pages are server-side rendered. requests + BeautifulSoup is sufficient.
No JavaScript runtime, login, or session cookie required.

Key HTML anchors:
    .xui_card_wrapper            — outer wrapper per listing; data-code, data-price attrs
    .xui_card_listing            — inner card with Schema.org itemprop markup
    .headline                    — seller-written description (listing title)
    meta[itemprop="description"] — canonical model name (fallback title)
    .ships_from                  — city, state location
    .seller_name                 — seller display name
    .attr                        — condition/storage/color attributes
    span[itemprop="price"]       — price integer (also on data-price)
"""

import re
import sys
from urllib.parse import quote_plus

import requests
from bs4 import BeautifulSoup

BASE_URL = "https://swappa.com"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}

DEFAULT_QUERY = "iphone 13"
MAX_LISTINGS = 10


# ---------------------------------------------------------------------------
# Step 1 — Resolve search term to a model slug via /search
# ---------------------------------------------------------------------------

def fetch_model_slugs(query: str) -> tuple[int, str, list[str]]:
    """
    Fetch /search?q={query} and return (status, page_title, ['/listings/...', ...]).
    Slugs are deduplicated and ordered as they appear on the page.
    """
    url = f"{BASE_URL}/search?q={quote_plus(query)}"
    print(f"\n[STEP 1] Fetching search page: {url}")
    try:
        r = requests.get(url, headers=HEADERS, timeout=20)
    except requests.RequestException as exc:
        print(f"  FAILURE: {exc}")
        return 0, str(exc), []

    print(f"  HTTP status : {r.status_code}")
    print(f"  Final URL   : {r.url}")
    print(f"  Body length : {len(r.text)} bytes")

    soup = BeautifulSoup(r.text, "lxml")
    page_title = soup.title.get_text(strip=True) if soup.title else "(no title)"
    print(f"  Page title  : {page_title}")

    # Collect /listings/{slug} hrefs, preserving first-seen order
    seen: dict[str, None] = {}
    for a in soup.find_all("a", href=True):
        href = a.get("href", "")
        if href.startswith("/listings/"):
            seen[href] = None

    slugs = list(seen.keys())
    print(f"  Model slugs : {len(slugs)} found")
    for s in slugs[:8]:
        print(f"    {s}")
    if len(slugs) > 8:
        print(f"    ... ({len(slugs) - 8} more)")

    return r.status_code, page_title, slugs


# ---------------------------------------------------------------------------
# Step 2 — Fetch individual listings for first matched model slug
# ---------------------------------------------------------------------------

def fetch_listings(slug: str) -> tuple[int, str, list[dict]]:
    """
    Fetch /listings/{slug} and parse individual listing cards.
    Returns (status, page_title, [candidate_dict, ...]).

    Candidate dict keys:
        source, title, price (int|None), location, link, condition, seller, code
    """
    url = f"{BASE_URL}{slug}"
    print(f"\n[STEP 2] Fetching model listings page: {url}")
    try:
        r = requests.get(url, headers=HEADERS, timeout=20)
    except requests.RequestException as exc:
        print(f"  FAILURE: {exc}")
        return 0, str(exc), []

    print(f"  HTTP status : {r.status_code}")
    print(f"  Body length : {len(r.text)} bytes")

    soup = BeautifulSoup(r.text, "lxml")
    page_title = soup.title.get_text(strip=True) if soup.title else "(no title)"
    print(f"  Page title  : {page_title}")

    wrappers = soup.select(".xui_card_wrapper")
    print(f"  Listing cards (.xui_card_wrapper): {len(wrappers)}")

    # JS-render guard: if zero wrappers but page is 200 and large, results may be
    # dynamically injected — flag it.
    if r.status_code == 200 and len(r.text) > 20_000 and not wrappers:
        print("  WARNING: 200 response but no listing cards found — may require JS rendering")

    candidates = []
    for wrapper in wrappers[:MAX_LISTINGS]:
        code = wrapper.get("data-code", "").strip()
        price_raw = wrapper.get("data-price", "").strip()

        # Title: prefer seller headline, fall back to model meta description
        headline_el = wrapper.select_one(".headline")
        meta_desc_el = wrapper.select_one('meta[itemprop="description"]')
        if headline_el and headline_el.get_text(strip=True):
            title = headline_el.get_text(strip=True)
        elif meta_desc_el:
            title = meta_desc_el.get("content", "").strip()
        else:
            title = ""

        # Price: numeric int from data-price attr (most reliable)
        price: int | None = None
        if price_raw:
            m = re.search(r"\d+", price_raw)
            if m:
                price = int(m.group())

        # Location: .ships_from holds "City, ST"
        ships_el = wrapper.select_one(".ships_from")
        location = ships_el.get_text(strip=True) if ships_el else ""

        # Seller name
        seller_el = wrapper.select_one(".seller_name")
        seller = seller_el.get_text(strip=True) if seller_el else ""

        # Condition: first .attr element typically holds condition text
        attrs = [a.get_text(strip=True) for a in wrapper.select(".attr")]
        condition = attrs[0] if attrs else ""

        link = f"{BASE_URL}/listing/view/{code}" if code else ""

        candidates.append({
            "source": "swappa",
            "title": title,
            "price": price,
            "location": location,
            "link": link,
            "condition": condition,
            "seller": seller,
            "code": code,
        })

    return r.status_code, page_title, candidates


# ---------------------------------------------------------------------------
# Main probe
# ---------------------------------------------------------------------------

def run_probe(query: str) -> None:
    print("=" * 66)
    print(f"Swappa Probe — query: {query!r}")
    print("=" * 66)

    # -- Step 1: search page --------------------------------------------------
    status1, title1, slugs = fetch_model_slugs(query)

    if status1 == 0 or not slugs:
        print("\nProbe halted: search page fetch failed or returned no model slugs.")
        print("This may indicate the query matched nothing or network access is blocked.")
        return

    best_slug = slugs[0]
    print(f"\n  Using first slug: {best_slug}")

    # -- Step 2: model listing page -------------------------------------------
    status2, title2, candidates = fetch_listings(best_slug)

    if status2 == 0 or not candidates:
        print("\nProbe halted: listing page fetch failed or returned no listing cards.")
        return

    # -- Print results --------------------------------------------------------
    print(f"\n{'=' * 66}")
    print(f"Results  ({len(candidates)} of up to {MAX_LISTINGS} shown)")
    print(f"{'=' * 66}")

    for i, c in enumerate(candidates, 1):
        price_str = f"${c['price']}" if c["price"] is not None else "N/A"
        print(f"\n  [{i}] {c['title']}")
        print(f"       Price     : {price_str}")
        print(f"       Location  : {c['location'] or 'N/A'}")
        print(f"       Condition : {c['condition'] or 'N/A'}")
        print(f"       Seller    : {c['seller'] or 'N/A'}")
        print(f"       Link      : {c['link']}")

    print(f"\n{'=' * 66}")
    print("Normalized candidate dicts")
    print(f"{'=' * 66}")
    for c in candidates:
        print(c)

    print(f"\n{'=' * 66}")
    print("Probe summary")
    print(f"{'=' * 66}")
    has_title    = any(c["title"]    for c in candidates)
    has_price    = any(c["price"] is not None for c in candidates)
    has_location = any(c["location"] for c in candidates)
    has_link     = any(c["link"]     for c in candidates)
    print(f"  title     : {'YES' if has_title    else 'NO'}")
    print(f"  price     : {'YES' if has_price    else 'NO'}")
    print(f"  location  : {'YES' if has_location else 'NO'}")
    print(f"  link      : {'YES' if has_link     else 'NO'}")
    print(f"  JS needed : NO  (server-rendered HTML)")
    print(f"  login req : NO")
    print()


if __name__ == "__main__":
    query = " ".join(sys.argv[1:]).strip() or DEFAULT_QUERY
    run_probe(query)
