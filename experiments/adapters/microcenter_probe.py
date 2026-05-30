"""
Micro Center candidate-source probe
====================================
Reconnaissance only. Does NOT write to SQLite, send Discord alerts,
touch .env, change DB schema, or register in adapters/registry.py.

Usage:
    python experiments/adapters/microcenter_probe.py
    python experiments/adapters/microcenter_probe.py "rtx 4070"
    python experiments/adapters/microcenter_probe.py "rtx 4070" --storeid 115
    python experiments/adapters/microcenter_probe.py --cffi

Default queries (no args): rtx 4070, ryzen 5600, gaming laptop, macbook

Goal: determine whether Micro Center is a viable future retail adapter and
whether plain requests (or curl_cffi TLS impersonation) can reach product data.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from typing import Any, Optional
from urllib.parse import urlencode, urljoin

import requests
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

MICROCENTER_ORIGIN = "https://www.microcenter.com"
SEARCH_PATH = "/search/search_results.aspx"

DEFAULT_QUERIES = [
    "rtx 4070",
    "ryzen 5600",
    "gaming laptop",
    "macbook",
]

# Known store IDs (streetmerchant / community recon); used to test location control.
SAMPLE_STORE_IDS: dict[str, str] = {
    "brooklyn": "115",
    "columbus": "141",
    "dallas": "131",
    "tustin": "101",
    "web_shippable": "029",
}

REQUEST_HEADERS = {
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
    "Accept-Encoding": "gzip, deflate, br",
    "DNT": "1",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
}

REQUEST_TIMEOUT = 25
MAX_CANDIDATES_DISPLAY = 5

CHALLENGE_SIGNALS = [
    "captcha",
    "are you a human",
    "please verify",
    "unusual traffic",
    "robot",
    "bot detection",
    "security check",
    "just a moment",
    "cf-browser-verification",
    "challenge-form",
    "cdn-cgi/challenge-platform",
    "access denied",
    "403 forbidden",
    "enable javascript and cookies",
    "checking your browser",
    "px-captcha",
    "datadome",
    "incapsula",
    "verify you are human",
]

SPA_MARKERS = [
    "__NEXT_DATA__",
    "__NUXT__",
    "data-reactroot",
    "window.__INITIAL_STATE__",
    "ng-version",
]

# Selectors from prior probe + live HTML recon (product grid on search_results.aspx)
LISTING_SELECTORS = [
    "li.product_wrapper",
    ".product_wrapper",
    "a.productClick",
    ".productClick",
    "#productGrid li",
    "#productGrid .product_wrapper",
    ".SearchResultProduct",
    ".search-result-product",
    "div[class*='SearchResult']",
    "tr.SearchResultProduct",
    "[data-product-id]",
    "article.product",
]

FIELD_SELECTORS = {
    "title": [
        "a.productClick",
        "a.SearchResultProductName",
        "h2 a",
        ".productClick span",
        "[class*='productName']",
        "[class*='ProductName']",
    ],
    "price": [
        ".price",
        ".price_wrapper",
        "span[itemprop='price']",
        "[class*='price']",
        "[data-price]",
    ],
    "link": [
        "a.productClick",
        "a[href*='/product/']",
        "h2 a[href]",
    ],
    "availability": [
        ".instock",
        ".inventory",
        "[class*='inventory']",
        "[class*='pickup']",
        "[class*='InStock']",
        ".storePickup",
    ],
}

SERVER_RENDERED_SIGNALS = [
    "#productGrid",
    ".product_wrapper",
    "a.productClick",
    "li.product_wrapper",
]


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass
class ProbeResult:
    query: str
    search_url: str
    final_url: str
    status_code: int
    page_title: Optional[str]
    response_bytes: int
    challenge: dict[str, Any]
    block_platforms: dict[str, list[str]]
    spa_markers: list[str]
    js_only_shell: bool
    server_rendered: dict[str, Any]
    listing_detection: dict[str, Any]
    candidate_count: int
    candidates: list[dict[str, Any]]
    location_control: dict[str, Any]
    fetch_error: Optional[str] = None
    transport: str = "requests"


# ---------------------------------------------------------------------------
# URL builders
# ---------------------------------------------------------------------------


def build_search_url(query: str, store_id: Optional[str] = None) -> str:
    params: dict[str, str] = {"Ntt": query, "Ntk": "all", "sortby": "match"}
    if store_id:
        params["storeid"] = store_id
    return f"{MICROCENTER_ORIGIN}{SEARCH_PATH}?{urlencode(params)}"


# ---------------------------------------------------------------------------
# HTTP fetch (requests or curl_cffi)
# ---------------------------------------------------------------------------


def fetch_page(
    url: str,
    *,
    use_cffi: bool = False,
) -> tuple[Optional[requests.Response], Optional[str], str]:
    """Return (response, error_message, transport_label)."""
    if use_cffi:
        try:
            from curl_cffi import requests as cffi_requests  # type: ignore[import]
        except ImportError:
            return None, "curl_cffi_not_installed (pip install curl_cffi)", "curl_cffi"

        try:
            resp = cffi_requests.get(
                url,
                headers=REQUEST_HEADERS,
                timeout=REQUEST_TIMEOUT,
                allow_redirects=True,
                impersonate="chrome124",
            )
            return resp, None, "curl_cffi"
        except Exception as exc:  # noqa: BLE001 — probe must not crash
            return None, f"{type(exc).__name__}: {exc}", "curl_cffi"

    session = requests.Session()
    try:
        resp = session.get(
            url,
            headers=REQUEST_HEADERS,
            timeout=REQUEST_TIMEOUT,
            allow_redirects=True,
        )
        return resp, None, "requests"
    except requests.exceptions.Timeout:
        return None, "timeout", "requests"
    except requests.exceptions.RequestException as exc:
        return None, f"{type(exc).__name__}: {exc}", "requests"


# ---------------------------------------------------------------------------
# Detection helpers
# ---------------------------------------------------------------------------


def _detect_block_platforms(
    html: str,
    headers: dict[str, str],
    cookie_names: list[str],
) -> dict[str, list[str]]:
    hits: dict[str, list[str]] = {}
    combined = html.lower()
    header_str = " ".join(f"{k}:{v}" for k, v in headers.items()).lower()
    cookies = set(cookie_names)

    header_checks = {
        "cloudflare": ["cf-ray", "cf-cache-status", "cf-mitigated", "server: cloudflare"],
        "akamai": ["x-akamai", "akamai-grn"],
        "datadome": ["x-datadome"],
        "perimeterx": ["x-px-"],
    }
    for platform, keys in header_checks.items():
        for key in keys:
            if key in header_str:
                hits.setdefault(platform, []).append(f"header:{key}")

    cookie_checks = {
        "cloudflare": ["cf_clearance", "__cf_bm", "__cflb"],
        "akamai": ["_abck", "bm_sz", "ak_bmsc"],
        "datadome": ["datadome"],
    }
    for platform, keys in cookie_checks.items():
        for key in keys:
            if any(c == key or c.startswith(key) for c in cookies):
                hits.setdefault(platform, []).append(f"cookie:{key}")

    html_checks = {
        "cloudflare": [
            "cdn-cgi/challenge-platform",
            "cf-browser-verification",
            "checking your browser",
            "enable javascript and cookies",
            "__cf_chl",
        ],
        "generic_block": [
            "access denied",
            "unusual traffic",
            "too many requests",
        ],
    }
    for platform, markers in html_checks.items():
        for marker in markers:
            if marker.lower() in combined:
                hits.setdefault(platform, []).append(f"html:{marker}")

    if "just a moment" in combined and "cloudflare" not in hits:
        hits.setdefault("cloudflare", []).append("html:just a moment (title/body)")

    return hits


def detect_challenge(html: str, soup: BeautifulSoup, status_code: int) -> dict[str, Any]:
    html_lower = html.lower()
    triggered = [sig for sig in CHALLENGE_SIGNALS if sig in html_lower]
    title = soup.title.get_text(strip=True) if soup.title else ""

    is_403 = status_code == 403
    is_captcha_page = any(
        w in html_lower for w in ("captcha", "g-recaptcha", "hcaptcha", "px-captcha")
    )
    is_access_denied = "access denied" in html_lower or "forbidden" in title.lower()
    is_cloudflare_wait = "just a moment" in title.lower() or "just a moment" in html_lower

    return {
        "challenge_detected": bool(triggered) or is_403 or is_cloudflare_wait,
        "http_403": is_403,
        "captcha_signals": is_captcha_page,
        "access_denied": is_access_denied,
        "cloudflare_interstitial": is_cloudflare_wait,
        "triggered_signals": triggered,
        "page_title": title or None,
    }


def detect_spa_markers(html: str) -> list[str]:
    return [m for m in SPA_MARKERS if m in html]


def detect_js_only_shell(soup: BeautifulSoup, html: str) -> bool:
    body = soup.body
    body_text = body.get_text(" ", strip=True) if body else ""
    word_count = len(body_text.split())
    script_count = len(soup.find_all("script"))
    has_product_grid = bool(soup.select_one("#productGrid, .product_wrapper, a.productClick"))

    if has_product_grid:
        return False

    if "just a moment" in html.lower():
        return True

    if word_count < 80 and script_count >= 3:
        return True

    return False


def detect_server_rendering(soup: BeautifulSoup) -> dict[str, Any]:
    counts = {sel: len(soup.select(sel)) for sel in SERVER_RENDERED_SIGNALS}
    return {
        "appears_server_rendered": any(v > 0 for v in counts.values()),
        "selector_hit_counts": counts,
    }


def detect_listing_cards(soup: BeautifulSoup) -> dict[str, Any]:
    selector_counts: dict[str, int] = {}
    best_selector: Optional[str] = None
    best_count = 0

    for selector in LISTING_SELECTORS:
        count = len(soup.select(selector))
        if count:
            selector_counts[selector] = count
            if count > best_count:
                best_count = count
                best_selector = selector

    return {
        "cards_accessible": best_count > 0,
        "selector_used": best_selector,
        "card_count": best_count,
        "selector_hit_counts": selector_counts,
    }


def _first_text(el) -> Optional[str]:
    if el is None:
        return None
    text = el.get_text(" ", strip=True)
    return text or None


def _first_attr(el, attr: str) -> Optional[str]:
    if el is None:
        return None
    val = el.get(attr)
    return str(val).strip() if val else None


def _normalize_link(href: Optional[str]) -> Optional[str]:
    if not href:
        return None
    href = href.strip()
    if href.startswith("/"):
        return urljoin(MICROCENTER_ORIGIN, href)
    if href.startswith("http"):
        return href
    return urljoin(MICROCENTER_ORIGIN + "/", href)


def parse_price(raw: Optional[str]) -> Optional[str]:
    if not raw:
        return None
    m = re.search(r"\$[\d,]+(?:\.\d{2})?", raw)
    return m.group(0) if m else raw.strip()[:40] or None


def extract_candidates(soup: BeautifulSoup, listing_info: dict[str, Any]) -> list[dict[str, Any]]:
    selector = listing_info.get("selector_used")
    if not selector:
        return []

    cards = soup.select(selector)
    results: list[dict[str, Any]] = []

    for card in cards[:MAX_CANDIDATES_DISPLAY]:
        title = link = price = availability = None

        for sel in FIELD_SELECTORS["title"]:
            el = card.select_one(sel)
            if el:
                title = _first_text(el)
                if el.name == "a":
                    link = _normalize_link(_first_attr(el, "href"))
                break

        for sel in FIELD_SELECTORS["link"]:
            if link:
                break
            el = card.select_one(sel)
            if el:
                link = _normalize_link(_first_attr(el, "href"))
                if not title:
                    title = _first_text(el)

        for sel in FIELD_SELECTORS["price"]:
            el = card.select_one(sel)
            if el:
                price = parse_price(_first_text(el))
                break

        for sel in FIELD_SELECTORS["availability"]:
            el = card.select_one(sel)
            if el:
                availability = _first_text(el)
                break

        if not link:
            el = card.select_one("a[href*='/product/']")
            if el:
                link = _normalize_link(_first_attr(el, "href"))

        if title or link:
            results.append(
                {
                    "title": title,
                    "price": price,
                    "link": link,
                    "availability": availability,
                }
            )

    return results


def assess_location_control(
    *,
    requested_store_id: Optional[str],
    final_url: str,
    html: str,
    cookie_names: list[str],
) -> dict[str, Any]:
    """Whether store/location appears controllable via URL or cookies."""
    store_cookies = [c for c in cookie_names if "store" in c.lower()]
    url_has_storeid = "storeid=" in final_url.lower()
    html_mentions_store = any(
        phrase in html.lower()
        for phrase in (
            "my store",
            "current store",
            "store pickup",
            "in-store pickup",
            "select a store",
        )
    )

    known_ids_in_html = []
    for name, sid in SAMPLE_STORE_IDS.items():
        if sid in html:
            known_ids_in_html.append(f"{name}:{sid}")

    return {
        "storeid_query_param_supported": True,  # documented API/scraper convention
        "requested_store_id": requested_store_id,
        "final_url_contains_storeid": url_has_storeid,
        "store_related_cookies": store_cookies,
        "html_store_ux_signals": html_mentions_store,
        "appears_controllable": bool(requested_store_id) or url_has_storeid or bool(store_cookies),
        "notes": (
            "Micro Center uses ?storeid=<id> on product/search URLs for per-store "
            "pricing and inventory (e.g. 115=Brooklyn, 141=Columbus). Controllable "
            "in principle; requires reaching real HTML past bot protection."
        ),
        "sample_store_ids": SAMPLE_STORE_IDS,
    }


# ---------------------------------------------------------------------------
# Single-query probe
# ---------------------------------------------------------------------------


def probe_query(
    query: str,
    *,
    store_id: Optional[str] = None,
    use_cffi: bool = False,
) -> ProbeResult:
    search_url = build_search_url(query, store_id)
    resp, err, transport = fetch_page(search_url, use_cffi=use_cffi)

    if err or resp is None:
        return ProbeResult(
            query=query,
            search_url=search_url,
            final_url=search_url,
            status_code=0,
            page_title=None,
            response_bytes=0,
            challenge={"challenge_detected": True, "fetch_failed": True},
            block_platforms={},
            spa_markers=[],
            js_only_shell=True,
            server_rendered={"appears_server_rendered": False},
            listing_detection={"cards_accessible": False, "card_count": 0},
            candidate_count=0,
            candidates=[],
            location_control=assess_location_control(
                requested_store_id=store_id,
                final_url=search_url,
                html="",
                cookie_names=[],
            ),
            fetch_error=err,
            transport=transport,
        )

    html = resp.text
    soup = BeautifulSoup(html, "lxml")
    cookie_names = list(getattr(resp, "cookies", {}) or [])
    if hasattr(resp, "cookies") and hasattr(resp.cookies, "keys"):
        try:
            cookie_names = list(resp.cookies.keys())  # type: ignore[union-attr]
        except Exception:  # noqa: BLE001
            cookie_names = []

    challenge = detect_challenge(html, soup, resp.status_code)
    block_platforms = _detect_block_platforms(html, dict(resp.headers), cookie_names)
    spa = detect_spa_markers(html)
    js_shell = detect_js_only_shell(soup, html)
    server = detect_server_rendering(soup)
    listings = detect_listing_cards(soup)
    candidates = extract_candidates(soup, listings) if listings["cards_accessible"] else []
    location = assess_location_control(
        requested_store_id=store_id,
        final_url=str(resp.url),
        html=html,
        cookie_names=cookie_names,
    )

    title = soup.title.get_text(strip=True) if soup.title else None

    return ProbeResult(
        query=query,
        search_url=search_url,
        final_url=str(resp.url),
        status_code=resp.status_code,
        page_title=title,
        response_bytes=len(html.encode("utf-8", errors="replace")),
        challenge=challenge,
        block_platforms=block_platforms,
        spa_markers=spa,
        js_only_shell=js_shell,
        server_rendered=server,
        listing_detection=listings,
        candidate_count=listings.get("card_count", 0),
        candidates=candidates,
        location_control=location,
        transport=transport,
    )


def print_probe_result(result: ProbeResult) -> None:
    sep = "-" * 70
    print(sep)
    print(f"QUERY          : {result.query!r}")
    print(f"TRANSPORT      : {result.transport}")
    print(f"SEARCH URL     : {result.search_url}")
    print(f"FINAL URL      : {result.final_url}")
    print(f"STATUS CODE    : {result.status_code}")
    print(f"PAGE TITLE     : {result.page_title or '(none)'}")
    print(f"RESPONSE BYTES : {result.response_bytes:,}")

    if result.fetch_error:
        print(f"FETCH ERROR    : {result.fetch_error}")
        return

    print("\n--- Blocking / CAPTCHA / JS-only ---")
    ch = result.challenge
    print(f"  challenge_detected     : {ch.get('challenge_detected')}")
    print(f"  http_403               : {ch.get('http_403')}")
    print(f"  cloudflare_interstitial: {ch.get('cloudflare_interstitial')}")
    print(f"  captcha_signals        : {ch.get('captcha_signals')}")
    print(f"  access_denied          : {ch.get('access_denied')}")
    print(f"  js_only_shell          : {result.js_only_shell}")
    if ch.get("triggered_signals"):
        print(f"  triggered_signals      : {ch['triggered_signals'][:12]}")
    if result.block_platforms:
        print(f"  anti_bot_platforms     : {json.dumps(result.block_platforms)}")
    if result.spa_markers:
        print(f"  spa_markers            : {result.spa_markers}")

    print("\n--- Store / location control ---")
    loc = result.location_control
    print(f"  storeid_param_on_url   : {loc.get('final_url_contains_storeid')}")
    print(f"  requested_store_id     : {loc.get('requested_store_id')}")
    print(f"  store_cookies          : {loc.get('store_related_cookies')}")
    print(f"  html_store_signals     : {loc.get('html_store_ux_signals')}")
    print(f"  appears_controllable   : {loc.get('appears_controllable')}")
    print(f"  notes                  : {loc.get('notes')}")

    print("\n--- Product data ---")
    print(f"  server_rendered        : {result.server_rendered.get('appears_server_rendered')}")
    print(f"  candidate_card_count   : {result.candidate_count}")
    sel_counts = result.listing_detection.get("selector_hit_counts") or {}
    if sel_counts:
        top = sorted(sel_counts.items(), key=lambda x: -x[1])[:6]
        print(f"  selector_hits          : {top}")
    else:
        print("  selector_hits          : (none)")

    if result.candidates:
        print(f"\n--- Sample candidates (up to {MAX_CANDIDATES_DISPLAY}) ---")
        for i, cand in enumerate(result.candidates, 1):
            print(f"  [{i}] {json.dumps(cand, ensure_ascii=False)}")
    else:
        print("\n  No product candidates extracted (blocked, JS shell, or selectors miss).")


def run_assessment(results: list[ProbeResult], *, used_cffi: bool) -> None:
    print("\n" + "=" * 70)
    print("MICRO CENTER PROBE — ASSESSMENT")
    print("=" * 70)

    any_200_with_products = any(
        r.status_code == 200 and r.candidate_count > 0 and not r.js_only_shell
        for r in results
    )
    all_blocked = all(
        r.challenge.get("challenge_detected")
        or r.status_code in (403, 401, 503)
        or r.js_only_shell
        or r.candidate_count == 0
        for r in results
    )
    any_cloudflare = any(
        r.challenge.get("cloudflare_interstitial")
        or "cloudflare" in r.block_platforms
        for r in results
    )

    requests_viable = any_200_with_products and not all_blocked
    browser_likely = all_blocked or any_cloudflare
    location_controllable = any(r.location_control.get("appears_controllable") for r in results)

    print(f"\n  Transport used           : {'curl_cffi + requests' if used_cffi else 'requests'}")
    print(f"  Queries probed           : {len(results)}")
    print(f"  Any HTTP 200 + products  : {any_200_with_products}")
    print(f"  All queries blocked/empty: {all_blocked}")
    print(f"  Cloudflare observed      : {any_cloudflare}")

    print("\n  --- Verdict ---")
    print(f"  requests sufficient?     : {'YES' if requests_viable else 'NO'}")
    print(
        "  browser automation req?  : "
        + ("LIKELY YES (Cloudflare / JS challenge)" if browser_likely else "UNCLEAR — retest from residential IP")
    )
    loc_note = (
        "YES — ?storeid= accepted on URL (smoke test passed)"
        if location_controllable
        else "LIKELY — ?storeid= documented; verify on real HTML"
    )
    print(f"  location/store control?  : {loc_note}")
    print("  remain probe-only?       : YES (no runtime adapter until fetch path proven)")

    print("\n  --- Recommended next step ---")
    if requests_viable:
        print(
            "  Implement experimental adapter using confirmed selectors; "
            "validate storeid scoping on a stable host."
        )
    elif browser_likely:
        print(
            "  1. Run experiments/adapters/microcenter_playwright_probe.py "
            "from a residential or Raven host with `playwright install chromium`.\n"
            "  2. Confirm product grid selectors (#productGrid, .product_wrapper) on real HTML.\n"
            "  3. Test ?storeid=115 vs 141 for price/inventory deltas.\n"
            "  4. If Playwright + cf_clearance cookie works, evaluate periodic session refresh "
            "vs official/API alternatives before production adapter work."
        )
    else:
        print("  Re-run from Raven/residential IP; compare requests vs curl_cffi vs Playwright.")

    print("=" * 70)


def probe_location_smoke(*, use_cffi: bool) -> None:
    """Quick check: does storeid appear in URL/cookies when appended?"""
    print("\n" + "=" * 70)
    print("LOCATION SMOKE — storeid=115 (Brooklyn) on first default query")
    print("=" * 70)
    result = probe_query(DEFAULT_QUERIES[0], store_id="115", use_cffi=use_cffi)
    print_probe_result(result)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description="Micro Center recon probe (no runtime side effects)")
    parser.add_argument("queries", nargs="*", help="Search terms (default: 4 preset queries)")
    parser.add_argument("--storeid", help="Append storeid= to search URL (e.g. 115 Brooklyn)")
    parser.add_argument(
        "--cffi",
        action="store_true",
        help="Also run each query with curl_cffi Chrome TLS impersonation",
    )
    parser.add_argument(
        "--no-location-smoke",
        action="store_true",
        help="Skip storeid=115 smoke test after default batch",
    )
    args = parser.parse_args()

    queries = args.queries if args.queries else DEFAULT_QUERIES

    print("=" * 70)
    print("MICRO CENTER PROBE — Vulture 2.0 recon (probe-only)")
    print("=" * 70)
    print("Constraints: no SQLite, no Discord, no registry, no .env changes")
    print(f"Queries: {queries}")

    all_results: list[ProbeResult] = []

    for q in queries:
        r = probe_query(q, store_id=args.storeid, use_cffi=False)
        print_probe_result(r)
        all_results.append(r)

        if args.cffi:
            print("\n  [Phase 1b: curl_cffi chrome124]")
            r_cffi = probe_query(q, store_id=args.storeid, use_cffi=True)
            print_probe_result(r_cffi)
            all_results.append(r_cffi)

    if not args.no_location_smoke and not args.storeid:
        probe_location_smoke(use_cffi=args.cffi)

    run_assessment(all_results, used_cffi=args.cffi)


if __name__ == "__main__":
    main()
