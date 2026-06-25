"""
Target candidate-source probe
==============================
Reconnaissance only. Does NOT write to SQLite, send Discord alerts,
or modify adapters/registry.py.

Usage:
    python experiments/adapters/target_probe.py
    python experiments/adapters/target_probe.py --query "steam deck" --limit 5
    python experiments/adapters/target_probe.py --query "rtx 4070" --limit 5
    python experiments/adapters/target_probe.py --query "65 inch tv" --html-only
    python experiments/adapters/target_probe.py --query "steam deck" --playwright

Default queries (when --query is omitted):
    steam deck, macbook screen, rtx 4070, 65 inch tv, dyson vacuum

Parsing strategy (priority order):
    1. Redsky public search API (``plp_search_v2``) — same JSON the browser loads.
       Uses the public key embedded in Target's web bundles (not a stored secret).
    2. Playwright-rendered DOM cards (``[data-test="product-title"]``).
    3. ``__NEXT_DATA__`` if Target ever SSRs product payloads (currently sparse).

Fetch escalation:
    Redsky API (default) -> requests HTML -> optional curl_cffi (--cffi)
    -> optional Playwright (--playwright)

Known behavior (cloud/datacenter IP, 2026-06):
    Redsky may return HTTP 403 with captcha JSON from datacenter IPs.
    Search HTML returns 200 with a Next.js shell but no SSR product cards.
    Playwright renders a large page but product cards may not hydrate without
    consent-modal dismissal and residential IP. Re-validate from Raven.
"""

from __future__ import annotations

import argparse
import html
import json
import logging
import re
import sys
import time
from dataclasses import asdict, dataclass
from typing import Any, Optional
from urllib.parse import quote_plus, urljoin

import requests
from bs4 import BeautifulSoup

log = logging.getLogger("target_probe")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

TARGET_ORIGIN = "https://www.target.com"
SEARCH_BASE = f"{TARGET_ORIGIN}/s"
REDSKY_SEARCH_URL = "https://redsky.target.com/redsky_aggregations/v1/web/plp_search_v2"
# Public client key embedded in Target web bundles (not a stored credential).
REDSKY_PUBLIC_KEY = "ff457966e64d5e877fdbad070f276d18ecec4a01"
DEFAULT_PRICING_STORE_ID = "2885"

DEFAULT_QUERIES = [
    "steam deck",
    "macbook screen",
    "rtx 4070",
    "65 inch tv",
    "dyson vacuum",
]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;"
        "q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate",
    "DNT": "1",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
}

REDSKY_HEADERS = {
    **HEADERS,
    "Accept": "application/json",
    "Origin": TARGET_ORIGIN,
    "Referer": f"{TARGET_ORIGIN}/",
}

REQUEST_TIMEOUT = 25
PLAYWRIGHT_TIMEOUT_MS = 45_000
PLAYWRIGHT_SETTLE_MS = 5_000
MAX_CANDIDATES = 5

CHALLENGE_TITLE_FRAGMENTS = [
    "access denied",
    "just a moment",
    "attention required",
    "checking your browser",
    "verify you are human",
    "captcha",
    "security check",
    "robot",
]

CHALLENGE_BODY_MARKERS = [
    "px-captcha",
    "perimeterx",
    "_px",
    "g-recaptcha",
    "cf-browser-verification",
    "captcha",
    "unusual traffic",
    "access denied",
]

CARD_SELECTORS = (
    '[data-test="@web/site-top-of-funnel/ProductCardWrapper"]',
    '[data-test="@web/ProductCard"]',
    '[data-test="product-title"]',
)


@dataclass
class ProbeListing:
    """Normalized probe output (mirrors models.listing.Listing plus optional fields)."""

    source: str
    title: str
    price: Optional[int]
    location: Optional[str]
    link: str
    image: Optional[str] = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def build_search_url(query: str) -> str:
    return f"{SEARCH_BASE}?searchTerm={quote_plus(query)}"


def build_redsky_params(query: str, *, visitor_id: str, store_id: str = DEFAULT_PRICING_STORE_ID) -> dict[str, str]:
    encoded = quote_plus(query)
    return {
        "key": REDSKY_PUBLIC_KEY,
        "channel": "WEB",
        "keyword": query,
        "page": f"/s/{encoded}",
        "visitor_id": visitor_id,
        "pricing_store_id": store_id,
        "default_purchasability_filter": "true",
        "count": "24",
        "offset": "0",
        "platform": "desktop",
    }


def build_product_url(tcin: Optional[str]) -> Optional[str]:
    if not tcin:
        return None
    tcin = str(tcin).strip()
    if not tcin:
        return None
    return f"{TARGET_ORIGIN}/p/-/A-{tcin}"


def print_section(title: str) -> None:
    print()
    print("=" * 72)
    print(f"  {title}")
    print("=" * 72)


def parse_price(raw: Any) -> Optional[int]:
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return int(raw)
    text = str(raw).replace(",", "").strip().lower()
    if not text or "see price" in text or "cart" in text:
        return None
    match = re.search(r"\$?\s*([\d,]+)(?:\.\d+)?", text)
    if not match:
        return None
    try:
        return int(match.group(1).replace(",", ""))
    except ValueError:
        return None


def normalize_link(href: Optional[str], *, tcin: Optional[str] = None) -> Optional[str]:
    if href:
        href = href.strip()
        if href.startswith("//"):
            href = "https:" + href
        elif href.startswith("/"):
            href = urljoin(TARGET_ORIGIN, href)
        if href.startswith("http"):
            return href.split("?")[0].rstrip("/")
    return build_product_url(tcin)


def decode_title(raw: Optional[str]) -> Optional[str]:
    if not raw:
        return None
    return html.unescape(str(raw)).strip() or None


def has_real_search_results(data: dict[str, Any]) -> bool:
    """True when Redsky returned genuine matches (not recommendation filler)."""
    search = data.get("data", {}).get("search") or {}
    if not isinstance(search, dict):
        return False
    sr = search.get("search_response") or {}
    if not isinstance(sr, dict):
        return False
    facet_list = sr.get("facet_list")
    if isinstance(facet_list, list) and facet_list:
        return True
    return False


def redsky_product_to_probe_listing(product: dict[str, Any]) -> Optional[ProbeListing]:
    if product.get("__typename") not in (None, "ProductSummary", "Product"):
        return None

    tcin = str(product.get("tcin") or "").strip() or None
    item = product.get("item") or {}
    if not isinstance(item, dict):
        item = {}

    title = decode_title((item.get("product_description") or {}).get("title"))
    enrichment = item.get("enrichment") or {}
    if not isinstance(enrichment, dict):
        enrichment = {}

    link = normalize_link(enrichment.get("buy_url"), tcin=tcin)
    price_block = product.get("price") or {}
    if not isinstance(price_block, dict):
        price_block = {}
    price = parse_price(price_block.get("current_retail"))
    if price is None:
        price = parse_price(price_block.get("formatted_current_price"))

    image_info = enrichment.get("image_info") or {}
    if isinstance(image_info, dict):
        primary = image_info.get("primary_image") or {}
        if isinstance(primary, dict):
            image = primary.get("url")
        else:
            image = None
    else:
        image = None

    location = None
    fulfillment = product.get("fulfillment") or product.get("fulfillment_summary")
    if isinstance(fulfillment, dict):
        location = fulfillment.get("shipping_message") or fulfillment.get("display")
    elif isinstance(fulfillment, list):
        for entry in fulfillment:
            if isinstance(entry, dict) and entry.get("display"):
                location = str(entry["display"])
                break

    if not title or not link:
        return None

    return ProbeListing(
        source="target",
        title=title,
        price=price,
        location=location,
        link=link,
        image=str(image) if image else None,
    )


def extract_from_redsky_json(data: dict[str, Any], *, limit: int) -> list[ProbeListing]:
    if not has_real_search_results(data):
        return []

    products = (data.get("data", {}).get("search") or {}).get("products") or []
    listings: list[ProbeListing] = []
    seen_links: set[str] = set()

    for product in products:
        if len(listings) >= limit:
            break
        if not isinstance(product, dict):
            continue
        listing = redsky_product_to_probe_listing(product)
        if listing is None or listing.link in seen_links:
            continue
        seen_links.add(listing.link)
        listings.append(listing)

    return listings


def extract_next_data_blob(html: str) -> Optional[dict[str, Any]]:
    soup = BeautifulSoup(html, "lxml")
    script = soup.find("script", id="__NEXT_DATA__")
    if not script or not script.get_text(strip=True):
        return None
    try:
        return json.loads(script.get_text())
    except json.JSONDecodeError:
        return None


def extract_from_next_data(html: str, *, limit: int) -> list[ProbeListing]:
    data = extract_next_data_blob(html)
    if not data:
        return []

    products: list[dict[str, Any]] = []

    def walk(obj: Any) -> None:
        if isinstance(obj, dict):
            if "tcin" in obj and "item" in obj and isinstance(obj.get("item"), dict):
                products.append(obj)
            for value in obj.values():
                walk(value)
        elif isinstance(obj, list):
            for value in obj:
                walk(value)

    walk(data.get("props", {}).get("pageProps", {}))

    listings: list[ProbeListing] = []
    seen_links: set[str] = set()
    for product in products:
        if len(listings) >= limit:
            break
        listing = redsky_product_to_probe_listing(product)
        if listing is None or listing.link in seen_links:
            continue
        seen_links.add(listing.link)
        listings.append(listing)
    return listings


def _card_to_probe_listing(card) -> Optional[ProbeListing]:
    title_el = card.select_one('[data-test="product-title"]')
    if title_el is None and card.get("data-test") == "product-title":
        title_el = card

    price_el = card.select_one('[data-test="product-price"]')
    shipping_el = card.select_one('[data-test="product-shipping"]') or card.select_one(
        '[data-test="fulfillment-type"]'
    )
    image_el = card.select_one('[data-test="productImage"]') or card.select_one("img")

    title = title_el.get_text(" ", strip=True) if title_el else None
    href = title_el.get("href") if title_el and title_el.name == "a" else None
    if not href and title_el:
        parent_a = title_el.find_parent("a")
        href = parent_a.get("href") if parent_a else None

    tcin = None
    if href:
        match = re.search(r"/A-(\d+)", href)
        if match:
            tcin = match.group(1)

    raw_price = price_el.get_text(" ", strip=True) if price_el else None
    link = normalize_link(href, tcin=tcin)
    location = shipping_el.get_text(" ", strip=True) if shipping_el else None
    image = image_el.get("src") if image_el else None

    if not title or not link:
        return None

    return ProbeListing(
        source="target",
        title=title,
        price=parse_price(raw_price),
        location=location or None,
        link=link,
        image=image,
    )


def extract_from_dom(html: str, *, limit: int) -> list[ProbeListing]:
    soup = BeautifulSoup(html, "lxml")
    cards: list = []
    for selector in CARD_SELECTORS:
        found = soup.select(selector)
        if found:
            cards = found
            break

    listings: list[ProbeListing] = []
    seen_links: set[str] = set()
    for card in cards:
        if len(listings) >= limit:
            break
        listing = _card_to_probe_listing(card)
        if listing is None or listing.link in seen_links:
            continue
        seen_links.add(listing.link)
        listings.append(listing)
    return listings


def extract_listings_from_html(html: str, *, limit: int) -> tuple[list[ProbeListing], str]:
    from_next = extract_from_next_data(html, limit=limit)
    if from_next:
        return from_next, "next_data_json"
    from_dom = extract_from_dom(html, limit=limit)
    if from_dom:
        return from_dom, "dom_fallback"
    return [], "none"


def detect_redsky_blocking(status_code: Optional[int], body: str) -> dict[str, Any]:
    triggered: list[str] = []
    lower = (body or "").lower()

    if status_code == 403:
        triggered.append("http_403")
    if status_code == 429:
        triggered.append("http_429")
    if "captcharel" in lower or "captchaabsoluteurl" in lower:
        triggered.append("redsky_captcha")
    if status_code == 400:
        triggered.append("http_400")

    return {
        "blocking_detected": bool(triggered),
        "triggered_indicators": triggered,
    }


def detect_html_blocking(
    status_code: Optional[int],
    title: str,
    html: str,
    final_url: str,
    initial_url: str,
    listing_count: int = 0,
) -> dict[str, Any]:
    title_lower = title.lower()
    html_lower = html.lower()
    triggered: list[str] = []

    if status_code == 403:
        triggered.append("http_403")
    if status_code == 429:
        triggered.append("http_429")

    for fragment in CHALLENGE_TITLE_FRAGMENTS:
        if fragment in title_lower:
            triggered.append(f"title:{fragment}")

    for marker in CHALLENGE_BODY_MARKERS:
        if marker.lower() in html_lower:
            triggered.append(f"body:{marker}")

    redirected_away = (
        "target.com" in final_url.lower()
        and "/s" not in final_url.lower()
        and final_url != initial_url
        and listing_count == 0
    )
    if redirected_away:
        triggered.append(f"redirect_away:{final_url}")

    body = BeautifulSoup(html, "lxml").body
    body_word_count = len(body.get_text(" ", strip=True).split()) if body else 0
    js_shell = body_word_count < 80 and len(html) > 500
    no_listings = listing_count == 0 and body_word_count < 200

    if js_shell and no_listings and not triggered:
        triggered.append("js_only_shell_no_listings")

    hard_block = bool(triggered) and listing_count == 0

    return {
        "blocking_detected": hard_block,
        "triggered_indicators": triggered,
        "body_word_count": body_word_count,
        "js_shell": js_shell,
        "no_listings": no_listings,
    }


def warm_target_session() -> requests.Session:
    session = requests.Session()
    try:
        session.get(TARGET_ORIGIN, headers=HEADERS, timeout=REQUEST_TIMEOUT)
    except requests.RequestException:
        pass
    return session


def fetch_redsky(query: str, *, session: Optional[requests.Session] = None) -> dict[str, Any]:
    session = session or warm_target_session()
    visitor_id = session.cookies.get("visitorId") or "vulture-probe"
    params = build_redsky_params(query, visitor_id=visitor_id)
    url = REDSKY_SEARCH_URL
    t0 = time.monotonic()
    error = None
    status_code = None
    body = ""
    data: Optional[dict[str, Any]] = None

    try:
        resp = session.get(url, params=params, headers=REDSKY_HEADERS, timeout=REQUEST_TIMEOUT)
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        status_code = resp.status_code
        body = resp.text
        if resp.status_code == 200:
            data = resp.json()
    except requests.exceptions.Timeout:
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        error = "request_timeout"
    except requests.exceptions.ConnectionError as exc:
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        error = f"connection_error: {exc}"
    except (requests.RequestException, json.JSONDecodeError) as exc:
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        error = f"request_error: {exc}"

    return {
        "method": "redsky_api",
        "query": query,
        "url": resp.request.url if "resp" in locals() else url,
        "status_code": status_code,
        "body": body,
        "data": data,
        "elapsed_ms": elapsed_ms,
        "error": error,
        "visitor_id": visitor_id,
    }


def fetch_requests_html(query: str, *, session: Optional[requests.Session] = None) -> dict[str, Any]:
    session = session or warm_target_session()
    url = build_search_url(query)
    t0 = time.monotonic()
    error = None
    status_code = None
    final_url = url
    html = ""
    redirect_chain: list[str] = []

    try:
        resp = session.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT, allow_redirects=True)
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        status_code = resp.status_code
        final_url = resp.url
        redirect_chain = [r.url for r in resp.history]
        html = resp.text
    except requests.exceptions.Timeout:
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        error = "request_timeout"
    except requests.exceptions.ConnectionError as exc:
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        error = f"connection_error: {exc}"
    except requests.exceptions.RequestException as exc:
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        error = f"request_error: {exc}"

    return {
        "method": "requests_html",
        "query": query,
        "url": url,
        "final_url": final_url,
        "status_code": status_code,
        "redirect_chain": redirect_chain,
        "html": html,
        "html_length": len(html),
        "elapsed_ms": elapsed_ms,
        "error": error,
    }


def fetch_cffi_html(query: str) -> dict[str, Any]:
    url = build_search_url(query)
    t0 = time.monotonic()
    try:
        from curl_cffi import requests as cffi_requests
    except ImportError:
        return {
            "method": "curl_cffi_html",
            "query": query,
            "url": url,
            "final_url": url,
            "status_code": None,
            "html": "",
            "html_length": 0,
            "elapsed_ms": int((time.monotonic() - t0) * 1000),
            "error": "curl_cffi_not_installed",
        }

    error = None
    status_code = None
    final_url = url
    html = ""

    try:
        resp = cffi_requests.get(url, impersonate="chrome124", timeout=REQUEST_TIMEOUT, allow_redirects=True)
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        status_code = resp.status_code
        final_url = resp.url
        html = resp.text
    except Exception as exc:
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        error = f"curl_cffi_error: {exc}"

    return {
        "method": "curl_cffi_html",
        "query": query,
        "url": url,
        "final_url": final_url,
        "status_code": status_code,
        "html": html,
        "html_length": len(html),
        "elapsed_ms": elapsed_ms,
        "error": error,
    }


def fetch_playwright_html(query: str) -> dict[str, Any]:
    url = build_search_url(query)
    t0 = time.monotonic()
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return {
            "method": "playwright_html",
            "query": query,
            "url": url,
            "final_url": url,
            "status_code": None,
            "html": "",
            "html_length": 0,
            "elapsed_ms": int((time.monotonic() - t0) * 1000),
            "error": "playwright_not_installed",
        }

    error = None
    status_code = None
    final_url = url
    html = ""

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=["--disable-blink-features=AutomationControlled"],
            )
            context = browser.new_context(
                user_agent=HEADERS["User-Agent"],
                viewport={"width": 1366, "height": 768},
                locale="en-US",
            )
            page = context.new_page()
            page.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
            )
            resp = page.goto(url, wait_until="domcontentloaded", timeout=PLAYWRIGHT_TIMEOUT_MS)
            page.wait_for_timeout(PLAYWRIGHT_SETTLE_MS)
            for label in ("Continue shopping", "Accept", "Got it"):
                try:
                    btn = page.get_by_role("button", name=label)
                    if btn.count():
                        btn.first.click(timeout=2000)
                        page.wait_for_timeout(1000)
                        break
                except Exception:
                    pass
            status_code = resp.status if resp else None
            final_url = page.url
            html = page.content()
            browser.close()
        elapsed_ms = int((time.monotonic() - t0) * 1000)
    except Exception as exc:
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        error = f"playwright_error: {exc}"

    return {
        "method": "playwright_html",
        "query": query,
        "url": url,
        "final_url": final_url,
        "status_code": status_code,
        "html": html,
        "html_length": len(html),
        "elapsed_ms": elapsed_ms,
        "error": error,
    }


def probe_query(
    query: str,
    *,
    limit: int = MAX_CANDIDATES,
    use_cffi: bool = False,
    use_playwright: bool = False,
    html_only: bool = False,
    redsky_only: bool = False,
    quiet: bool = False,
) -> dict[str, Any]:
    if not quiet:
        print_section(f"TARGET PROBE — query={query!r}")

    session = warm_target_session()
    listings: list[ProbeListing] = []
    extraction_method = "none"
    method_used = "none"
    blocking_detected = False
    blocking_indicators: list[str] = []
    page_title = "(not fetched)"
    status_code = None
    error = None
    filler_warning = False

    if not html_only:
        redsky = fetch_redsky(query, session=session)
        if not quiet:
            print(f"  [Redsky] status={redsky['status_code']} elapsed={redsky['elapsed_ms']}ms visitor={redsky.get('visitor_id')!r}")
        if redsky["error"]:
            if not quiet:
                print(f"  [Redsky] ERROR: {redsky['error']}")
            error = redsky["error"]
        elif redsky.get("data") is not None:
            blocking = detect_redsky_blocking(redsky["status_code"], redsky.get("body", ""))
            if redsky["status_code"] == 200 and not blocking["blocking_detected"]:
                if has_real_search_results(redsky["data"]):
                    listings = extract_from_redsky_json(redsky["data"], limit=limit)
                    extraction_method = "redsky_json"
                    method_used = "redsky_api"
                else:
                    filler_warning = True
                    if not quiet:
                        print("  [Redsky] WARNING: zero-result filler detected (empty facet_list)")
            else:
                blocking_detected = blocking["blocking_detected"]
                blocking_indicators.extend(blocking["triggered_indicators"])
                if not quiet:
                    print(f"  [Redsky] blocking={blocking_detected} indicators={blocking['triggered_indicators']}")
        elif redsky["status_code"]:
            blocking = detect_redsky_blocking(redsky["status_code"], redsky.get("body", ""))
            blocking_detected = blocking["blocking_detected"] or redsky["status_code"] != 200
            blocking_indicators.extend(blocking["triggered_indicators"])
            status_code = redsky["status_code"]

    if not listings and not redsky_only:
        if use_playwright:
            html_fetch = fetch_playwright_html(query)
            method_used = "playwright_html"
        elif use_cffi:
            html_fetch = fetch_cffi_html(query)
            method_used = "curl_cffi_html"
        else:
            html_fetch = fetch_requests_html(query, session=session)
            method_used = "requests_html"

        if not quiet:
            print(f"  [HTML] method={html_fetch['method']} status={html_fetch['status_code']} len={html_fetch.get('html_length', 0):,}")

        if html_fetch["error"]:
            if not quiet:
                print(f"  [HTML] ERROR: {html_fetch['error']}")
            error = error or html_fetch["error"]
        else:
            soup = BeautifulSoup(html_fetch["html"], "lxml")
            page_title = soup.title.get_text(strip=True) if soup.title else "(no title)"
            listings, extraction_method = extract_listings_from_html(html_fetch["html"], limit=limit)
            html_blocking = detect_html_blocking(
                html_fetch["status_code"],
                page_title,
                html_fetch["html"],
                html_fetch["final_url"],
                html_fetch["url"],
                listing_count=len(listings),
            )
            if html_blocking["blocking_detected"] and not listings:
                blocking_detected = True
                blocking_indicators.extend(html_blocking["triggered_indicators"])
            status_code = html_fetch["status_code"]

    if not quiet:
        print(f"  Page title      : {page_title!r}")
        print(f"  Method used     : {method_used}")
        print(f"  Extraction      : {extraction_method}")
        print(f"  Blocking detected : {blocking_detected}")
        if blocking_indicators:
            print(f"  Blocking indicators: {blocking_indicators}")
        if filler_warning:
            print("  WARNING         : Redsky returned recommendation filler only")
        print(f"  Extracted listings: {len(listings)}")
        if listings:
            print("  First listings:")
            for i, listing in enumerate(listings, 1):
                print(f"\n    [{i}]")
                for key, value in asdict(listing).items():
                    print(f"      {key:14s}: {value!r}")
        else:
            print("  No listings extracted.")
            if blocking_detected or filler_warning or error:
                print("  WARNING         : safe empty results")

    viable = len(listings) > 0 and not blocking_detected

    return {
        "query": query,
        "method": method_used,
        "requests_viable": viable,
        "listings": listings,
        "blocking_detected": blocking_detected,
        "extraction_method": extraction_method,
        "page_title": page_title,
        "status_code": status_code,
        "error": error,
        "filler_warning": filler_warning,
        "blocking_indicators": blocking_indicators,
    }


def print_final_assessment(results: list[dict[str, Any]]) -> None:
    print_section("FINAL ASSESSMENT — ALL QUERIES")

    total = len(results)
    viable = sum(1 for r in results if r.get("requests_viable"))
    with_listings = sum(1 for r in results if r.get("listings"))
    blocked = sum(1 for r in results if r.get("blocking_detected"))

    print(f"  Queries probed              : {total}")
    print(f"  viable (per query)          : {viable}/{total}")
    print(f"  queries with listings       : {with_listings}/{total}")
    print(f"  queries with blocking       : {blocked}/{total}")
    print()

    methods = {r.get("method") for r in results if r.get("method")}
    print(f"  Fetch methods used          : {', '.join(sorted(methods)) or 'none'}")

    redsky_ok = any(r.get("extraction_method") == "redsky_json" for r in results)
    dom_ok = any(r.get("extraction_method") in ("dom_fallback", "next_data_json") for r in results)

    print(f"  Redsky JSON path viable?               : {'YES' if redsky_ok else 'NO / blocked from this host'}")
    print(f"  HTML/Playwright path viable?           : {'YES' if dom_ok else 'NO / sparse SSR'}")
    print(f"  Should remain probe-only?              : YES until repeated Raven residential evidence")
    print(f"  Promote to experimental adapter?       : NO — not until Redsky or DOM path stable on Raven")
    print()

    if with_listings == 0:
        print("  VERDICT:")
        print("    No live listings from this environment.")
        print("    Re-run from Raven residential IP — Redsky is the preferred path.")
        print("    Do NOT register in adapters/registry.py yet.")
    else:
        print("  RECOMMENDED NEXT STEP:")
        print("    Re-run from Raven over multiple days; sketch experimental adapter if stable.")
    print()


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Target reconnaissance probe (experimental, isolated)")
    parser.add_argument("--query", action="append", dest="queries", help="Search term (repeatable)")
    parser.add_argument("--limit", type=int, default=MAX_CANDIDATES, help=f"Max listings per query (default: {MAX_CANDIDATES})")
    parser.add_argument("--cffi", action="store_true", help="Use curl_cffi for HTML fetch fallback")
    parser.add_argument("--playwright", action="store_true", help="Use Playwright for HTML fetch fallback")
    parser.add_argument("--html-only", action="store_true", help="Skip Redsky API; HTML/Playwright only")
    parser.add_argument("--redsky-only", action="store_true", help="Skip HTML fallback")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON summary")
    args = parser.parse_args(argv)

    queries = args.queries if args.queries else DEFAULT_QUERIES

    if not args.json:
        print_section("Target Recon Probe — Redsky API + HTML/Playwright fallback")
        print(f"  Queries : {queries}")
        print(f"  Limit   : {args.limit}")

    results = [
        probe_query(
            q,
            limit=args.limit,
            use_cffi=args.cffi,
            use_playwright=args.playwright,
            html_only=args.html_only,
            redsky_only=args.redsky_only,
            quiet=args.json,
        )
        for q in queries
    ]

    if args.json:
        payload = []
        for r in results:
            payload.append(
                {
                    **{k: v for k, v in r.items() if k != "listings"},
                    "listings": [asdict(lst) for lst in r.get("listings", [])],
                }
            )
        print(json.dumps(payload, indent=2))
        return 0

    print_final_assessment(results)
    return 0


if __name__ == "__main__":
    sys.exit(main())
