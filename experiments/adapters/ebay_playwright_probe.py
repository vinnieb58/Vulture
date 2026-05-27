"""
eBay Playwright Recon Probe — Vulture 2.0 Phase 2 Experimental
===============================================================

This script is the Phase 2 escalation after plain requests AND curl_cffi
both failed on Raven (2026-05-24). Browser JavaScript execution is required
to pass eBay's HUMAN Defense challenge.

RECON COMPLETE — ALL SCRAPING PATHS EXHAUSTED (2026-05-27)

Confirmed findings — Raven residential IP:
  Phase 1  — requests + Chrome UA:         HTTP 403 / 389 bytes
  Phase 1b — curl_cffi Chrome124:          HTTP 403 / 546 bytes
  Phase 2  — bare headless Playwright:     HTTP 403 / 301 bytes
  Phase 2b — playwright-stealth:           HTTP 403 / 301 bytes  ← FINAL

Key observation: bare Playwright and playwright-stealth return identical
301-byte bodies. playwright-stealth patches JS fingerprints but the block
is happening at the TLS/network layer before any JS executes.
stealth cannot affect that layer.

CONCLUSION: eBay scraping is not viable from Raven.
RECOMMENDED PATH: eBay Browse API.
  https://developer.ebay.com/develop/apis/restful-apis/browse-api

This probe is preserved as a historical record of the recon process.
It can be re-run in the future if circumstances change (e.g., different
network environment, proxy, or eBay anti-bot vendor change).

This probe supports:
  bare mode (default)   — baseline measurement
  stealth mode          — with playwright-stealth applied (--stealth flag)

IMPORTANT — this probe DOES NOT:
  - write to SQLite
  - send Discord alerts
  - modify hunt execution or Discord behavior
  - represent a production adapter

One Chromium instance uses 150-500 MB RAM. Raven (~12 GB RAM) can handle
this. The browser is closed promptly after each run.

Prerequisites on Raven:
    playwright install chromium          # one-time, ~300 MB (already done)
    pip install playwright-stealth       # needed for --stealth mode (~small)

Usage:
    python3 experiments/adapters/ebay_playwright_probe.py "rtx 3080"
    python3 experiments/adapters/ebay_playwright_probe.py "rtx 3080" --limit 10
    python3 experiments/adapters/ebay_playwright_probe.py "rtx 3080" --stealth --limit 10
    python3 experiments/adapters/ebay_playwright_probe.py "rtx 3080" --stealth --slow --limit 10
"""

import re
import sys
import time
import argparse
from dataclasses import dataclass, asdict
from typing import Optional
from urllib.parse import urlencode

try:
    from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
except ImportError:
    print("ERROR: playwright is not installed.")
    print("Install: pip install playwright && playwright install chromium")
    sys.exit(1)

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
# Config
# ---------------------------------------------------------------------------

EBAY_SEARCH_BASE = "https://www.ebay.com/sch/i.html"

# Viewport matches a common 1080p laptop resolution
VIEWPORT = {"width": 1280, "height": 800}

# Realistic Chrome 124 user agent for a Windows desktop
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

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
    "access denied",
    "px-captcha",
    "px-spinner",
    "_pxParam",
    "datadome",
    "incapsula",
    "verify you are human",
]

LISTING_CARD_SELECTORS = [
    ".s-item",
    "li.s-item",
    ".srp-results .s-item",
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def build_search_url(query: str) -> str:
    params = {"_nkw": query, "_sacat": "0"}
    return f"{EBAY_SEARCH_BASE}?{urlencode(params)}"


def parse_price(raw: Optional[str]) -> Optional[int]:
    if not raw:
        return None
    range_match = re.match(r"\$?([\d,]+\.?\d*)\s+to\s+\$?([\d,]+\.?\d*)", raw, re.IGNORECASE)
    if range_match:
        raw = range_match.group(1)
    match = re.search(r"\$?([\d,]+)(?:\.\d+)?", raw)
    if match:
        return int(match.group(1).replace(",", ""))
    return None


def normalize_link(href: Optional[str]) -> Optional[str]:
    if not href:
        return None
    href = href.strip()
    match = re.match(r"(https://www\.ebay\.com/itm/[^?#]+)", href)
    if match:
        return match.group(1)
    return href


def print_section(title: str) -> None:
    print()
    print("=" * 60)
    print(f"  {title}")
    print("=" * 60)


def detect_challenge(html: str) -> dict:
    html_lower = html.lower()
    triggered = [sig for sig in CHALLENGE_SIGNALS if sig in html_lower]
    return {
        "challenge_detected": bool(triggered),
        "triggered_signals": triggered,
    }


def extract_candidates(html: str, limit: int = 20) -> list[CandidateListing]:
    soup = BeautifulSoup(html, "lxml")
    cards = []
    for selector in LISTING_CARD_SELECTORS:
        found = soup.select(selector)
        if found:
            cards = found
            break

    candidates: list[CandidateListing] = []
    for card in cards[:limit]:
        title_el = card.select_one(".s-item__title")
        price_el = card.select_one(".s-item__price")
        link_el = card.select_one(".s-item__link")
        location_el = card.select_one(".s-item__location")

        if not title_el or not link_el:
            continue

        raw_title = title_el.get_text(" ", strip=True)
        if "shop on ebay" in raw_title.lower():
            continue

        price = parse_price(price_el.get_text(strip=True) if price_el else None)
        link = normalize_link(link_el.get("href") if link_el else None)
        if not link:
            continue

        raw_location = location_el.get_text(strip=True) if location_el else None
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
# Playwright fetch
# ---------------------------------------------------------------------------

def run_playwright_probe(query: str, limit: int = 20, slow: bool = False, stealth: bool = False) -> None:
    url = build_search_url(query)

    stealth_available = False
    stealth_fn = None
    if stealth:
        # playwright-stealth 2.x uses Stealth class; 1.x used stealth_sync.
        # Try both so the probe works across versions.
        try:
            from playwright_stealth import Stealth  # type: ignore[import]
            _stealth_instance = Stealth()
            stealth_fn = _stealth_instance.apply_stealth_sync
            stealth_available = True
        except (ImportError, AttributeError):
            try:
                from playwright_stealth import stealth_sync  # type: ignore[import]
                stealth_fn = stealth_sync
                stealth_available = True
            except ImportError:
                print()
                print("ERROR: --stealth requires playwright-stealth.")
                print("  Install: pip install playwright-stealth")
                print()
                sys.exit(1)

    mode_label = "stealth (playwright-stealth)" if (stealth and stealth_available) else "bare headless"
    print_section(f"eBay Playwright Probe — Phase 2 Recon [{mode_label}]")
    print(f"  Query      : {query!r}")
    print(f"  Limit      : {limit}")
    print(f"  Stealth    : {stealth and stealth_available}")
    print(f"  Slow mode  : {slow}")
    print(f"  Headless   : True (required for Raven server)")
    print(f"  Viewport   : {VIEWPORT['width']}x{VIEWPORT['height']}")
    print(f"  UA         : {USER_AGENT[:60]}...")

    t0 = time.monotonic()
    final_url = None
    page_title = None
    html = ""
    status_code = None
    error = None

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
                # Suppress automation flags that eBay detects
                "--disable-automation",
                "--disable-infobars",
                "--disable-extensions",
            ],
        )
        context = browser.new_context(
            viewport=VIEWPORT,
            user_agent=USER_AGENT,
            locale="en-US",
            timezone_id="America/Chicago",
            extra_http_headers={
                "Accept-Language": "en-US,en;q=0.9",
                "Accept-Encoding": "gzip, deflate, br",
            },
        )

        # Always mask navigator.webdriver at the context level
        context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )

        page = context.new_page()

        # Apply playwright-stealth if requested — patches canvas, WebGL,
        # navigator plugins, permissions, and other fingerprint vectors
        if stealth_fn:
            stealth_fn(page)

        try:
            response = page.goto(
                url,
                wait_until="domcontentloaded",
                timeout=60_000,
            )
            status_code = response.status if response else None

            # Give the page time to execute the HUMAN Defense JS challenge
            wait_ms = 6_000 if slow else 3_500
            page.wait_for_timeout(wait_ms)

            final_url = page.url
            page_title = page.title()
            html = page.content()

        except PlaywrightTimeoutError as exc:
            error = f"playwright_timeout: {exc}"
        except Exception as exc:
            error = f"playwright_error: {exc}"
        finally:
            context.close()
            browser.close()

    elapsed_ms = int((time.monotonic() - t0) * 1000)

    print_section("HTTP / Navigation Diagnostics")
    print(f"  Initial URL    : {url}")
    print(f"  Final URL      : {final_url}")
    print(f"  HTTP status    : {status_code}")
    print(f"  Page title     : {page_title!r}")
    print(f"  HTML length    : {len(html):,} bytes")
    print(f"  Elapsed        : {elapsed_ms} ms")
    if error:
        print(f"  ERROR          : {error}")

    if error:
        print()
        print("  Probe failed with an error. Cannot continue.")
        return

    challenge = detect_challenge(html)
    print_section("Challenge / Anti-bot Detection")
    print(f"  Challenge detected : {challenge['challenge_detected']}")
    if challenge["triggered_signals"]:
        print(f"  Triggered signals  : {challenge['triggered_signals']}")
    else:
        print("  Triggered signals  : none")

    # Check listing card presence
    cards_found = False
    for selector in LISTING_CARD_SELECTORS:
        from bs4 import BeautifulSoup as _BS
        soup = _BS(html, "lxml")
        cards = soup.select(selector)
        if cards:
            print_section(f"Listing Card Detection")
            print(f"  Cards found      : {len(cards)}")
            print(f"  Selector used    : {selector}")
            cards_found = True
            break
    if not cards_found:
        print_section("Listing Card Detection")
        print("  No listing cards found with known selectors.")

    candidates = extract_candidates(html, limit=limit)
    print_section(f"Candidate Listings ({len(candidates)} extracted)")
    if not candidates:
        print("  No candidates extracted.")
    else:
        for i, c in enumerate(candidates, 1):
            d = asdict(c)
            print(f"\n  [{i}]")
            for k, v in d.items():
                print(f"    {k:10s}: {v}")

    mode_label = "stealth" if stealth else "bare"
    print_section(f"Phase 2 Verdict [{mode_label}]")
    if challenge["challenge_detected"]:
        if not stealth:
            print("  BLOCKED — challenge signals in bare headless Playwright HTML.")
            print()
            print("  Confirmed result (2026-05-27): bare headless = HTTP 403 / 301 bytes.")
            print("  The 301-byte body is smaller than curl_cffi (546 bytes), suggesting")
            print("  eBay detects headless Chromium at TLS/connection level, not just JS.")
            print()
            print("  Next step: run with --stealth flag.")
            print("  Install playwright-stealth first (small, no browser binary):")
            print()
            print("    pip install playwright-stealth")
            print("    python3 experiments/adapters/ebay_playwright_probe.py 'rtx 3080' --stealth --limit 10")
        else:
            print("  BLOCKED — challenge signals in stealth Playwright HTML.")
            print()
            print("  playwright-stealth did not bypass the challenge.")
            print("  eBay's HUMAN Defense is blocking at a layer stealth cannot patch.")
            print()
            print("  Remaining options:")
            print("    1. eBay API (Finding API / Browse API) — legitimate, stable,")
            print("       no scraping required. Requires eBay developer account.")
            print("       https://developer.ebay.com/develop/apis/restful-apis/browse-api")
            print("    2. Proxy rotation — residential proxies may help but add ongoing")
            print("       cost and complexity. Not recommended for Raven's use case.")
            print("    3. Accept eBay as out of scope for now.")
        playwright_viable = False
    elif not cards_found:
        print("  UNCERTAIN — HTTP 200 but no listing cards found.")
        print("  Page may have loaded a different layout or JS rendering is incomplete.")
        print("  Try --slow to give JS more time.")
        playwright_viable = False
    elif candidates:
        print(f"  SUCCESS — {len(candidates)} candidates extracted ({mode_label} mode).")
        print("  A Playwright-based adapter may be viable.")
        print("  Run --slow and 2-3 more search terms to confirm stability.")
        playwright_viable = True
    else:
        print("  PARTIAL — Cards found but no candidates normalized.")
        print("  Possible selector drift. Inspect the HTML.")
        playwright_viable = False

    print_section("Summary")
    print(f"  mode                     : {mode_label}")
    print(f"  playwright_viable        : {playwright_viable}")
    print(f"  should_stay_experimental : True")
    print()
    if not playwright_viable and not stealth:
        print("  NEXT STEP:")
        print("    pip install playwright-stealth")
        print("    python3 experiments/adapters/ebay_playwright_probe.py 'rtx 3080' --stealth --limit 10")
    elif not playwright_viable and stealth:
        print("  NEXT STEP:")
        print("    Evaluate eBay Browse API as the stable path.")
        print("    https://developer.ebay.com/develop/apis/restful-apis/browse-api")
        print("    If API is off the table: accept eBay as out of scope.")
    else:
        print("  NEXT STEPS:")
        print("    1. Run --slow and 2-3 more search terms to confirm stability")
        print("    2. Test over multiple days to check for CAPTCHA escalation")
        print("    3. If stable, sketch adapter behind experimental flag")
    print()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Vulture 2.0 eBay Playwright recon probe — Phase 2 (experimental, isolated). "
            "Requires: playwright install chromium  (already done on Raven). "
            "For --stealth: pip install playwright-stealth"
        )
    )
    parser.add_argument(
        "query",
        nargs="?",
        default="rtx 3080",
        help="Search term (default: 'rtx 3080')",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=20,
        help="Max candidates to extract (default: 20)",
    )
    parser.add_argument(
        "--stealth",
        action="store_true",
        help=(
            "Apply playwright-stealth before page.goto(). "
            "Patches canvas, WebGL, navigator fingerprints. "
            "Requires: pip install playwright-stealth"
        ),
    )
    parser.add_argument(
        "--slow",
        action="store_true",
        help="Wait 6s after page load instead of 3.5s — more human-like",
    )
    args = parser.parse_args()
    run_playwright_probe(query=args.query, limit=args.limit, slow=args.slow, stealth=args.stealth)


if __name__ == "__main__":
    main()
