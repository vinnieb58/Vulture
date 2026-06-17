"""
Walmart candidate-source probe
==============================
Reconnaissance only. Does NOT write to SQLite, send Discord alerts,
or modify adapters/registry.py.

Usage:
    python experiments/adapters/walmart_probe.py
    python experiments/adapters/walmart_probe.py --query "steam deck" --limit 5
    python experiments/adapters/walmart_probe.py --query "65 inch tv" --limit 5
    python experiments/adapters/walmart_probe.py --query "rtx 4070" --playwright
    python experiments/adapters/walmart_probe.py --query "steam deck" --cffi

Default queries (when --query is omitted):
    steam deck, macbook screen, rtx 4070, 65 inch tv, dyson vacuum

Parsing strategy (when HTML is not blocked):
    1. Prefer ``__NEXT_DATA__`` JSON:
       props.pageProps.initialData.searchResult.itemStacks[].items
       Filter ``__typename == "Product"``.
    2. CSS fallback: ``[data-item-id]`` cards with
       ``[data-automation-id="product-title"]`` and ``[itemprop='price']``.

Fetch escalation:
    requests (default) -> optional curl_cffi (--cffi) -> optional Playwright (--playwright)

Known blocking (cloud/datacenter IP, 2026-06):
    PerimeterX redirects search to ``/blocked?url=...`` with title "Robot or human?".
    curl_cffi Chrome124 impersonation and headless Playwright hit the same wall here.
    Re-validate from Raven residential IP before any runtime adapter registration.
"""

from __future__ import annotations

import argparse
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

log = logging.getLogger("walmart_probe")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

WALMART_ORIGIN = "https://www.walmart.com"
SEARCH_BASE = f"{WALMART_ORIGIN}/search"

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

REQUEST_TIMEOUT = 25
PLAYWRIGHT_TIMEOUT_MS = 45_000
PLAYWRIGHT_SETTLE_MS = 3_000
MAX_CANDIDATES = 5

CHALLENGE_TITLE_FRAGMENTS = [
    "robot or human",
    "access denied",
    "just a moment",
    "attention required",
    "checking your browser",
    "verify you are human",
    "captcha",
    "security check",
    "let us know you're not a robot",
]

CHALLENGE_BODY_MARKERS = [
    "px-captcha",
    "perimeterx",
    "_px",
    "g-recaptcha",
    "cf-browser-verification",
    "enable javascript and cookies",
    "unusual traffic",
    "automated queries",
    "bot detection",
    "access denied",
]

CARD_SELECTORS = (
    "[data-item-id]",
    "[data-testid='list-view'] [role='group']",
    "div[data-item-id]",
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
    return f"{SEARCH_BASE}?q={quote_plus(query)}"


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
    text = str(raw).replace(",", "")
    match = re.search(r"\$?\s*([\d,]+)(?:\.\d+)?", text)
    if not match:
        return None
    try:
        return int(match.group(1).replace(",", ""))
    except ValueError:
        return None


def normalize_link(href: Optional[str], *, item_id: Optional[str] = None) -> Optional[str]:
    if href:
        href = href.strip()
        if href.startswith("//"):
            href = "https:" + href
        elif href.startswith("/"):
            href = urljoin(WALMART_ORIGIN, href)
        if href.startswith("http"):
            return href.split("?")[0].rstrip("/")
    if item_id:
        return f"{WALMART_ORIGIN}/ip/{item_id}"
    return None


def _location_from_item(item: dict[str, Any]) -> Optional[str]:
    parts: list[str] = []
    avail = item.get("availabilityStatusV2") or {}
    if isinstance(avail, dict):
        display = avail.get("display") or avail.get("value")
        if display:
            parts.append(str(display).strip())

    for summary in item.get("fulfillmentSummary") or []:
        if not isinstance(summary, dict):
            continue
        text = summary.get("fulfillmentText") or summary.get("display")
        if text:
            parts.append(str(text).strip())

    badge = item.get("fulfillmentBadge")
    if badge:
        parts.append(str(badge).strip())

    if not parts:
        status = item.get("availabilityStatus")
        if status:
            parts.append(str(status).strip())

    if not parts:
        return None
    return " · ".join(dict.fromkeys(parts))


def _price_from_item(item: dict[str, Any]) -> Optional[int]:
    price_info = item.get("priceInfo") or {}
    if isinstance(price_info, dict):
        current = price_info.get("currentPrice") or {}
        if isinstance(current, dict):
            for key in ("price", "priceString"):
                parsed = parse_price(current.get(key))
                if parsed is not None:
                    return parsed
    for key in ("price", "primaryOfferPrice"):
        parsed = parse_price(item.get(key))
        if parsed is not None:
            return parsed
    return None


def _image_from_item(item: dict[str, Any]) -> Optional[str]:
    image_info = item.get("imageInfo") or {}
    if isinstance(image_info, dict):
        for key in ("thumbnailUrl", "thumbnail", "imageUrl"):
            url = image_info.get(key)
            if url:
                return str(url)
    return None


def item_dict_to_probe_listing(item: dict[str, Any]) -> Optional[ProbeListing]:
    if item.get("__typename") not in (None, "Product"):
        return None

    title = (item.get("name") or item.get("title") or "").strip()
    item_id = str(item.get("usItemId") or item.get("id") or "").strip() or None
    link = normalize_link(item.get("canonicalUrl") or item.get("productPageUrl"), item_id=item_id)
    price = _price_from_item(item)
    location = _location_from_item(item)
    image = _image_from_item(item)

    if not title or not link:
        return None

    return ProbeListing(
        source="walmart",
        title=title,
        price=price,
        location=location,
        link=link,
        image=image,
    )


def extract_next_data_blob(html: str) -> Optional[dict[str, Any]]:
    soup = BeautifulSoup(html, "lxml")
    script = soup.find("script", id="__NEXT_DATA__")
    if not script or not script.get_text(strip=True):
        return None
    try:
        return json.loads(script.get_text())
    except json.JSONDecodeError:
        return None


def extract_items_from_next_data(data: dict[str, Any]) -> list[dict[str, Any]]:
    search_result = (
        data.get("props", {})
        .get("pageProps", {})
        .get("initialData", {})
        .get("searchResult")
    )
    if not isinstance(search_result, dict):
        return []

    items: list[dict[str, Any]] = []
    for stack in search_result.get("itemStacks") or []:
        if not isinstance(stack, dict):
            continue
        for item in stack.get("items") or []:
            if isinstance(item, dict):
                items.append(item)
    return items


def extract_from_next_data(html: str, *, limit: int) -> list[ProbeListing]:
    data = extract_next_data_blob(html)
    if not data:
        return []

    listings: list[ProbeListing] = []
    seen_links: set[str] = set()

    for item in extract_items_from_next_data(data):
        if len(listings) >= limit:
            break
        if item.get("__typename") not in (None, "Product"):
            continue
        listing = item_dict_to_probe_listing(item)
        if listing is None or listing.link in seen_links:
            continue
        seen_links.add(listing.link)
        listings.append(listing)

    return listings


def _card_to_probe_listing(card) -> Optional[ProbeListing]:
    item_id = (card.get("data-item-id") or "").strip() or None
    title_el = card.select_one('[data-automation-id="product-title"]') or card.select_one(
        "a[link-identifier='itemClick']"
    )
    price_el = card.select_one("[itemprop='price']") or card.select_one(
        "[data-automation-id='product-price']"
    )
    shipping_el = card.select_one("[data-testid='product-shipping']") or card.select_one(
        "[data-automation-id='fulfillment-badge']"
    )
    image_el = card.select_one("img[data-testid='product-image']") or card.select_one("img")

    title = title_el.get_text(" ", strip=True) if title_el else None
    href = title_el.get("href") if title_el and title_el.name == "a" else None
    if not href and title_el:
        parent_a = title_el.find_parent("a")
        href = parent_a.get("href") if parent_a else None

    raw_price = None
    if price_el:
        raw_price = price_el.get("content") or price_el.get_text(" ", strip=True)

    link = normalize_link(href, item_id=item_id)
    location = shipping_el.get_text(" ", strip=True) if shipping_el else None
    image = image_el.get("src") if image_el else None

    if not title or not link:
        return None

    return ProbeListing(
        source="walmart",
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


def extract_listings(html: str, *, limit: int) -> tuple[list[ProbeListing], str]:
    """Return listings and the extraction method used."""
    from_json = extract_from_next_data(html, limit=limit)
    if from_json:
        return from_json, "next_data_json"
    from_dom = extract_from_dom(html, limit=limit)
    if from_dom:
        return from_dom, "dom_fallback"
    return [], "none"


def detect_blocking(
    status_code: Optional[int],
    title: str,
    html: str,
    final_url: str,
    initial_url: str,
    listing_count: int = 0,
) -> dict[str, Any]:
    title_lower = title.lower()
    html_lower = html.lower()
    final_lower = (final_url or "").lower()
    triggered: list[str] = []

    if status_code == 403:
        triggered.append("http_403")
    if status_code == 429:
        triggered.append("http_429")
    if "/blocked" in final_lower:
        triggered.append("redirect:/blocked")

    for fragment in CHALLENGE_TITLE_FRAGMENTS:
        if fragment in title_lower:
            triggered.append(f"title:{fragment}")

    for marker in CHALLENGE_BODY_MARKERS:
        if marker.lower() in html_lower:
            triggered.append(f"body:{marker}")

    redirected_away = (
        "walmart.com" in final_lower
        and "/search" not in final_lower
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


def fetch_requests(query: str) -> dict[str, Any]:
    url = build_search_url(query)
    t0 = time.monotonic()
    error = None
    status_code = None
    final_url = url
    html = ""
    redirect_chain: list[str] = []

    try:
        resp = requests.get(
            url,
            headers=HEADERS,
            timeout=REQUEST_TIMEOUT,
            allow_redirects=True,
        )
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
        "method": "requests",
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


def fetch_cffi(query: str) -> dict[str, Any]:
    url = build_search_url(query)
    t0 = time.monotonic()
    try:
        from curl_cffi import requests as cffi_requests
    except ImportError:
        return {
            "method": "curl_cffi",
            "query": query,
            "url": url,
            "final_url": url,
            "status_code": None,
            "redirect_chain": [],
            "html": "",
            "html_length": 0,
            "elapsed_ms": int((time.monotonic() - t0) * 1000),
            "error": "curl_cffi_not_installed",
        }

    error = None
    status_code = None
    final_url = url
    html = ""
    redirect_chain: list[str] = []

    try:
        resp = cffi_requests.get(
            url,
            impersonate="chrome124",
            timeout=REQUEST_TIMEOUT,
            allow_redirects=True,
        )
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        status_code = resp.status_code
        final_url = resp.url
        redirect_chain = [r.url for r in getattr(resp, "history", [])]
        html = resp.text
    except Exception as exc:
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        error = f"curl_cffi_error: {exc}"

    return {
        "method": "curl_cffi",
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


def fetch_playwright(query: str) -> dict[str, Any]:
    url = build_search_url(query)
    t0 = time.monotonic()
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return {
            "method": "playwright",
            "query": query,
            "url": url,
            "final_url": url,
            "status_code": None,
            "redirect_chain": [],
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
            status_code = resp.status if resp else None
            final_url = page.url
            html = page.content()
            browser.close()
        elapsed_ms = int((time.monotonic() - t0) * 1000)
    except Exception as exc:
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        error = f"playwright_error: {exc}"

    return {
        "method": "playwright",
        "query": query,
        "url": url,
        "final_url": final_url,
        "status_code": status_code,
        "redirect_chain": [],
        "html": html,
        "html_length": len(html),
        "elapsed_ms": elapsed_ms,
        "error": error,
    }


def fetch_query(
    query: str,
    *,
    use_cffi: bool = False,
    use_playwright: bool = False,
) -> dict[str, Any]:
    if use_playwright:
        return fetch_playwright(query)
    if use_cffi:
        return fetch_cffi(query)
    return fetch_requests(query)


def probe_query(
    query: str,
    *,
    limit: int = MAX_CANDIDATES,
    use_cffi: bool = False,
    use_playwright: bool = False,
    quiet: bool = False,
) -> dict[str, Any]:
    if not quiet:
        print_section(f"WALMART PROBE — query={query!r}")

    fetch = fetch_query(query, use_cffi=use_cffi, use_playwright=use_playwright)

    if not quiet:
        print(f"  Method          : {fetch['method']}")
        print(f"  Query           : {fetch['query']!r}")
        print(f"  Initial URL     : {fetch['url']}")
        print(f"  Final URL       : {fetch['final_url']}")
        print(f"  HTTP status     : {fetch['status_code']}")
        print(f"  Response length : {fetch['html_length']:,} bytes")
        print(f"  Elapsed         : {fetch['elapsed_ms']} ms")

    if fetch.get("redirect_chain") and not quiet:
        print(f"  Redirect hops   : {len(fetch['redirect_chain'])}")
        for i, hop in enumerate(fetch["redirect_chain"], 1):
            print(f"    hop {i}: {hop}")

    if fetch["error"]:
        if not quiet:
            print(f"  ERROR           : {fetch['error']}")
            print("  WARNING         : returning safe empty results")
        return {
            "query": query,
            "method": fetch["method"],
            "requests_viable": False,
            "listings": [],
            "blocking_detected": True,
            "error": fetch["error"],
            "extraction_method": "none",
        }

    soup = BeautifulSoup(fetch["html"], "lxml")
    page_title = soup.title.get_text(strip=True) if soup.title else "(no title)"
    listings, extraction_method = extract_listings(fetch["html"], limit=limit)

    blocking = detect_blocking(
        fetch["status_code"],
        page_title,
        fetch["html"],
        fetch["final_url"],
        fetch["url"],
        listing_count=len(listings),
    )

    if not quiet:
        print(f"  Page title      : {page_title!r}")
        print(f"  Extraction      : {extraction_method}")
        print(f"  Blocking detected : {blocking['blocking_detected']}")
        if blocking["triggered_indicators"]:
            print(f"  Blocking indicators: {blocking['triggered_indicators']}")
        print(f"  Body word count   : {blocking['body_word_count']}")
        print(f"  Extracted listings: {len(listings)}")

        if listings:
            print("  First listings:")
            for i, listing in enumerate(listings, 1):
                print(f"\n    [{i}]")
                for key, value in asdict(listing).items():
                    print(f"      {key:14s}: {value!r}")
        else:
            print("  No listings extracted.")
            if blocking["blocking_detected"]:
                print("  WARNING         : bot block or empty parse — safe empty results")

    requests_viable = (
        fetch["status_code"] == 200
        and not blocking["blocking_detected"]
        and len(listings) > 0
    )

    return {
        "query": query,
        "method": fetch["method"],
        "requests_viable": requests_viable,
        "listings": listings,
        "blocking_detected": blocking["blocking_detected"],
        "extraction_method": extraction_method,
        "page_title": page_title,
        "status_code": fetch["status_code"],
        "html_length": fetch["html_length"],
        "error": None,
        "blocking": blocking,
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

    methods = {r.get("method") for r in results}
    print(f"  Fetch methods used          : {', '.join(sorted(methods)) or 'requests'}")

    has_title_price_link = with_listings > 0 and all(
        all(lst.title and lst.link and lst.price is not None for lst in r.get("listings", [])[:1])
        for r in results
        if r.get("listings")
    )
    has_location = any(
        lst.location for r in results for lst in r.get("listings", [])
    )
    stable_links = with_listings > 0 and all(
        all("walmart.com/ip/" in lst.link for lst in r.get("listings", [])[:1])
        for r in results
        if r.get("listings")
    )

    needs_browser = with_listings == 0 and blocked == total
    requests_enough = viable == total and with_listings == total

    print(f"  Is requests enough?                    : {'YES' if requests_enough else 'NO'}")
    print(f"  Is browser automation required?        : {'LIKELY YES' if needs_browser else 'UNCERTAIN — validate on Raven'}")
    print(f"  Title/price/link reliably visible?     : {'YES' if has_title_price_link else 'PARTIAL/NO (blocked or untested live)'}")
    print(f"  Shipping/availability visible?         : {'YES' if has_location else 'PARTIAL/NO'}")
    print(f"  Stable product links for dedupe?       : {'YES' if stable_links else 'UNCERTAIN'}")
    print(f"  Viable for future runtime adapter?     : {'PROMISING (parser ready; live fetch blocked here)' if blocked == total else 'PROMISING (parser ready)' if has_title_price_link else 'PROBE ONLY — validate on Raven'}")
    print(f"  Should remain probe-only?              : YES until repeated Raven residential evidence")
    print()

    if blocked == total:
        print("  VERDICT:")
        print("    PerimeterX bot wall from this environment (redirect to /blocked).")
        print("    Parser targets __NEXT_DATA__ JSON; re-run from Raven residential IP.")
        print("    Do NOT register in adapters/registry.py yet.")
    elif requests_enough:
        print("  RECOMMENDED NEXT STEP:")
        print("    Re-run from Raven over multiple days; then sketch experimental adapter.")
    else:
        print("  RECOMMENDED NEXT STEP:")
        print("    Try --cffi or --playwright; inspect saved HTML if still blocked.")
    print()


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Walmart reconnaissance probe (experimental, isolated)"
    )
    parser.add_argument(
        "--query",
        action="append",
        dest="queries",
        help="Search term (repeatable). Default: built-in query list.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=MAX_CANDIDATES,
        help=f"Max listings per query (default: {MAX_CANDIDATES})",
    )
    parser.add_argument(
        "--cffi",
        action="store_true",
        help="Use curl_cffi Chrome TLS impersonation instead of requests",
    )
    parser.add_argument(
        "--playwright",
        action="store_true",
        help="Use headless Playwright Chromium (slow; last resort)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print machine-readable JSON summary to stdout",
    )
    args = parser.parse_args(argv)

    queries = args.queries if args.queries else DEFAULT_QUERIES

    if not args.json:
        print_section("Walmart Recon Probe — requests / optional cffi / Playwright")
        print(f"  Queries : {queries}")
        print(f"  Limit   : {args.limit}")
        if args.playwright:
            print("  Mode    : Playwright")
        elif args.cffi:
            print("  Mode    : curl_cffi")
        else:
            print("  Mode    : requests")

    results = [
        probe_query(
            q,
            limit=args.limit,
            use_cffi=args.cffi,
            use_playwright=args.playwright,
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
