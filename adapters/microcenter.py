"""
adapters/microcenter.py

Micro Center retail search adapter for Vulture.

Status: EXPERIMENTAL — requires Playwright Chromium on a host that can pass
Cloudflare (validated on Raven residential IP, May 2026).
----------------------------------------------------------------------
Plain ``requests`` / ``curl_cffi`` return HTTP 403 ("Just a moment...").
Use headless Chromium per search call; do not assume a long-lived browser.

Store scoping: append ``&storeid=<id>`` to search URLs. Availability text in
``.price_wrapper`` reflects per-store stock (e.g. "25+ IN STOCK at Brooklyn Store").

Default storeid: ``141`` (Columbus, OH) when none supplied — conservative fallback.
Override via ``storeid=`` argument or ``adapter_options`` when the execution
model supports it.

Does not write to SQLite. Does not send Discord alerts. Never raises on failure.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any, Optional
from urllib.parse import urlencode, urljoin

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
    log.warning(
        "microcenter: playwright not installed; adapter will return []. "
        "Install: pip install playwright && python -m playwright install chromium"
    )

_MICROCENTER_ORIGIN = "https://www.microcenter.com"
_SEARCH_PATH = "/search/search_results.aspx"

# Default store when no storeid/city mapping (Columbus — Raven probe default).
_DEFAULT_STOREID = "141"

_STORE_ID_RE = re.compile(r"^\d{2,3}$")

# Optional city name → storeid (extend as needed).
_CITY_TO_STOREID: dict[str, str] = {
    "brooklyn": "115",
    "columbus": "141",
    "dallas": "131",
    "tustin": "101",
    "houston": "155",
}

_VIEWPORT = {"width": 1440, "height": 900}
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
_LOCALE = "en-US"
_TIMEZONE = "America/New_York"

_NAV_TIMEOUT_MS = 60_000
_LISTING_WAIT_MS = 20_000
_SETTLE_MS = 2_000

_CARD_SELECTOR = "#productGrid li.product_wrapper"

_CHALLENGE_TITLE_FRAGMENTS = (
    "just a moment",
    "access denied",
    "checking your browser",
    "attention required",
)

_CHALLENGE_HTML_MARKERS = (
    "cdn-cgi/challenge-platform",
    "cf-browser-verification",
    "enable javascript and cookies",
    "__cf_chl",
    "cf-challenge-running",
)


def build_search_url(query: str, store_id: Optional[str] = None) -> str:
    params: dict[str, str] = {
        "Ntt": query,
        "Ntk": "all",
        "sortby": "match",
    }
    if store_id:
        params["storeid"] = store_id
    return f"{_MICROCENTER_ORIGIN}{_SEARCH_PATH}?{urlencode(params)}"


def resolve_storeid(
    city: str | None,
    storeid: str | int | None,
    **kwargs: Any,
) -> str:
    """Resolve Micro Center storeid from explicit args, kwargs, city, or default."""
    if storeid is not None:
        return str(storeid).strip()
    kw_sid = kwargs.get("storeid")
    if kw_sid is not None:
        return str(kw_sid).strip()
    if city:
        stripped = city.strip()
        if _STORE_ID_RE.match(stripped):
            return stripped
        mapped = _CITY_TO_STOREID.get(stripped.lower().replace(" ", "_"))
        if mapped:
            return mapped
    return _DEFAULT_STOREID


def parse_price_int(raw: str | int | None) -> int | None:
    if raw is None:
        return None
    s = str(raw).replace(",", "").replace("$", "").strip()
    m = re.search(r"(\d+)", s)
    if not m:
        return None
    try:
        return int(m.group(1))
    except ValueError:
        return None


def normalize_product_link(href: str | None) -> str | None:
    if not href:
        return None
    href = href.strip()
    if href.startswith("/"):
        return urljoin(_MICROCENTER_ORIGIN, href)
    if href.startswith("http"):
        return href.split("#")[0].rstrip("/") if "/product/" in href else href
    return urljoin(_MICROCENTER_ORIGIN + "/", href)


def summarize_availability(raw: str | None) -> str | None:
    if not raw:
        return None
    text = raw.strip()
    for sep in (" QUICK VIEW ", " ADD TO CART ", " Add To List "):
        if sep in text:
            text = text.split(sep)[0].strip()
    if len(text) > 160:
        text = text[:160] + "…"
    return text or None


def is_page_blocked(title: str, html: str, card_count: int) -> tuple[bool, str]:
    title_lower = (title or "").lower()
    html_lower = (html or "").lower()

    for frag in _CHALLENGE_TITLE_FRAGMENTS:
        if frag in title_lower:
            return True, f"challenge_title:{frag}"

    if card_count == 0:
        for marker in _CHALLENGE_HTML_MARKERS:
            if marker in html_lower:
                return True, f"challenge_html:{marker}"
        if len(html_lower) < 12_000:
            return True, "thin_html_no_products"

    return False, ""


def _inject_stealth(page: Page) -> None:
    page.add_init_script("""
        Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
        Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
        window.chrome = { runtime: {} };
    """)


def card_to_listing(card, *, store_id: str) -> Listing | None:
    """Parse one ``li.product_wrapper`` card into a Listing."""
    title: str | None = None
    price: int | None = None
    link: str | None = None
    location: str | None = None

    data_anchor = card.select_one("a[data-name][href*='/product/']") or card.select_one(
        "a[data-name]"
    )
    if data_anchor:
        title = (data_anchor.get("data-name") or "").strip() or None
        link = normalize_product_link(data_anchor.get("href"))
        raw_price = (data_anchor.get("data-price") or "").strip()
        if raw_price and raw_price not in ("0", "0.00"):
            price = parse_price_int(raw_price)

    if not title:
        img = card.select_one("img.SearchResultProductImage, img[alt]")
        if img and img.get("alt"):
            title = img.get("alt", "").strip() or None

    if not link:
        for el in card.select("a[href*='/product/']"):
            href = el.get("href")
            if href and "/product/" in href:
                link = normalize_product_link(href)
                if not title:
                    title = (el.get("data-name") or el.get_text(" ", strip=True) or "").strip() or None
                break

    if price is None:
        for el in card.select("[data-price]"):
            raw_price = (el.get("data-price") or "").strip()
            if raw_price and raw_price not in ("0", "0.00"):
                price = parse_price_int(raw_price)
                break

    pw = card.select_one(".price_wrapper")
    if pw:
        pw_text = pw.get_text(" ", strip=True)
        location = summarize_availability(pw_text)
        if price is None and pw_text and "$" in pw_text:
            m = re.search(r"\$[\d,]+(?:\.\d{2})?", pw_text)
            if m:
                price = parse_price_int(m.group(0))

    if not title or not link:
        return None

    if location and store_id and f"storeid={store_id}" not in (link or ""):
        location = f"storeid={store_id} | {location}"

    return Listing(
        source="microcenter",
        title=title,
        price=price,
        location=location,
        link=link,
    )


def parse_listings_from_html(html: str, *, limit: int, store_id: str) -> list[Listing]:
    soup = BeautifulSoup(html, "lxml")
    cards = soup.select(_CARD_SELECTOR)
    if not cards:
        return []

    listings: list[Listing] = []
    seen_links: set[str] = set()

    for card in cards:
        if len(listings) >= limit:
            break
        listing = card_to_listing(card, store_id=store_id)
        if listing is None or listing.link in seen_links:
            continue
        seen_links.add(listing.link)
        listings.append(listing)

    return listings


def _fetch_html(url: str, query: str) -> tuple[str | None, str, int]:
    """
    Playwright fetch. Returns (html, page_title, card_count).
    html is None on hard failure.
    """
    if not _PLAYWRIGHT_AVAILABLE:
        log.error("microcenter: playwright unavailable for query %r", query)
        return None, "", 0

    browser: Browser | None = None
    page_title = ""
    card_count = 0

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(
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
                resp = page.goto(url, wait_until="domcontentloaded", timeout=_NAV_TIMEOUT_MS)
            except PlaywrightTimeout:
                log.error(
                    "microcenter: navigation timeout (%d ms) query=%r url=%s",
                    _NAV_TIMEOUT_MS,
                    query,
                    url,
                )
                return None, "", 0
            except Exception as exc:
                err = str(exc).lower()
                if "http2_protocol_error" in err or "err_http2" in err:
                    log.error(
                        "microcenter: HTTP/2 block for query %r — use residential IP (Raven)",
                        query,
                    )
                else:
                    log.error(
                        "microcenter: navigation error query=%r: %s",
                        query,
                        str(exc)[:200],
                    )
                return None, "", 0

            http_status = resp.status if resp else None
            if http_status == 403:
                log.error(
                    "microcenter: HTTP 403 for query %r — Cloudflare block",
                    query,
                )
                return None, page.title() or "", 0

            if http_status not in (200, 206, None):
                log.error("microcenter: HTTP %s for query %r", http_status, query)
                return None, page.title() or "", 0

            try:
                page.wait_for_selector(_CARD_SELECTOR, timeout=_LISTING_WAIT_MS)
            except PlaywrightTimeout:
                log.warning(
                    "microcenter: product grid not found within %d ms for query %r",
                    _LISTING_WAIT_MS,
                    query,
                )

            page.wait_for_timeout(_SETTLE_MS)
            page_title = page.title() or ""
            html = page.content()
            card_count = len(BeautifulSoup(html, "lxml").select(_CARD_SELECTOR))
            log.info(
                "microcenter: fetched %d chars, %d cards, title=%r query=%r",
                len(html),
                card_count,
                page_title[:60],
                query,
            )
            return html, page_title, card_count
    except Exception as exc:
        log.error("microcenter: unexpected Playwright error query=%r: %s", query, exc)
        return None, "", 0
    finally:
        if browser is not None:
            try:
                browser.close()
            except Exception:
                pass


def search_microcenter(
    query: str,
    city: str | None = None,
    limit: int = 25,
    storeid: str | int | None = None,
    **kwargs: Any,
) -> list[Listing]:
    """
    Search Micro Center and return up to *limit* normalized listings.

    Never raises; returns ``[]`` on Cloudflare block, missing grid, or fetch errors.
    """
    store_id = resolve_storeid(city, storeid, **kwargs)
    url = build_search_url(query, store_id)

    log.info(
        "microcenter search: query=%r storeid=%s city=%r limit=%d url=%s",
        query,
        store_id,
        city,
        limit,
        url,
    )

    html, page_title, card_count = _fetch_html(url, query)
    if html is None:
        log.warning(
            "microcenter: no HTML for query=%r storeid=%s — returning []",
            query,
            store_id,
        )
        return []

    blocked, reason = is_page_blocked(page_title, html, card_count)
    if blocked:
        log.warning(
            "microcenter: blocked or empty results query=%r storeid=%s reason=%s "
            "title=%r cards=%d",
            query,
            store_id,
            reason,
            page_title,
            card_count,
        )
        return []

    listings = parse_listings_from_html(html, limit=limit, store_id=store_id)
    if not listings:
        log.warning(
            "microcenter: 0 listings parsed query=%r storeid=%s cards_in_dom=%d",
            query,
            store_id,
            card_count,
        )
        return []

    log.info(
        "microcenter: query=%r storeid=%s -> %d listing(s)",
        query,
        store_id,
        len(listings),
    )
    return listings
