"""
Newegg candidate-source probe
==============================
Reconnaissance only. Does NOT write to SQLite, send Discord alerts,
or touch the production adapter registry.

Usage:
    python experiments/adapters/newegg_probe.py
    python experiments/adapters/newegg_probe.py "rtx 4070"
    python experiments/adapters/newegg_probe.py "rtx 4070" --limit 5

Default queries (when no query arg is given):
    rtx 4070, ryzen 5600, gaming laptop, macbook, 2tb nvme ssd

Goal: determine whether Newegg is a viable future Vulture adapter for
computer_parts, gaming, electronics, laptops/prebuilt PCs, and retail.

Viability questions answered:
    1. Is requests enough?
    2. Is browser automation required?
    3. Are title, price, and link reliably visible?
    4. Is availability/shipping visible?
    5. Does Newegg expose stable product links suitable for dedupe?
    6. Does Newegg look viable for a future runtime adapter?
    7. Should it remain probe-only?
"""

from __future__ import annotations

import argparse
import re
import sys
import time
from urllib.parse import quote_plus, urljoin

import requests
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

NEWEGG_ORIGIN = "https://www.newegg.com"
SEARCH_BASE = f"{NEWEGG_ORIGIN}/p/pl"

DEFAULT_QUERIES = [
    "rtx 4070",
    "ryzen 5600",
    "gaming laptop",
    "macbook",
    "2tb nvme ssd",
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
    # Omit "br" — requests needs brotli/brotlicffi to decode Brotli; without it
    # Newegg may return a truncated or alternate response body.
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
MAX_CANDIDATES = 5

# Product card selectors commonly used on Newegg search pages
CARD_SELECTORS = [
    ".item-cell",
    ".item-container",
    "div.item-cell",
    "div.item-container",
    ".list-wrap .item-cell",
    ".row-body .item-cell",
]

CANDIDATE_SELECTOR_COUNTS = [
    ".item-cell",
    ".item-container",
    "a.item-title",
    ".price-current",
    ".item-promo",
    ".item-stock",
    ".item-shipping",
    ".price-ship",
    ".item-msg",
    ".item-rating",
    ".item-rating-num",
    "i.rating",
    "[class*='item-rating']",
]

CHALLENGE_TITLE_FRAGMENTS = [
    "access denied",
    "just a moment",
    "attention required",
    "checking your browser",
    "ddos protection",
    "please wait",
    "security check",
    "verify you are human",
    "robot or human",
    "captcha",
]

CHALLENGE_BODY_MARKERS = [
    "cf-browser-verification",
    "Enable JavaScript and cookies to continue",
    "Checking your browser before accessing",
    "akamai-bm-telemetry",
    "_akamai_edgescape",
    "px-captcha",
    "PerimeterX",
    "datadome",
    "incapsula",
    "prove you are human",
    "human verification",
    "unusual traffic",
    "automated queries",
    "bot detection",
    "access denied",
    "g-recaptcha",
    "www.google.com/recaptcha",
]

# CDN resource paths — present on normal pages, not active challenge walls
CDN_ONLY_MARKERS = [
    "cdn-cgi/challenge-platform",
]

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def build_search_url(query: str) -> str:
    return f"{SEARCH_BASE}?d={quote_plus(query)}"


def print_section(title: str) -> None:
    print()
    print("=" * 72)
    print(f"  {title}")
    print("=" * 72)


def parse_price(raw: str | None) -> int | None:
    if not raw:
        return None
    match = re.search(r"\$?\s*([\d,]+)(?:\.\d+)?", raw.replace(",", ""))
    if match:
        try:
            return int(match.group(1).replace(",", ""))
        except ValueError:
            return None
    return None


def normalize_link(href: str | None) -> str | None:
    if not href:
        return None
    href = href.strip()
    if href.startswith("//"):
        return "https:" + href
    if href.startswith("/"):
        return urljoin(NEWEGG_ORIGIN, href)
    if href.startswith("http"):
        return href.split("?")[0] if "/p/" in href or "/Product/" in href else href
    return href


def detect_blocking(
    status_code: int | None,
    title: str,
    html: str,
    final_url: str,
    initial_url: str,
    product_card_count: int = 0,
) -> dict:
    title_lower = title.lower()
    html_lower = html.lower()
    triggered: list[str] = []
    cdn_only: list[str] = []

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

    for marker in CDN_ONLY_MARKERS:
        if marker.lower() in html_lower:
            cdn_only.append(f"cdn:{marker}")

    redirected_away = (
        "newegg.com" in (final_url or "").lower()
        and "/p/pl" not in (final_url or "")
        and final_url != initial_url
    )
    if redirected_away:
        triggered.append(f"redirect_away:{final_url}")

    body_text = BeautifulSoup(html, "lxml").body
    body_word_count = len(body_text.get_text(" ", strip=True).split()) if body_text else 0
    js_shell = body_word_count < 80 and len(html) > 500
    no_product_cards = product_card_count == 0 and body_word_count < 200

    if js_shell and no_product_cards:
        triggered.append("js_only_shell_no_product_cards")

    # Cloudflare CDN scripts appear on normal Newegg pages when cards are present
    effective_triggered = list(triggered)
    if cdn_only and product_card_count >= 5 and body_word_count > 400:
        effective_triggered = triggered
    elif cdn_only and not triggered:
        effective_triggered = cdn_only

    hard_block = bool(triggered) or (bool(cdn_only) and product_card_count == 0)

    return {
        "blocking_detected": hard_block,
        "triggered_indicators": effective_triggered,
        "cdn_only_indicators": cdn_only,
        "body_word_count": body_word_count,
        "js_shell": js_shell,
        "no_product_cards": no_product_cards,
    }


def count_selectors(soup: BeautifulSoup) -> dict[str, int]:
    counts: dict[str, int] = {}
    for selector in CANDIDATE_SELECTOR_COUNTS:
        try:
            counts[selector] = len(soup.select(selector))
        except Exception:
            counts[selector] = 0
    return counts


def extract_candidates(soup: BeautifulSoup, limit: int = MAX_CANDIDATES) -> list[dict]:
    cards: list = []
    matched_selector: str | None = None

    for selector in CARD_SELECTORS:
        found = soup.select(selector)
        if found:
            cards = found
            matched_selector = selector
            break

    if not cards:
        return []

    results: list[dict] = []
    for card in cards[:limit]:
        title_el = card.select_one("a.item-title") or card.select_one(".item-title")
        price_el = card.select_one(".price-current") or card.select_one(".price")
        link_el = title_el if title_el and title_el.name == "a" else card.select_one("a.item-title")
        shipping_el = (
            card.select_one(".price-ship")
            or card.select_one(".item-shipping")
            or card.select_one(".item-msg")
        )
        stock_el = card.select_one(".item-stock") or card.select_one(".item-operating-days")
        promo_el = card.select_one(".item-promo") or card.select_one(".item-promo-new")
        rating_el = (
            card.select_one(".item-rating-num")
            or card.select_one(".item-rating")
            or card.select_one("i.rating")
        )

        title = title_el.get_text(" ", strip=True) if title_el else None
        raw_price = price_el.get_text(" ", strip=True) if price_el else None
        link = normalize_link(link_el.get("href") if link_el else None)
        shipping = shipping_el.get_text(" ", strip=True) if shipping_el else None
        stock = stock_el.get_text(" ", strip=True) if stock_el else None
        promo = promo_el.get_text(" ", strip=True) if promo_el else None
        rating = rating_el.get_text(" ", strip=True) if rating_el else None

        raw_snippet = card.get_text(" ", strip=True)
        if len(raw_snippet) > 240:
            raw_snippet = raw_snippet[:237] + "..."

        if not title and not link:
            continue

        results.append(
            {
                "title": title,
                "price": parse_price(raw_price),
                "price_raw": raw_price,
                "link": link,
                "shipping": shipping,
                "stock": stock,
                "promo": promo,
                "rating": rating,
                "raw_snippet": raw_snippet,
                "card_selector": matched_selector,
            }
        )

    return results


def fetch_query(query: str) -> dict:
    url = build_search_url(query)
    t0 = time.monotonic()
    error = None
    status_code = None
    final_url = url
    html = ""
    redirect_chain: list[str] = []

    try:
        session = requests.Session()
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


def probe_query(query: str, limit: int = MAX_CANDIDATES) -> dict:
    print_section(f"NEWEGG PROBE — query={query!r}")

    fetch = fetch_query(query)

    print(f"  Query           : {fetch['query']!r}")
    print(f"  Initial URL     : {fetch['url']}")
    print(f"  Final URL       : {fetch['final_url']}")
    print(f"  HTTP status     : {fetch['status_code']}")
    print(f"  Response length : {fetch['html_length']:,} bytes")
    print(f"  Elapsed         : {fetch['elapsed_ms']} ms")

    if fetch["redirect_chain"]:
        print(f"  Redirect hops   : {len(fetch['redirect_chain'])}")
        for i, hop in enumerate(fetch["redirect_chain"], 1):
            print(f"    hop {i}: {hop}")

    if fetch["error"]:
        print(f"  ERROR           : {fetch['error']}")
        return {
            "query": query,
            "requests_viable": False,
            "candidates": [],
            "blocking_detected": True,
            "error": fetch["error"],
        }

    soup = BeautifulSoup(fetch["html"], "lxml")
    page_title = soup.title.get_text(strip=True) if soup.title else "(no title)"
    print(f"  Page title      : {page_title!r}")

    selector_counts = count_selectors(soup)
    product_card_count = selector_counts.get(".item-cell", 0)

    blocking = detect_blocking(
        fetch["status_code"],
        page_title,
        fetch["html"],
        fetch["final_url"],
        fetch["url"],
        product_card_count=product_card_count,
    )
    print(f"  Blocking detected : {blocking['blocking_detected']}")
    if blocking["triggered_indicators"]:
        print(f"  Blocking indicators: {blocking['triggered_indicators']}")
    if blocking.get("cdn_only_indicators"):
        print(f"  CDN-only signals   : {blocking['cdn_only_indicators']} (non-blocking when cards present)")
    print(f"  Body word count   : {blocking['body_word_count']}")
    print(f"  JS shell          : {blocking['js_shell']}")

    print("  Candidate selector counts:")
    for sel, count in selector_counts.items():
        if count:
            print(f"    {sel:24s}: {count}")

    candidates = extract_candidates(soup, limit=limit)
    print(f"  Extracted products: {len(candidates)}")

    if candidates:
        print("  First candidates:")
        for i, c in enumerate(candidates, 1):
            print(f"\n    [{i}]")
            for k, v in c.items():
                print(f"      {k:14s}: {v!r}")
    else:
        print("  No product candidates extracted.")

    requests_viable = (
        fetch["status_code"] == 200
        and not blocking["blocking_detected"]
        and len(candidates) > 0
    )

    return {
        "query": query,
        "requests_viable": requests_viable,
        "candidates": candidates,
        "blocking_detected": blocking["blocking_detected"],
        "selector_counts": selector_counts,
        "page_title": page_title,
        "status_code": fetch["status_code"],
        "html_length": fetch["html_length"],
        "error": None,
    }


def print_final_assessment(results: list[dict]) -> None:
    print_section("FINAL ASSESSMENT — ALL QUERIES")

    total = len(results)
    viable = sum(1 for r in results if r.get("requests_viable"))
    with_candidates = sum(1 for r in results if r.get("candidates"))
    blocked = sum(1 for r in results if r.get("blocking_detected"))

    print(f"  Queries probed              : {total}")
    print(f"  requests viable (per query) : {viable}/{total}")
    print(f"  queries with candidates     : {with_candidates}/{total}")
    print(f"  queries with blocking       : {blocked}/{total}")
    print()

    requests_enough = viable == total and with_candidates == total
    needs_browser = with_candidates == 0 or any(r.get("error") for r in results)

    has_title_price_link = all(
        all(c.get("title") and c.get("link") and c.get("price") is not None for c in r.get("candidates", [])[:1])
        for r in results
        if r.get("candidates")
    )
    has_availability = any(
        c.get("stock") or c.get("shipping")
        for r in results
        for c in r.get("candidates", [])
    )
    stable_links = all(
        all(
            c.get("link") and "newegg.com" in (c.get("link") or "")
            for c in r.get("candidates", [])[:1]
        )
        for r in results
        if r.get("candidates")
    )

    print(f"  Is requests enough?                    : {'YES' if requests_enough else 'NO'}")
    print(f"  Is browser automation required?        : {'LIKELY YES' if needs_browser else 'NO (for now)'}")
    print(f"  Title/price/link reliably visible?     : {'YES' if has_title_price_link else 'PARTIAL/NO'}")
    print(f"  Availability/shipping visible?         : {'YES' if has_availability else 'PARTIAL/NO'}")
    print(f"  Stable product links for dedupe?       : {'YES' if stable_links else 'UNCERTAIN'}")
    print(f"  Viable for future runtime adapter?     : {'PROMISING' if requests_enough else 'PROBE FURTHER — validate on Raven'}")
    print(f"  Should remain probe-only?              : YES (no registry/default-source changes yet)")
    print()

    if needs_browser:
        print("  RECOMMENDED NEXT STEP:")
        print("    Run experiments/adapters/newegg_playwright_probe.py for JS-render fallback check.")
    elif requests_enough:
        print("  RECOMMENDED NEXT STEP:")
        print("    Re-run probe from Raven over multiple days.")
        print("    If stable, sketch experimental adapter behind a flag.")
        print("    Do NOT register in adapters/registry.py until validated.")
    else:
        print("  RECOMMENDED NEXT STEP:")
        print("    Inspect HTML for selector drift. Try Playwright probe.")
    print()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Newegg reconnaissance probe (experimental, isolated)"
    )
    parser.add_argument(
        "query",
        nargs="*",
        help="Search term(s). Default: built-in query list.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=MAX_CANDIDATES,
        help=f"Max candidates per query (default: {MAX_CANDIDATES})",
    )
    args = parser.parse_args()

    queries = args.query if args.query else DEFAULT_QUERIES

    print_section("Newegg Recon Probe — requests + BeautifulSoup")
    print(f"  Queries : {queries}")
    print(f"  Limit   : {args.limit}")

    results = [probe_query(q, limit=args.limit) for q in queries]
    print_final_assessment(results)


if __name__ == "__main__":
    main()
