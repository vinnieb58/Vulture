"""
adapters/bestbuy.py

Best Buy retail search adapter for Vulture.

Status: EXPERIMENTAL — Playwright Chromium required (plain HTTP fails on Raven).
----------------------------------------------------------------------
Included in computer/electronics vertical profiles when
INCLUDE_EXPERIMENTAL_COMPUTER_RETAIL_DEFAULTS is enabled.

Does not write to SQLite. Does not send Discord alerts. Never raises on failure.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any
from urllib.parse import quote_plus, urljoin

from bs4 import BeautifulSoup

from models.listing import Listing

log = logging.getLogger(__name__)

if not os.environ.get("PLAYWRIGHT_HOST_PLATFORM_OVERRIDE"):
    os.environ["PLAYWRIGHT_HOST_PLATFORM_OVERRIDE"] = "ubuntu24.04-x64"

try:
    from playwright.sync_api import (
        Browser,
        BrowserContext,
        Page,
        TimeoutError as PlaywrightTimeout,
        sync_playwright,
    )

    _PLAYWRIGHT_AVAILABLE = True
except ImportError:
    _PLAYWRIGHT_AVAILABLE = False

_BASE_URL = "https://www.bestbuy.com"
_SEARCH_URL = f"{_BASE_URL}/site/searchpage.jsp"

_CARD_SELECTORS = (
    ".list-item",
    "li.product-list-item",
    "li.sku-item",
)

_TITLE_SELECTORS = (
    "a.sku-title span.nc-product-title",
    "a.sku-title",
    "a.product-list-item-link",
    "span.nc-product-title",
)

_PRICE_SELECTORS = (
    "span.font-500",
    ".priceView-customer-price span",
    "[data-testid='customer-price']",
    ".priceView-price span",
)

_LINK_SELECTORS = (
    "a.sku-title",
    "a.product-list-item-link",
    "a[href*='/product/']",
)

_VIEWPORT = {"width": 1440, "height": 900}
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
_NAV_TIMEOUT_MS = 90_000
_CARD_WAIT_MS = 20_000
_SETTLE_MS = 2_000


def build_search_url(query: str) -> str:
    return f"{_SEARCH_URL}?st={quote_plus(query)}"


def parse_price_int(raw: str | None) -> int | None:
    if not raw:
        return None
    match = re.search(r"\$?\s*([\d,]+)", str(raw).replace(",", ""))
    if not match:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


def normalize_link(href: str | None) -> str | None:
    if not href:
        return None
    href = href.strip()
    if href.startswith("//"):
        href = "https:" + href
    elif href.startswith("/"):
        href = urljoin(_BASE_URL, href)
    if href.startswith("http") and "/site/" in href:
        return href.split("?")[0]
    return href if href.startswith("http") else None


def _select_text(card, selectors: tuple[str, ...]) -> str | None:
    for sel in selectors:
        el = card.select_one(sel)
        if el:
            text = el.get_text(" ", strip=True)
            if text:
                return text
    return None


def _select_link(card) -> str | None:
    for sel in _LINK_SELECTORS:
        el = card.select_one(sel)
        if el and el.get("href"):
            link = normalize_link(el["href"])
            if link and ("/product/" in link or "/site/" in link):
                return link
    return None


def card_to_listing(card) -> Listing | None:
    title = _select_text(card, _TITLE_SELECTORS)
    price_raw = _select_text(card, _PRICE_SELECTORS)
    if not price_raw:
        match = re.search(r"\$[\d,]+(?:\.\d{2})?", card.get_text(" ", strip=True))
        price_raw = match.group(0) if match else None
    link = _select_link(card)

    if not title or not link:
        return None

    return Listing(
        source="bestbuy",
        title=title,
        price=parse_price_int(price_raw),
        location=None,
        link=link,
    )


def parse_listings_from_html(html: str, *, limit: int) -> list[Listing]:
    soup = BeautifulSoup(html, "lxml")
    cards: list = []
    for selector in _CARD_SELECTORS:
        found = soup.select(selector)
        if found:
            cards = found
            break
    if not cards:
        return []

    listings: list[Listing] = []
    seen: set[str] = set()
    for card in cards:
        if len(listings) >= limit:
            break
        listing = card_to_listing(card)
        if listing is None or listing.link in seen:
            continue
        seen.add(listing.link)
        listings.append(listing)
    return listings


def _inject_stealth(page: Page) -> None:
    page.add_init_script(
        "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
    )


def _fetch_html(url: str, query: str) -> str | None:
    if not _PLAYWRIGHT_AVAILABLE:
        log.error("bestbuy: playwright unavailable for query %r", query)
        return None

    browser: Browser | None = None
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(
                headless=True,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--disable-dev-shm-usage",
                    "--no-sandbox",
                ],
            )
            ctx: BrowserContext = browser.new_context(
                viewport=_VIEWPORT,
                user_agent=_USER_AGENT,
                locale="en-US",
                timezone_id="America/Chicago",
            )
            page: Page = ctx.new_page()
            _inject_stealth(page)

            try:
                resp = page.goto(url, wait_until="domcontentloaded", timeout=_NAV_TIMEOUT_MS)
            except PlaywrightTimeout:
                log.error("bestbuy: navigation timeout query=%r", query)
                return None
            except Exception as exc:
                log.error("bestbuy: navigation error query=%r: %s", query, str(exc)[:200])
                return None

            status = resp.status if resp else None
            if status == 403:
                log.error("bestbuy: HTTP 403 for query %r", query)
                return None
            if status not in (200, 206, None):
                log.error("bestbuy: HTTP %s for query %r", status, query)
                return None

            for sel in _CARD_SELECTORS:
                try:
                    page.wait_for_selector(sel, timeout=_CARD_WAIT_MS)
                    break
                except PlaywrightTimeout:
                    continue

            page.wait_for_timeout(_SETTLE_MS)
            return page.content()
    except Exception as exc:
        log.error("bestbuy: unexpected error query=%r: %s", query, exc)
        return None
    finally:
        if browser is not None:
            try:
                browser.close()
            except Exception:
                pass


def search_bestbuy(
    query: str,
    city: str | None = None,
    limit: int = 25,
    **kwargs: Any,
) -> list[Listing]:
    """
    Search Best Buy for *query* and return up to *limit* listings.

    *city* is advisory only — Best Buy search is not location-targeted via URL.
    Returns [] on failure; never raises.
    """
    del kwargs
    log.info(
        "Best Buy search: query=%r requested_city=%r (advisory) limit=%d",
        query,
        city,
        limit,
    )
    try:
        url = build_search_url(query)
        html = _fetch_html(url, query)
        if html is None:
            return []
        listings = parse_listings_from_html(html, limit=limit)
        if not listings:
            log.warning("bestbuy: query %r yielded 0 listings", query)
        else:
            log.info("bestbuy: query=%r returned %d listing(s)", query, len(listings))
        return listings
    except Exception as exc:
        log.error("bestbuy: unexpected error query=%r: %s", query, exc, exc_info=True)
        return []
