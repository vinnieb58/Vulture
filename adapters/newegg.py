"""
adapters/newegg.py

Newegg search adapter for Vulture (computer parts / gaming / electronics hunts).

Status: EXPERIMENTAL — server-rendered HTML via requests + BeautifulSoup.
----------------------------------------------------------------------
Do not set ``stable=True`` until extended smoke and production hunt cycles pass.

Parsing strategy (confirmed by experiments/adapters/newegg_probe.py, May 2026):

  GET https://www.newegg.com/p/pl?d={query}
  Cards: ``.item-cell`` / ``.item-container``
  Fields: ``a.item-title``, ``.price-current``, ``.price-ship``, ``.item-promo``

Important: ``Accept-Encoding`` must be ``gzip, deflate`` only. Advertising
Brotli (``br``) without a decoder yields truncated HTML and zero product cards.

Location: not targetable. The ``city`` argument is accepted for registry
interface compatibility and logged for observability only.

Does not write to SQLite. Does not send Discord alerts.
"""

from __future__ import annotations

import logging
import re
from urllib.parse import quote_plus, urljoin

import requests
from bs4 import BeautifulSoup

from models.listing import Listing

log = logging.getLogger(__name__)

_BASE_URL = "https://www.newegg.com"
_SEARCH_URL = f"{_BASE_URL}/p/pl"
_CARD_SELECTORS = (".item-cell", ".item-container")
_REQUEST_TIMEOUT = 20

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate",
    "DNT": "1",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}


def _build_search_url(query: str) -> str:
    return f"{_SEARCH_URL}?d={quote_plus(query)}"


def _parse_price(raw: str | None) -> int | None:
    """Parse a dollar price string to integer dollars, or None."""
    if not raw:
        return None
    match = re.search(r"\$?\s*([\d,]+)", str(raw).replace(",", ""))
    if not match:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


def _normalize_link(href: str | None) -> str | None:
    if not href:
        return None
    href = href.strip()
    if href.startswith("//"):
        href = "https:" + href
    elif href.startswith("/"):
        href = urljoin(_BASE_URL, href)
    if href.startswith("http"):
        return href.split("?")[0].rstrip("/") if "/p/" in href else href
    return href


def _card_to_listing(card) -> Listing | None:
    title_el = card.select_one("a.item-title") or card.select_one(".item-title")
    price_el = card.select_one(".price-current") or card.select_one(".price")
    link_el = title_el if title_el and title_el.name == "a" else card.select_one("a.item-title")

    title = title_el.get_text(" ", strip=True) if title_el else None
    link = _normalize_link(link_el.get("href") if link_el else None)
    raw_price = price_el.get_text(" ", strip=True) if price_el else None
    price = _parse_price(raw_price)

    if not title or not link:
        return None

    return Listing(
        source="newegg",
        title=title,
        price=price,
        location=None,
        link=link,
    )


def _parse_search_html(html: str, *, limit: int) -> list[Listing]:
    soup = BeautifulSoup(html, "lxml")
    cards: list = []
    for selector in _CARD_SELECTORS:
        found = soup.select(selector)
        if found:
            cards = found
            break

    if not cards:
        log.debug("Newegg: no product cards found in search HTML (len=%d)", len(html))
        return []

    listings: list[Listing] = []
    seen_links: set[str] = set()

    for card in cards:
        if len(listings) >= limit:
            break
        listing = _card_to_listing(card)
        if listing is None:
            continue
        if listing.link in seen_links:
            continue
        seen_links.add(listing.link)
        listings.append(listing)

    return listings


def _fetch_search_html(url: str) -> str | None:
    log.debug("Newegg fetch: %s", url)
    try:
        resp = requests.get(
            url,
            headers=_HEADERS,
            timeout=_REQUEST_TIMEOUT,
            allow_redirects=True,
        )
    except requests.RequestException as exc:
        log.error("Newegg request failed %s: %s", url, exc)
        return None

    if resp.status_code == 403:
        log.error("Newegg returned 403 Forbidden — possible IP/bot block")
        return None
    if resp.status_code == 429:
        log.error("Newegg returned 429 Too Many Requests — rate limit")
        return None
    if resp.status_code not in (200, 206):
        log.error("Newegg returned HTTP %d for %s", resp.status_code, url)
        return None

    final_url = str(resp.url).lower()
    if "newegg.com" not in final_url or "/p/pl" not in final_url:
        log.warning("Newegg redirected away from search results: %s", resp.url)
        return None

    return resp.text


def search_newegg(
    query: str,
    city: str | None = None,
    limit: int = 25,
) -> list[Listing]:
    """
    Search Newegg for *query* and return up to *limit* ``Listing`` objects.

    *city* is advisory only — Newegg search is not location-targeted.

    Returns an empty list on request, load, or parse failures; never raises.
    """
    log.info(
        "Newegg search: query=%r requested_city=%r (advisory — location_control=not_supported) limit=%d",
        query,
        city,
        limit,
    )

    try:
        url = _build_search_url(query)
        html = _fetch_search_html(url)
        if html is None:
            return []

        listings = _parse_search_html(html, limit=limit)
        if not listings:
            log.warning("Newegg: query %r yielded 0 usable listings", query)
            return []

        log.info("Newegg: query=%r returned %d listing(s)", query, len(listings))
        return listings
    except Exception as exc:
        log.error("Newegg: unexpected error for query %r: %s", query, exc, exc_info=True)
        return []
