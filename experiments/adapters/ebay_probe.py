"""
eBay Recon Probe — Vulture 2.0 Experimental Reconnaissance
===========================================================

Phase 1: requests-only fetch analysis
Phase 2: browser escalation assessment (recommendation only, no implementation)

Pipeline contract being evaluated:
    hunt -> adapter -> normalized Listing -> deterministic rules -> dedupe -> alert

This script is EXPERIMENTAL and ISOLATED.
It does NOT:
  - write to SQLite
  - send Discord alerts
  - modify hunt execution
  - modify Discord behavior
  - connect to any Vulture runtime

Usage:
    python experiments/adapters/ebay_probe.py
    python experiments/adapters/ebay_probe.py "rtx 3080"
    python experiments/adapters/ebay_probe.py "rtx 3080" --limit 5
"""

import re
import sys
import time
import argparse
from dataclasses import dataclass, asdict
from typing import Optional
from urllib.parse import urlencode, urljoin

import requests
from bs4 import BeautifulSoup


# ---------------------------------------------------------------------------
# Probe-local candidate type (mirrors models.listing.Listing without import)
# ---------------------------------------------------------------------------

@dataclass
class CandidateListing:
    source: str
    title: str
    price: Optional[int]
    location: Optional[str]
    link: str


# ---------------------------------------------------------------------------
# Request configuration
# ---------------------------------------------------------------------------

EBAY_SEARCH_BASE = "https://www.ebay.com/sch/i.html"

REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xhtml+xml;q=0.9,"
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

REQUEST_TIMEOUT = 20


# ---------------------------------------------------------------------------
# Challenge / anti-bot detection heuristics
# ---------------------------------------------------------------------------

CHALLENGE_SIGNALS = [
    "captcha",
    "are you a human",
    "please verify",
    "unusual traffic",
    "robot",
    "bot detection",
    "security check",
    "just a moment",                 # Cloudflare
    "cf-browser-verification",       # Cloudflare
    "challenge-form",                # Cloudflare
    "access denied",
    "403 forbidden",
    "sign in to continue",
    "verify you are human",
    "distil-",                       # Distil Networks
    "px-captcha",                    # PerimeterX
    "px-spinner",
    "_pxParam",
    "datadome",
    "incapsula",
]

LISTING_CARD_SELECTORS = [
    ".s-item",
    "li.s-item",
    ".srp-results .s-item",
    "[data-view='mi:1686|iid:1']",
]

SERVER_RENDERED_SIGNALS = [
    ".s-item__title",
    ".s-item__price",
    ".s-item__link",
]


# ---------------------------------------------------------------------------
# Price parsing
# ---------------------------------------------------------------------------

def parse_price(raw: Optional[str]) -> Optional[int]:
    if not raw:
        return None
    raw = raw.strip()
    # Handle price ranges like "$150.00 to $300.00" — take the lower end
    range_match = re.match(r"\$?([\d,]+\.?\d*)\s+to\s+\$?([\d,]+\.?\d*)", raw, re.IGNORECASE)
    if range_match:
        raw = range_match.group(1)
    match = re.search(r"\$?([\d,]+)(?:\.\d+)?", raw)
    if match:
        return int(match.group(1).replace(",", ""))
    return None


# ---------------------------------------------------------------------------
# Link normalization
# ---------------------------------------------------------------------------

def normalize_link(href: Optional[str]) -> Optional[str]:
    if not href:
        return None
    href = href.strip()
    # Strip eBay tracking parameters — preserve the item ID portion
    # Typical: https://www.ebay.com/itm/<id>?hash=...&...
    match = re.match(r"(https://www\.ebay\.com/itm/[^?#]+)", href)
    if match:
        return match.group(1)
    # For relative links (rare on eBay) resolve against base
    if href.startswith("/"):
        return urljoin("https://www.ebay.com", href)
    return href


# ---------------------------------------------------------------------------
# Phase 1: HTTP fetch and diagnostic capture
# ---------------------------------------------------------------------------

def build_search_url(query: str) -> str:
    params = {"_nkw": query, "_sacat": "0"}
    return f"{EBAY_SEARCH_BASE}?{urlencode(params)}"


def detect_challenge(html: str, soup: BeautifulSoup) -> dict:
    html_lower = html.lower()
    triggered = [sig for sig in CHALLENGE_SIGNALS if sig in html_lower]
    title_text = soup.title.get_text(strip=True) if soup.title else ""
    return {
        "challenge_detected": bool(triggered),
        "triggered_signals": triggered,
        "page_title": title_text,
    }


def detect_server_rendering(soup: BeautifulSoup) -> dict:
    found = {}
    for selector in SERVER_RENDERED_SIGNALS:
        els = soup.select(selector)
        found[selector] = len(els)
    any_found = any(v > 0 for v in found.values())
    return {
        "appears_server_rendered": any_found,
        "selector_hit_counts": found,
    }


def detect_listing_cards(soup: BeautifulSoup) -> dict:
    for selector in LISTING_CARD_SELECTORS:
        cards = soup.select(selector)
        if cards:
            return {
                "cards_accessible": True,
                "selector_used": selector,
                "card_count": len(cards),
            }
    return {
        "cards_accessible": False,
        "selector_used": None,
        "card_count": 0,
    }


def fetch_ebay(query: str) -> dict:
    url = build_search_url(query)
    redirect_chain = []
    status_code = None
    final_url = url
    html = ""
    error = None
    elapsed_ms = None

    t0 = time.monotonic()
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
        error = "request_timeout"
        elapsed_ms = int((time.monotonic() - t0) * 1000)
    except requests.exceptions.ConnectionError as exc:
        error = f"connection_error: {exc}"
        elapsed_ms = int((time.monotonic() - t0) * 1000)
    except requests.exceptions.RequestException as exc:
        error = f"request_error: {exc}"
        elapsed_ms = int((time.monotonic() - t0) * 1000)

    return {
        "query": query,
        "url": url,
        "final_url": final_url,
        "status_code": status_code,
        "redirected": bool(redirect_chain),
        "redirect_chain": redirect_chain,
        "html": html,
        "html_length": len(html),
        "error": error,
        "elapsed_ms": elapsed_ms,
    }


# ---------------------------------------------------------------------------
# Phase 1: Listing extraction
# ---------------------------------------------------------------------------

def extract_candidates(soup: BeautifulSoup, source_query: str, limit: int = 20) -> list[CandidateListing]:
    cards = []
    selector_used = None

    for selector in LISTING_CARD_SELECTORS:
        found = soup.select(selector)
        if found:
            cards = found
            selector_used = selector
            break

    if not cards:
        return []

    candidates: list[CandidateListing] = []

    for card in cards[:limit]:
        title_el = card.select_one(".s-item__title")
        price_el = card.select_one(".s-item__price")
        link_el = card.select_one(".s-item__link")
        location_el = card.select_one(".s-item__location")

        if not title_el or not link_el:
            continue

        raw_title = title_el.get_text(" ", strip=True)

        # eBay injects a phantom "Shop on eBay" card as the first result
        if "shop on ebay" in raw_title.lower():
            continue

        raw_price = price_el.get_text(strip=True) if price_el else None
        raw_location = location_el.get_text(strip=True) if location_el else None
        raw_link = link_el.get("href") if link_el else None

        price = parse_price(raw_price)
        link = normalize_link(raw_link)
        if not link:
            continue

        # Clean location strings like "from United States", "from China", etc.
        location = None
        if raw_location:
            location = re.sub(r"^from\s+", "", raw_location, flags=re.IGNORECASE).strip()

        candidates.append(
            CandidateListing(
                source="ebay",
                title=raw_title,
                price=price,
                location=location,
                link=link,
            )
        )

    return candidates


# ---------------------------------------------------------------------------
# Phase 2: Browser escalation assessment (no implementation)
# ---------------------------------------------------------------------------

BROWSER_ASSESSMENT = {
    "playwright_likely_viable_on_raven": True,
    "rationale": (
        "eBay's search page does not structurally require JavaScript rendering for "
        "the listing cards themselves — the anti-bot layer (PerimeterX / HUMAN Defense) "
        "is the real barrier. Playwright with a stealth-configured Chromium context can "
        "bypass this, but introduces several trade-offs described below."
    ),
    "complexity_estimate": "medium",
    "complexity_detail": (
        "Playwright is already present in requirements.txt. "
        "The main work is: (1) launching Chromium with stealth headers and a real viewport, "
        "(2) waiting for the SRP (Search Results Page) to hydrate, "
        "(3) extracting the same .s-item cards. "
        "No login is required for basic eBay search."
    ),
    "maintenance_burden": "medium-high",
    "maintenance_detail": (
        "eBay is a high-value target for anti-bot vendors. "
        "PerimeterX / HUMAN Defense fingerprinting changes periodically. "
        "A working Playwright profile can break within weeks without notice. "
        "Selector drift is also a risk — eBay A/B tests its SRP layout aggressively. "
        "Expect to revisit the adapter every 1-3 months."
    ),
    "anti_bot_risk": "high",
    "anti_bot_detail": (
        "eBay uses PerimeterX (now HUMAN Defense) for bot detection. "
        "Basic requests with a real UA may work transiently or in some network environments. "
        "IP reputation, TLS fingerprint, browser fingerprint, and behavioral signals are all checked. "
        "A headless Playwright browser without extra stealth (playwright-stealth or equivalent) "
        "will likely be blocked on production eBay within seconds. "
        "Even with stealth, rate limits and CAPTCHA escalation are possible."
    ),
    "memory_runtime_impact_on_raven": "moderate",
    "memory_detail": (
        "One headless Chromium instance uses approximately 150-300 MB RAM at idle and up to "
        "500 MB+ during active page load. Raven has ~12 GB RAM so a single browser context "
        "per hunt cycle is acceptable. "
        "DO NOT run concurrent Chromium sessions. "
        "One browser context, one page, close immediately after extraction. "
        "Monitor with `free -h` and `ps aux` to confirm no zombie processes."
    ),
    "disk_impact_on_raven": "low-once",
    "disk_detail": (
        "Playwright Chromium browser binary is ~300 MB and is downloaded once via "
        "`playwright install chromium`. This is acceptable on the 32 GB SATA M.2 SSD "
        "but should be tracked since it is a meaningful chunk of available space. "
        "Avoid installing the full playwright browser suite (firefox, webkit)."
    ),
    "recommendation": (
        "Do NOT implement Playwright integration yet. "
        "Evaluate requests-only behavior first across multiple search terms and network environments. "
        "If requests returns consistent HTTP 200 with extractable listing cards, "
        "a production adapter may not need browser automation at all. "
        "Only escalate to Playwright if requests consistently fails or returns challenge pages. "
        "If Playwright is pursued, implement it as a separate optional code path "
        "behind a flag in the adapter, not as the default execution path."
    ),
    "verdict_for_vulture": (
        "eBay should remain EXPERIMENTAL. "
        "Do not promote to a stable adapter until at least 5 independent runs across "
        "different search terms confirm consistent listings. "
        "Maintenance cost is real. Schedule periodic re-probing after any eBay layout change."
    ),
}


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def print_section(title: str) -> None:
    print()
    print("=" * 60)
    print(f"  {title}")
    print("=" * 60)


def run_probe(query: str, limit: int = 20) -> None:
    print_section("eBay Recon Probe — Phase 1: requests fetch")
    print(f"  Query     : {query!r}")
    print(f"  Limit     : {limit}")

    fetch = fetch_ebay(query)

    print_section("HTTP Diagnostics")
    print(f"  URL        : {fetch['url']}")
    print(f"  Status     : {fetch['status_code']}")
    print(f"  Final URL  : {fetch['final_url']}")
    print(f"  Redirected : {fetch['redirected']}")
    if fetch["redirect_chain"]:
        for i, r in enumerate(fetch["redirect_chain"], 1):
            print(f"    hop {i}   : {r}")
    print(f"  HTML len   : {fetch['html_length']:,} bytes")
    print(f"  Elapsed    : {fetch['elapsed_ms']} ms")
    if fetch["error"]:
        print(f"  ERROR      : {fetch['error']}")
        print()
        print("Fetch failed — cannot continue with parsing.")
        return

    soup = BeautifulSoup(fetch["html"], "lxml")

    challenge = detect_challenge(fetch["html"], soup)
    print_section("Challenge / Anti-bot Detection")
    print(f"  Challenge detected   : {challenge['challenge_detected']}")
    print(f"  Page title           : {challenge['page_title']!r}")
    if challenge["triggered_signals"]:
        print(f"  Triggered signals    : {challenge['triggered_signals']}")
    else:
        print("  Triggered signals    : none")

    rendering = detect_server_rendering(soup)
    print_section("Server-Rendered Content Detection")
    print(f"  Appears server-rendered : {rendering['appears_server_rendered']}")
    for selector, count in rendering["selector_hit_counts"].items():
        print(f"    {selector:40s} : {count} elements")

    cards_info = detect_listing_cards(soup)
    print_section("Listing Card Accessibility")
    print(f"  Cards accessible : {cards_info['cards_accessible']}")
    print(f"  Selector used    : {cards_info['selector_used']}")
    print(f"  Card count       : {cards_info['card_count']}")

    candidates = extract_candidates(soup, query, limit=limit)

    print_section(f"Candidate Listings ({len(candidates)} extracted)")
    if not candidates:
        print("  No candidate listings could be extracted.")
    else:
        for i, c in enumerate(candidates, 1):
            d = asdict(c)
            print(f"\n  [{i}]")
            for k, v in d.items():
                print(f"    {k:10s}: {v}")

    print_section("Phase 1 Verdict")
    if challenge["challenge_detected"]:
        print("  BLOCKED — challenge / anti-bot page detected.")
        print("  requests-only is NOT viable in this environment.")
        requests_viable = False
    elif not cards_info["cards_accessible"]:
        print("  PARTIAL — HTTP 200 but no listing cards found.")
        print("  Possible JS-rendered content or layout change.")
        print("  requests-only is UNCERTAIN — further investigation needed.")
        requests_viable = False
    elif candidates:
        print(f"  SUCCESS — {len(candidates)} normalized candidates extracted.")
        print("  requests-only appears VIABLE in this environment.")
        requests_viable = True
    else:
        print("  PARTIAL — Cards found by selector but no candidates normalized.")
        print("  Possible selector drift or empty result set.")
        requests_viable = False

    print_section("Phase 2: Browser Escalation Assessment")
    for key, val in BROWSER_ASSESSMENT.items():
        if isinstance(val, str) and len(val) > 80:
            print(f"\n  [{key}]")
            # Word-wrap at ~70 chars
            words = val.split()
            line = "    "
            for word in words:
                if len(line) + len(word) + 1 > 74:
                    print(line)
                    line = "    " + word
                else:
                    line += (" " if line.strip() else "") + word
            if line.strip():
                print(line)
        else:
            print(f"\n  {key}: {val}")

    print_section("Summary for Engineering Decision")
    print(f"  requests_only_viable     : {requests_viable}")
    print(f"  browser_required         : {not requests_viable}")
    print(f"  anti_bot_risk            : {BROWSER_ASSESSMENT['anti_bot_risk']}")
    print(f"  maintenance_burden       : {BROWSER_ASSESSMENT['maintenance_burden']}")
    print(f"  memory_impact_on_raven   : {BROWSER_ASSESSMENT['memory_runtime_impact_on_raven']}")
    print(f"  should_stay_experimental : True")
    print()
    print("  RECOMMENDATION:")
    for line in BROWSER_ASSESSMENT["recommendation"].split(". "):
        if line.strip():
            print(f"    - {line.strip()}.")
    print()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Vulture 2.0 eBay reconnaissance probe (experimental, isolated)"
    )
    parser.add_argument(
        "query",
        nargs="?",
        default="rtx 3080",
        help="Search term to probe on eBay (default: 'rtx 3080')",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=20,
        help="Maximum number of candidate listings to extract (default: 20)",
    )
    args = parser.parse_args()
    run_probe(query=args.query, limit=args.limit)


if __name__ == "__main__":
    main()
