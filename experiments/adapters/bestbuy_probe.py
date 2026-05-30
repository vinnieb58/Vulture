"""
Best Buy retail adapter feasibility probe
=========================================
Reconnaissance only. Does NOT write to SQLite, send Discord alerts,
register in adapters/registry.py, or touch .env.

Usage:
    python experiments/adapters/bestbuy_probe.py
    python experiments/adapters/bestbuy_probe.py "rtx 4070"
    python experiments/adapters/bestbuy_probe.py "macbook air" "gaming laptop"
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from dataclasses import asdict, dataclass
from typing import Any, Optional
from urllib.parse import quote_plus, urljoin

import requests
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

BESTBUY_SEARCH_BASE = "https://www.bestbuy.com/site/searchpage.jsp"
DEFAULT_QUERIES = ["rtx 4070", "macbook air", "gaming laptop"]

REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
}

REQUEST_TIMEOUT = 25
MAX_CANDIDATES_DISPLAY = 5
BARE_BLOCK_THRESHOLD_BYTES = 2_000

# Strong signals only — avoid matching "robot" in product copy (e.g. robot vacuums).
BLOCKING_SIGNALS = [
    "are you a human",
    "unusual traffic",
    "bot detection",
    "security check",
    "just a moment",
    "cf-browser-verification",
    "challenge-form",
    "access denied",
    "403 forbidden",
    "verify you are human",
    "distil-",
    "px-captcha",
    "px-spinner",
    "_pxparam",
    "datadome",
    "incapsula",
    "akamai",
    "blocked",
    "enable javascript",
    "javascript is required",
    "please enable cookies",
]

SPA_MARKERS = [
    "__NEXT_DATA__",
    "__NUXT__",
    "data-reactroot",
    "window.__INITIAL_STATE__",
    "window.__PRELOADED_STATE__",
    "window.__APOLLO_STATE__",
    "id=\"app\"",
]

PRODUCT_CARD_SELECTORS = [
    "li.sku-item",
    ".sku-item",
    "[data-testid='product-card']",
    ".product-item",
    ".shop-sku-list-item",
    "div[class*='sku-item']",
    "li[class*='sku-item']",
    ".list-item",
    "section.sku-item-list li",
]

PRODUCT_FIELD_SELECTORS = {
    "title": [
        ".sku-title a",
        ".sku-title",
        "h4.sku-title",
        "[data-testid='product-title']",
        "a[data-track='Product Title']",
    ],
    "price": [
        ".priceView-customer-price span",
        ".priceView-hero-price span",
        "[data-testid='customer-price']",
        ".pricing-price__regular-price",
        "div[data-testid='price']",
        ".priceView-price",
    ],
    "link": [
        ".sku-title a",
        "h4.sku-title a",
        "a.image-link",
        "a[href*='/site/']",
    ],
    "availability": [
        ".fulfillment-add-to-cart-button",
        ".availability-text",
        "[data-testid='availability-message']",
        ".fulfillment-fulfillment-summary",
        ".c-button-add",
    ],
    "pickup": [
        "[class*='pickup']",
        "[data-testid*='pickup']",
        ".fulfillment-pickup",
    ],
}


@dataclass
class ProductCandidate:
    title: Optional[str]
    price: Optional[str]
    link: Optional[str]
    availability: Optional[str]
    store_pickup: Optional[str]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def build_search_url(query: str) -> str:
    return f"{BESTBUY_SEARCH_BASE}?st={quote_plus(query)}"


def parse_price_int(raw: Optional[str]) -> Optional[int]:
    if not raw:
        return None
    match = re.search(r"\$?([\d,]+)(?:\.\d+)?", raw.replace(",", ""))
    if match:
        try:
            return int(match.group(1).replace(",", ""))
        except ValueError:
            return None
    return None


def normalize_link(href: Optional[str]) -> Optional[str]:
    if not href:
        return None
    href = href.strip()
    if href.startswith("//"):
        return "https:" + href
    if href.startswith("/"):
        return urljoin("https://www.bestbuy.com", href)
    if href.startswith("http"):
        return href.split("?")[0] if "/site/" in href else href
    return href


def _first_text(el) -> Optional[str]:
    if el is None:
        return None
    text = el.get_text(" ", strip=True)
    return text or None


def _select_one_text(card, selectors: list[str]) -> Optional[str]:
    for sel in selectors:
        found = card.select_one(sel)
        if found:
            text = _first_text(found)
            if text:
                return text
    return None


def _select_one_href(card, selectors: list[str]) -> Optional[str]:
    for sel in selectors:
        found = card.select_one(sel)
        if found and found.get("href"):
            return normalize_link(found["href"])
    return None


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------


def detect_blocking(html: str, soup: BeautifulSoup, status_code: Optional[int]) -> dict:
    html_lower = html.lower()
    triggered = [sig for sig in BLOCKING_SIGNALS if sig in html_lower]
    title_text = soup.title.get_text(strip=True) if soup.title else ""

    captcha = any(
        s in html_lower
        for s in (
            "g-recaptcha",
            "hcaptcha",
            "verify you are human",
            "please verify you",
            "security challenge",
            "access denied",
        )
    ) or (
        "captcha" in html_lower
        and any(
            s in html_lower
            for s in ("please verify", "unusual traffic", "are you a human")
        )
    )
    access_denied = status_code == 403 or "access denied" in html_lower
    blocked = status_code in (403, 401, 429) or access_denied

    return {
        "blocked_http": blocked,
        "status_blocked": status_code in (403, 401, 429),
        "captcha_detected": captcha,
        "access_denied_detected": access_denied,
        "challenge_detected": bool(triggered),
        "triggered_signals": triggered,
        "page_title": title_text,
    }


def detect_js_shell(html: str, soup: BeautifulSoup) -> dict:
    found_spa = [m for m in SPA_MARKERS if m.lower() in html.lower()]
    body = soup.body
    body_text = body.get_text(" ", strip=True) if body else ""
    body_word_count = len(body_text.split())
    script_count = max(len(soup.find_all("script")), html.lower().count("<script"))
    json_ld = soup.find_all("script", type="application/ld+json")
    inline_json = soup.find_all("script", type="application/json")

    thin_body = body_word_count < 150
    js_only_likely = thin_body and script_count > 5

    return {
        "spa_markers": found_spa,
        "body_word_count": body_word_count,
        "script_count": script_count,
        "json_ld_scripts": len(json_ld),
        "inline_json_scripts": len(inline_json),
        "thin_body": thin_body,
        "js_only_shell_likely": js_only_likely,
    }


def detect_product_cards(soup: BeautifulSoup) -> dict:
    for selector in PRODUCT_CARD_SELECTORS:
        cards = soup.select(selector)
        if cards:
            return {
                "cards_found": True,
                "selector_used": selector,
                "card_count": len(cards),
            }
    return {
        "cards_found": False,
        "selector_used": None,
        "card_count": 0,
    }


def diagnose_bare_block(html: str, status_code: Optional[int]) -> dict:
    size = len(html)
    bare = size < BARE_BLOCK_THRESHOLD_BYTES and status_code in (403, 401, 429, None)
    return {
        "html_size_bytes": size,
        "bare_block": bare,
        "likely_tls_or_ip_reject": bare and status_code == 403,
    }


# ---------------------------------------------------------------------------
# Fetch
# ---------------------------------------------------------------------------


def fetch_search(query: str) -> dict:
    url = build_search_url(query)
    t0 = time.monotonic()
    error = None
    status_code = None
    final_url = url
    html = ""
    redirect_chain: list[str] = []

    try:
        session = requests.Session()
        response = session.get(
            url,
            headers=REQUEST_HEADERS,
            timeout=REQUEST_TIMEOUT,
            allow_redirects=True,
        )
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        status_code = response.status_code
        final_url = response.url
        redirect_chain = [r.url for r in response.history]
        html = response.text
    except requests.exceptions.Timeout:
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        error = "request_timeout"
    except requests.exceptions.ConnectionError as exc:
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        error = f"connection_error: {exc}"
    except requests.exceptions.RequestException as exc:
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        error = f"request_error: {exc}"
    else:
        elapsed_ms = int((time.monotonic() - t0) * 1000)

    return {
        "query": query,
        "url": url,
        "final_url": final_url,
        "status_code": status_code,
        "redirect_chain": redirect_chain,
        "html": html,
        "html_length": len(html),
        "error": error,
        "elapsed_ms": elapsed_ms,
    }


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------


def _walk_json_for_products(obj: Any, depth: int = 0, max_depth: int = 10) -> list[dict]:
    if depth > max_depth:
        return []
    results: list[dict] = []
    if isinstance(obj, dict):
        keys_lower = {k.lower() for k in obj}
        has_name = "name" in keys_lower or "title" in keys_lower
        has_price = any(
            k in keys_lower
            for k in ("price", "currentprice", "customerprice", "regularprice")
        )
        has_sku = "sku" in keys_lower or "productid" in keys_lower or "skuId" in obj
        if has_name and (has_price or has_sku or "url" in keys_lower):
            candidate: dict[str, Any] = {}
            for k, v in obj.items():
                lk = k.lower()
                if lk in ("name", "title"):
                    candidate["title"] = v
                elif lk in ("price", "currentprice", "customerprice", "regularprice"):
                    candidate["price"] = v
                elif lk in ("url", "pdpurl", "producturl", "href"):
                    candidate["link"] = v
                elif lk in ("availability", "buttonstate", "fulfillment"):
                    candidate["availability"] = str(v)
            if candidate.get("title"):
                results.append(candidate)
        for v in obj.values():
            results.extend(_walk_json_for_products(v, depth + 1, max_depth))
    elif isinstance(obj, list):
        for item in obj:
            results.extend(_walk_json_for_products(item, depth + 1, max_depth))
    return results


def extract_from_embedded_json(soup: BeautifulSoup) -> list[dict]:
    candidates: list[dict] = []
    for script in soup.find_all("script", type="application/ld+json"):
        if not script.string:
            continue
        try:
            data = json.loads(script.string)
        except json.JSONDecodeError:
            continue
        items = data if isinstance(data, list) else [data]
        for item in items:
            if not isinstance(item, dict):
                continue
            if item.get("@type") not in ("Product", "ItemList", None):
                if item.get("@type") == "ItemList":
                    for sub in item.get("itemListElement", []):
                        if isinstance(sub, dict):
                            prod = sub.get("item") or sub
                            if isinstance(prod, dict) and prod.get("name"):
                                offer = prod.get("offers") or {}
                                if isinstance(offer, list):
                                    offer = offer[0] if offer else {}
                                candidates.append({
                                    "title": prod.get("name"),
                                    "price": offer.get("price") if isinstance(offer, dict) else None,
                                    "link": prod.get("url"),
                                    "availability": offer.get("availability") if isinstance(offer, dict) else None,
                                })
                continue
            if item.get("name"):
                offer = item.get("offers") or {}
                if isinstance(offer, list):
                    offer = offer[0] if offer else {}
                candidates.append({
                    "title": item.get("name"),
                    "price": offer.get("price") if isinstance(offer, dict) else None,
                    "link": item.get("url"),
                    "availability": offer.get("availability") if isinstance(offer, dict) else None,
                })

    next_tag = soup.find("script", id="__NEXT_DATA__")
    if next_tag and next_tag.string:
        try:
            next_data = json.loads(next_tag.string)
            candidates.extend(_walk_json_for_products(next_data))
        except json.JSONDecodeError:
            pass

    # Deduplicate by title
    seen: set[str] = set()
    unique: list[dict] = []
    for c in candidates:
        title = str(c.get("title") or "")
        if title and title not in seen:
            seen.add(title)
            unique.append(c)
    return unique


def extract_from_dom(soup: BeautifulSoup, limit: int = MAX_CANDIDATES_DISPLAY) -> list[ProductCandidate]:
    cards_info = detect_product_cards(soup)
    if not cards_info["cards_found"]:
        return []

    selector = cards_info["selector_used"]
    cards = soup.select(selector)[: limit * 2]
    results: list[ProductCandidate] = []

    for card in cards:
        title = _select_one_text(card, PRODUCT_FIELD_SELECTORS["title"])
        price = _select_one_text(card, PRODUCT_FIELD_SELECTORS["price"])
        link = _select_one_href(card, PRODUCT_FIELD_SELECTORS["link"])
        availability = _select_one_text(card, PRODUCT_FIELD_SELECTORS["availability"])
        pickup = _select_one_text(card, PRODUCT_FIELD_SELECTORS["pickup"])

        if not title and not link:
            continue

        results.append(
            ProductCandidate(
                title=title,
                price=price,
                link=link,
                availability=availability,
                store_pickup=pickup,
            )
        )
        if len(results) >= limit:
            break

    return results


def rough_dict_from_candidate(c: ProductCandidate | dict) -> dict:
    if isinstance(c, ProductCandidate):
        d = asdict(c)
    else:
        d = dict(c)
    price_raw = d.get("price")
    if price_raw is not None and not isinstance(price_raw, str):
        price_raw = str(price_raw)
    return {
        "title": d.get("title"),
        "price": price_raw,
        "price_parsed": parse_price_int(price_raw if isinstance(price_raw, str) else str(price_raw or "")),
        "link": normalize_link(d.get("link")) if d.get("link") else None,
        "availability": d.get("availability"),
        "store_pickup": d.get("store_pickup"),
    }


# ---------------------------------------------------------------------------
# Probe run
# ---------------------------------------------------------------------------


def print_section(title: str) -> None:
    print()
    print("=" * 70)
    print(f"  {title}")
    print("=" * 70)


def probe_query(query: str) -> dict:
    """Run probe for one query; return summary dict for final report."""
    print_section(f"Best Buy Probe — query={query!r}")

    fetch = fetch_search(query)

    print(f"  Query          : {fetch['query']!r}")
    print(f"  Request URL    : {fetch['url']}")
    print(f"  Final URL      : {fetch['final_url']}")
    print(f"  Status code    : {fetch['status_code']}")
    print(f"  HTML length    : {fetch['html_length']:,} bytes")
    print(f"  Elapsed        : {fetch['elapsed_ms']} ms")
    if fetch["redirect_chain"]:
        print(f"  Redirect hops  : {len(fetch['redirect_chain'])}")
        for i, hop in enumerate(fetch["redirect_chain"], 1):
            print(f"    {i}. {hop}")

    if fetch["error"]:
        print(f"  Fetch error    : {fetch['error']}")
        return {
            "query": query,
            "requests_viable": False,
            "browser_likely_required": True,
            "error": fetch["error"],
        }

    soup = BeautifulSoup(fetch["html"], "lxml")
    blocking = detect_blocking(fetch["html"], soup, fetch["status_code"])
    js_shell = detect_js_shell(fetch["html"], soup)
    cards = detect_product_cards(soup)
    bare = diagnose_bare_block(fetch["html"], fetch["status_code"])

    print_section("Page title & blocking indicators")
    print(f"  Page title           : {blocking['page_title']!r}")
    print(f"  HTTP blocked         : {blocking['blocked_http']}")
    print(f"  CAPTCHA detected     : {blocking['captcha_detected']}")
    print(f"  Access denied        : {blocking['access_denied_detected']}")
    print(f"  Challenge signals    : {blocking['triggered_signals'] or 'none'}")
    print(f"  Bare block (<{BARE_BLOCK_THRESHOLD_BYTES}B): {bare['bare_block']}")

    print_section("JS-only / SPA indicators")
    print(f"  SPA markers          : {js_shell['spa_markers'] or 'none'}")
    print(f"  Body word count      : {js_shell['body_word_count']}")
    print(f"  Script tags          : {js_shell['script_count']}")
    print(f"  JSON-LD scripts      : {js_shell['json_ld_scripts']}")
    print(f"  JS-only shell likely : {js_shell['js_only_shell_likely']}")

    print_section("Product card detection")
    print(f"  Cards found          : {cards['cards_found']}")
    print(f"  Selector used        : {cards['selector_used']}")
    print(f"  Candidate card count : {cards['card_count']}")

    dom_candidates = extract_from_dom(soup, limit=MAX_CANDIDATES_DISPLAY)
    json_candidates = extract_from_embedded_json(soup)

    # Prefer DOM; fall back to JSON
    if dom_candidates:
        display_source = "DOM"
        raw_for_display = [rough_dict_from_candidate(c) for c in dom_candidates]
        candidate_count = cards["card_count"]
    elif json_candidates:
        display_source = "embedded JSON"
        raw_for_display = [rough_dict_from_candidate(c) for c in json_candidates[:MAX_CANDIDATES_DISPLAY]]
        candidate_count = len(json_candidates)
    else:
        display_source = "none"
        raw_for_display = []
        candidate_count = 0

    print_section(f"Rough candidates ({display_source}) — count={candidate_count}")
    if not raw_for_display:
        print("  No product candidates extracted (empty server-rendered content).")
    else:
        for i, cand in enumerate(raw_for_display, 1):
            print(f"\n  [{i}]")
            for k, v in cand.items():
                print(f"    {k}: {v}")

    empty_server_rendered = (
        fetch["status_code"] == 200
        and not blocking["challenge_detected"]
        and candidate_count == 0
        and js_shell["js_only_shell_likely"]
    )

    requests_viable = (
        fetch["status_code"] == 200
        and not blocking["blocked_http"]
        and not blocking["captcha_detected"]
        and candidate_count > 0
        and bool(raw_for_display)
    )

    browser_likely = (
        blocking["blocked_http"]
        or blocking["captcha_detected"]
        or blocking["challenge_detected"]
        or empty_server_rendered
        or (fetch["status_code"] == 200 and candidate_count == 0)
    )

    print_section("Query verdict")
    if blocking["status_blocked"]:
        print("  BLOCKED at HTTP layer (403/401/429).")
    elif blocking["captcha_detected"]:
        print("  CAPTCHA or human-verification page detected.")
    elif empty_server_rendered:
        print("  EMPTY — 200 OK but JS-only shell; no server-rendered products.")
    elif requests_viable:
        print(f"  OK — requests returned {candidate_count} product card(s) with extractable fields.")
    else:
        print("  UNCERTAIN — no reliable product extraction via requests.")

    print(f"  requests_viable          : {requests_viable}")
    print(f"  browser_likely_required  : {browser_likely}")

    return {
        "query": query,
        "status_code": fetch["status_code"],
        "final_url": fetch["final_url"],
        "page_title": blocking["page_title"],
        "candidate_count": candidate_count,
        "requests_viable": requests_viable,
        "browser_likely_required": browser_likely,
        "blocked": blocking["blocked_http"],
        "captcha": blocking["captcha_detected"],
        "empty_server_rendered": empty_server_rendered,
    }


def run_probe(queries: list[str]) -> None:
    summaries: list[dict] = []
    for q in queries:
        summaries.append(probe_query(q))

    print_section("Overall summary")
    any_viable = any(s.get("requests_viable") for s in summaries)
    any_browser = any(s.get("browser_likely_required") for s in summaries)
    all_blocked = all(s.get("blocked") or s.get("captcha") for s in summaries)

    print(f"  Queries probed           : {len(summaries)}")
    print(f"  requests viable (any)  : {any_viable}")
    print(f"  browser required (any) : {any_browser}")
    print(f"  all queries blocked    : {all_blocked}")
    print()
    print("  Per-query:")
    for s in summaries:
        print(
            f"    {s['query']!r}: status={s.get('status_code')} "
            f"candidates={s.get('candidate_count')} "
            f"viable={s.get('requests_viable')}"
        )

    print()
    if any_viable:
        print("  RECOMMENDATION: requests may be sufficient for a follow-up parser pass.")
        print("  Stay probe-only until validated on Raven/residential IP.")
    elif all_blocked:
        print("  RECOMMENDATION: remain probe-only; try curl_cffi or Playwright on Raven.")
    else:
        print("  RECOMMENDATION: remain probe-only; browser automation likely required.")
    print()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Best Buy retail adapter feasibility probe (experimental, isolated)"
    )
    parser.add_argument(
        "queries",
        nargs="*",
        help="Search term(s). Defaults to rtx 4070, macbook air, gaming laptop.",
    )
    args = parser.parse_args()
    queries = args.queries if args.queries else DEFAULT_QUERIES
    run_probe(queries)


if __name__ == "__main__":
    main()
