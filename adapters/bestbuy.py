"""
adapters/bestbuy.py

Best Buy retail search adapter for Vulture (computer / electronics hunts).

Status: EXPERIMENTAL — Playwright Chromium required; validated on Raven May 2026.
----------------------------------------------------------------------
Plain requests/curl stall or timeout on Best Buy (Akamai). Playwright Chromium
loads full search-result pages from residential IP (Raven).

Parsing strategy
----------------
Playwright fetch → BeautifulSoup on rendered HTML.

Two PLP card layouts (May 2026):

  1. Grid: ``.list-item`` + ``a.sku-title`` + ``span.nc-product-title``
  2. List: ``li.product-list-item`` + ``a.product-list-item-link``

Price from ``span.font-500`` or first ``$NNN.NN`` token in card text.
Location field carries pickup/fulfillment text when visible (e.g. "Pick up in 1 hour").

The ``city`` argument is accepted for registry interface compatibility only;
Best Buy store selection is not implemented — it is logged and ignored.

Does not write to SQLite. Does not send Discord alerts. Never raises.
"""

from __future__ import annotations

import logging
import os
import re
from urllib.parse import quote_plus, urljoin

from bs4 import BeautifulSoup

from models.listing import Listing

log = logging.getLogger(__name__)

# Ubuntu 26.04 Playwright compatibility (same pattern as carsdotcom.py).
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
    log.warning(
        "playwright package not found. Best Buy adapter will return empty results. "
        "Install with: pip install playwright && python -m playwright install chromium"
    )

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

_BESTBUY_ORIGIN = "https://www.bestbuy.com"
_SEARCH_URL = f"{_BESTBUY_ORIGIN}/site/searchpage.jsp"

_VIEWPORT = {"width": 1440, "height": 900}
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
_LOCALE = "en-US"
_TIMEZONE = "America/Chicago"

_NAV_TIMEOUT_MS = 90_000
_CARD_WAIT_MS = 20_000
_SETTLE_MS = 2_000

# Primary card selectors — order matters (grid before list when both present).
_CARD_SELECTORS = [
    ".list-item",
    "li.product-list-item",
    "li.sku-item",
    ".sku-item",
]

_CARD_WAIT_SELECTORS = [
    ".list-item",
    "li.product-list-item",
    "a.product-list-item-link",
    "a.sku-title",
]

_TITLE_SELECTORS = [
    "a.sku-title span.nc-product-title",
    "a.sku-title",
    "a.product-list-item-link",
    "span.nc-product-title",
    ".sku-title a",
    ".sku-title",
    "h4.sku-title",
]

_PRICE_SELECTORS = [
    "span.font-500",
    ".priceView-customer-price span",
    ".priceView-hero-price span",
    "[data-testid='customer-price']",
    ".priceView-price",
]

_LINK_SELECTORS = [
    "a.sku-title",
    "a.product-list-item-link",
    ".sku-title a",
    "h4.sku-title a",
    "a[href*='/product/']",
]

_FULFILLMENT_SELECTORS = [
    "[class*='pickup']",
    "[data-testid*='pickup']",
    ".fulfillment-pickup",
    ".fulfillment-fulfillment-summary",
    "[class*='fulfillment']",
]

# ---------------------------------------------------------------------------
# URL / price helpers
# ---------------------------------------------------------------------------


def _build_search_url(query: str) -> str:
    return f"{_SEARCH_URL}?st={quote_plus(query)}"


def _parse_price(raw: object) -> int | None:
    """Parse dollar string to integer dollars (truncate cents): ``$599.99`` → 599."""
    if raw is None:
        return None
    m = re.search(r"\$?([\d,]+)(?:\.\d+)?", str(raw).replace(",", ""))
    if not m:
        return None
    try:
        return int(m.group(1).replace(",", ""))
    except ValueError:
        return None


def _normalize_link(href: str | None) -> str | None:
    if not href:
        return None
    href = href.strip()
    if href.startswith("//"):
        return "https:" + href
    if href.startswith("/"):
        return urljoin(_BESTBUY_ORIGIN, href)
    if href.startswith("http"):
        return href.split("?")[0] if "/product/" in href else href
    return href


def _price_from_card_text(card) -> str | None:
    for el in card.find_all(["span", "div"]):
        text = el.get_text(strip=True)
        if re.match(r"^\$[\d,]+(?:\.\d{2})?$", text):
            return text
    match = re.search(r"\$[\d,]+(?:\.\d{2})?", card.get_text(" ", strip=True))
    return match.group(0) if match else None


# ---------------------------------------------------------------------------
# DOM extraction
# ---------------------------------------------------------------------------


def _extract_text(card, selectors: list[str]) -> str | None:
    for sel in selectors:
        try:
            el = card.select_one(sel)
            if el:
                text = el.get_text(" ", strip=True)
                if text:
                    return text
        except Exception:
            pass
    return None


def _extract_link(card) -> str | None:
    for sel in _LINK_SELECTORS:
        try:
            el = card.select_one(sel)
            if el and el.get("href"):
                link = _normalize_link(el["href"])
                if link and "/product/" in link:
                    return link
        except Exception:
            pass
    return None


def _extract_fulfillment(card) -> str | None:
    for sel in _FULFILLMENT_SELECTORS:
        try:
            for el in card.select(sel):
                text = el.get_text(" ", strip=True)
                if not text or len(text) > 120:
                    continue
                lower = text.lower()
                if any(
                    tok in lower
                    for tok in ("pick up", "pickup", "shipping", "delivery", "get it by")
                ):
                    return text
        except Exception:
            pass
    return None


def _card_to_listing(card) -> Listing | None:
    title = _extract_text(card, _TITLE_SELECTORS)
    price_raw = _extract_text(card, _PRICE_SELECTORS)
    if not price_raw:
        price_raw = _price_from_card_text(card)
    price = _parse_price(price_raw)
    link = _extract_link(card)
    location = _extract_fulfillment(card)

    if not title:
        return None
    if not link:
        log.debug("bestbuy: skipping card %r — no product link", title[:60])
        return None

    return Listing(
        source="bestbuy",
        title=title,
        price=price,
        location=location,
        link=link,
    )


def _find_cards(soup: BeautifulSoup) -> tuple[list, str | None]:
    for selector in _CARD_SELECTORS:
        try:
            cards = soup.select(selector)
            if cards:
                return cards, selector
        except Exception:
            continue
    return [], None


def _parse_listings(html: str, limit: int) -> list[Listing]:
    soup = BeautifulSoup(html, "lxml")
    cards, selector = _find_cards(soup)
    if not cards:
        log.warning(
            "bestbuy: no product cards matched known selectors — "
            "layout may have changed or page is empty"
        )
        return []

    log.debug("bestbuy: selector %r matched %d card(s)", selector, len(cards))

    listings: list[Listing] = []
    seen_links: set[str] = set()

    for card in cards:
        if len(listings) >= limit:
            break
        try:
            listing = _card_to_listing(card)
        except Exception as exc:
            log.debug("bestbuy: card parse error: %s", exc)
            continue
        if listing is None:
            continue
        if listing.link in seen_links:
            continue
        seen_links.add(listing.link)
        listings.append(listing)

    return listings


# ---------------------------------------------------------------------------
# Playwright fetch
# ---------------------------------------------------------------------------


def _inject_stealth(page: Page) -> None:
    page.add_init_script("""
        Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
        Object.defineProperty(navigator, 'plugins', {
            get: () => [
                { name: 'Chrome PDF Plugin', filename: 'internal-pdf-viewer' },
            ],
        });
        Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
        window.chrome = { runtime: {}, loadTimes: function(){}, csi: function(){}, app: {} };
    """)


def _fetch_html(query: str) -> str | None:
    if not _PLAYWRIGHT_AVAILABLE:
        log.error(
            "bestbuy: playwright not available. "
            "Install: pip install playwright && python -m playwright install chromium"
        )
        return None

    url = _build_search_url(query)
    log.info("bestbuy: GET %s", url)

    try:
        with sync_playwright() as pw:
            browser: Browser = pw.chromium.launch(
                headless=True,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--disable-dev-shm-usage",
                    "--no-sandbox",
                    "--disable-gpu",
                ],
            )
            ctx: BrowserContext = browser.new_context(
                viewport=_VIEWPORT,
                user_agent=_USER_AGENT,
                locale=_LOCALE,
                timezone_id=_TIMEZONE,
                java_script_enabled=True,
                bypass_csp=True,
                extra_http_headers={
                    "Accept-Language": "en-US,en;q=0.9",
                    "DNT": "1",
                    "Upgrade-Insecure-Requests": "1",
                },
            )
            page: Page = ctx.new_page()
            _inject_stealth(page)

            try:
                resp = page.goto(
                    url,
                    wait_until="domcontentloaded",
                    timeout=_NAV_TIMEOUT_MS,
                )
            except PlaywrightTimeout:
                log.error(
                    "bestbuy: navigation timed out after %d ms for query %r",
                    _NAV_TIMEOUT_MS,
                    query,
                )
                browser.close()
                return None
            except Exception as exc:
                log.error(
                    "bestbuy: navigation error for query %r: %s",
                    query,
                    str(exc)[:200],
                )
                browser.close()
                return None

            http_status = resp.status if resp else None
            if http_status == 403:
                log.error(
                    "bestbuy: HTTP 403 for query %r — Akamai/block likely",
                    query,
                )
                browser.close()
                return None
            if http_status not in (200, 206, None):
                log.error("bestbuy: HTTP %s for query %r", http_status, query)
                browser.close()
                return None

            try:
                page.evaluate("window.scrollTo(0, document.body.scrollHeight / 3)")
                page.wait_for_timeout(800)
                page.evaluate("window.scrollTo(0, 0)")
            except Exception:
                pass

            for sel in _CARD_WAIT_SELECTORS:
                try:
                    page.wait_for_selector(sel, timeout=_CARD_WAIT_MS)
                    break
                except PlaywrightTimeout:
                    continue

            page.wait_for_timeout(_SETTLE_MS)
            html = page.content()
            browser.close()

            log.info(
                "bestbuy: fetched %d chars for query=%r (status=%s)",
                len(html),
                query,
                http_status,
            )
            return html

    except Exception as exc:
        log.error("bestbuy: unexpected Playwright error for query %r: %s", query, exc)
        return None


# ---------------------------------------------------------------------------
# Public adapter
# ---------------------------------------------------------------------------


def search_bestbuy(
    query: str,
    city: str | None = None,
    limit: int = 25,
) -> list[Listing]:
    """
    Search Best Buy and return up to *limit* ``Listing`` objects.

    Parameters
    ----------
    query:
        Search term, e.g. ``"rtx 4070"`` or ``"macbook air"``.
    city:
        Accepted for registry compatibility; **not used** for store targeting.
        Logged when provided.
    limit:
        Maximum listings to return.

    Returns
    -------
    list[Listing]
        De-duplicated listings, or ``[]`` on any failure. Never raises.
    """
    if city:
        log.debug(
            "bestbuy: city=%r ignored — store selection not implemented",
            city,
        )

    log.info("bestbuy search: query=%r limit=%d", query, limit)

    try:
        html = _fetch_html(query)
        if html is None:
            log.warning("bestbuy: fetch returned no HTML for query %r", query)
            return []

        listings = _parse_listings(html, limit)
        if not listings:
            log.warning(
                "bestbuy: query=%r returned 0 usable listings",
                query,
            )
            return []

        log.info(
            "bestbuy: query=%r -> %d listing(s)",
            query,
            len(listings),
        )
        return listings

    except Exception as exc:
        log.error("bestbuy: unexpected error for query %r: %s", query, exc)
        return []
