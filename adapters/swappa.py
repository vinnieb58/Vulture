"""
adapters/swappa.py

Swappa search adapter for Vulture (computer / electronics / gaming hunts).

Status: EXPERIMENTAL — server-rendered HTML via requests + BeautifulSoup.
----------------------------------------------------------------------
Do not set ``stable=True`` until the adapter has passed extended smoke and
production hunt cycles.

Parsing strategy
----------------
Two-step fetch (confirmed by experiments/adapters/swappa_probe.py, May 2026):

  1. ``GET /search?q={query}`` — model-card page; yields ``/listings/{slug}`` hrefs
  2. ``GET /listings/{slug}`` — up to ~50 individual listing cards per model

Each listing card is a ``.xui_card_wrapper`` element with:

  - ``data-code``   — listing code for canonical URL
  - ``data-price``  — integer dollar price (most reliable)
  - ``.headline``   — seller-written title (preferred)
  - ``meta[itemprop="description"]`` — model name fallback
  - ``.ships_from`` — "City, ST" ship-from location (when present)

Canonical link: ``https://swappa.com/listing/view/{code}``

Location limitation
-------------------
Swappa does not support city/zip targeting via URL or cookies. The ``city``
argument is accepted for registry interface compatibility and logged for
observability only. Individual listings may include a ship-from city/state in
``.ships_from`` when the seller exposes it.

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

_BASE_URL = "https://swappa.com"
_SEARCH_URL = f"{_BASE_URL}/search"
_LISTING_URL_TEMPLATE = f"{_BASE_URL}/listing/view/{{code}}"
_CARD_SELECTOR = ".xui_card_wrapper"
_MAX_SLUG_PAGES = 5

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) "
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


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _parse_price(raw: object) -> int | None:
    """Convert a raw price value to an integer dollar amount, or None."""
    if raw is None:
        return None
    m = re.search(r"[\d,]+", str(raw).replace("$", ""))
    if not m:
        return None
    try:
        return int(m.group().replace(",", ""))
    except ValueError:
        return None


def _absolute_url(path: str) -> str:
    if not path:
        return ""
    if path.startswith("http://") or path.startswith("https://"):
        return path
    return urljoin(_BASE_URL, path)


def _fetch_html(url: str, *, context: str) -> str | None:
    log.debug("Swappa fetch (%s): %s", context, url)
    try:
        resp = requests.get(url, headers=_HEADERS, timeout=20, allow_redirects=True)
    except requests.RequestException as exc:
        log.error("Swappa request failed (%s) %s: %s", context, url, exc)
        return None

    if resp.status_code == 403:
        log.error(
            "Swappa returned 403 Forbidden (%s) — possible IP/bot block", context
        )
        return None

    if resp.status_code not in (200, 206):
        log.error(
            "Swappa returned HTTP %d (%s) for %s", resp.status_code, context, url
        )
        return None

    final_url = str(resp.url).lower()
    if "login" in final_url or "signin" in final_url:
        log.error("Swappa redirected to login (%s): %s", context, resp.url)
        return None

    return resp.text


def _extract_model_slugs(html: str) -> list[str]:
    """Return deduplicated ``/listings/...`` paths from a search results page."""
    soup = BeautifulSoup(html, "lxml")
    slugs: list[str] = []
    seen: set[str] = set()
    for anchor in soup.find_all("a", href=True):
        href = anchor["href"].strip()
        if href.startswith("/listings/") and href not in seen:
            seen.add(href)
            slugs.append(href)
    return slugs


def _card_to_listing(wrapper) -> Listing | None:
    """Parse one ``.xui_card_wrapper`` element into a Listing, or None."""
    code = (wrapper.get("data-code") or "").strip()
    if not code:
        log.debug("Swappa: skipping card without data-code")
        return None

    headline_el = wrapper.select_one(".headline")
    meta_desc_el = wrapper.select_one('meta[itemprop="description"]')
    if headline_el and headline_el.get_text(strip=True):
        title = headline_el.get_text(strip=True)
    elif meta_desc_el:
        title = (meta_desc_el.get("content") or "").strip()
    else:
        title = ""

    if not title:
        log.debug("Swappa: skipping card %s with empty title", code)
        return None

    price = _parse_price(wrapper.get("data-price"))
    if price is None:
        price_el = wrapper.select_one('span[itemprop="price"]')
        if price_el:
            price = _parse_price(price_el.get("content") or price_el.get_text())

    ships_el = wrapper.select_one(".ships_from")
    location = ships_el.get_text(strip=True) if ships_el else None
    if location:
        location = location.strip() or None

    link = _LISTING_URL_TEMPLATE.format(code=code)

    return Listing(
        source="swappa",
        title=title,
        price=price,
        location=location,
        link=link,
    )


def _parse_listings_page(html: str, *, slug: str) -> list[Listing]:
    soup = BeautifulSoup(html, "lxml")
    wrappers = soup.select(_CARD_SELECTOR)
    if not wrappers:
        log.debug(
            "Swappa: no %r cards on %s (len=%d)",
            _CARD_SELECTOR,
            slug,
            len(html),
        )
        return []

    listings: list[Listing] = []
    for wrapper in wrappers:
        listing = _card_to_listing(wrapper)
        if listing is not None:
            listings.append(listing)
    return listings


def _fetch_listings_for_slug(slug: str) -> list[Listing]:
    url = _absolute_url(slug)
    html = _fetch_html(url, context=f"listings:{slug}")
    if html is None:
        return []
    return _parse_listings_page(html, slug=slug)


# ---------------------------------------------------------------------------
# Public adapter function
# ---------------------------------------------------------------------------


def search_swappa(
    query: str,
    city: str | None = None,
    limit: int = 25,
) -> list[Listing]:
    """
    Search Swappa for *query* and return up to *limit* ``Listing`` objects.

    Resolves the query via ``/search?q=...``, then aggregates individual
    listings from one or more matching model pages (``/listings/{slug}``)
    until *limit* is reached.

    *city* is advisory only — Swappa does not support location targeting from
    a server-side requests call. Ship-from city/state may appear per listing
    when the seller exposes it in ``.ships_from``.

    Does not write to SQLite. Does not send Discord alerts.
    """
    log.info(
        "Swappa search: query=%r requested_city=%r (advisory — location_control=not_supported) limit=%d",
        query,
        city,
        limit,
    )

    search_url = f"{_SEARCH_URL}?q={quote_plus(query)}"
    search_html = _fetch_html(search_url, context="search")
    if search_html is None:
        return []

    slugs = _extract_model_slugs(search_html)
    if not slugs:
        log.warning(
            "Swappa: zero model slugs for query %r — query may match nothing",
            query,
        )
        return []

    log.debug(
        "Swappa: query=%r matched %d model slug(s); probing up to %d",
        query,
        len(slugs),
        _MAX_SLUG_PAGES,
    )

    listings: list[Listing] = []
    seen_codes: set[str] = set()

    for slug in slugs[:_MAX_SLUG_PAGES]:
        if len(listings) >= limit:
            break
        page_listings = _fetch_listings_for_slug(slug)
        for listing in page_listings:
            if len(listings) >= limit:
                break
            code = listing.link.rstrip("/").split("/")[-1]
            if code in seen_codes:
                continue
            seen_codes.add(code)
            listings.append(listing)

    if not listings:
        log.warning(
            "Swappa: query %r yielded 0 usable listings after parsing %d slug(s)",
            query,
            min(len(slugs), _MAX_SLUG_PAGES),
        )
        return []

    locations = sorted({lst.location for lst in listings if lst.location})
    log.info(
        "Swappa: query=%r returned %d listing(s). requested_city=%r, locations_observed=%s",
        query,
        len(listings),
        city,
        locations,
    )
    return listings
