"""
eBay Recon Probe — Vulture 2.0 Experimental Reconnaissance
===========================================================

Phase 1:  requests-only fetch analysis
Phase 1b: curl_cffi TLS-impersonation probe  (--cffi flag)
Phase 2:  Playwright browser escalation assessment (recommendation)

Pipeline contract being evaluated:
    hunt -> adapter -> normalized Listing -> deterministic rules -> dedupe -> alert

This script is EXPERIMENTAL and ISOLATED.
It does NOT:
  - write to SQLite
  - send Discord alerts
  - modify hunt execution
  - modify Discord behavior
  - connect to any Vulture runtime

Confirmed findings (2026-05-23 / 2026-05-24):
  Phase 1 — plain requests + Chrome UA:
    Cloud agent (datacenter IP): HTTP 403 / 535 bytes / "Access Denied"
    Raven (residential IP):      HTTP 403 / 389 bytes / "Access Denied"
    → Both blocked. TLS fingerprint mismatch suspected.

  Phase 1b — curl_cffi Chrome124 TLS impersonation:
    Cloud agent (datacenter IP): HTTP 403 / 546 bytes / "Access Denied"
    Raven (residential IP):      HTTP 403 / 546 bytes / "Access Denied"
    → Both blocked even with correct TLS fingerprint from residential IP.

  Conclusion: eBay's HUMAN Defense requires more than TLS impersonation.
  It almost certainly issues a JavaScript challenge that must be computed
  and returned by a real browser engine before the SRP is served.
  Non-browser HTTP clients cannot pass this challenge regardless of IP
  or TLS fingerprint.

  Next step: ebay_playwright_probe.py with stealth Chromium config.
  See experiments/adapters/ebay_playwright_probe.py.

Usage:
    python3 experiments/adapters/ebay_probe.py
    python3 experiments/adapters/ebay_probe.py "rtx 3080"
    python3 experiments/adapters/ebay_probe.py "rtx 3080" --limit 5
    python3 experiments/adapters/ebay_probe.py "rtx 3080" --cffi --limit 5
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

# HTML body size threshold below which a 403 is almost certainly a TLS/IP
# block rather than a CAPTCHA challenge page (no form content).
BARE_BLOCK_THRESHOLD_BYTES = 2_000

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


def diagnose_403(html: str) -> dict:
    """
    When eBay returns HTTP 403 we distinguish two cases:

    1. Bare block (< BARE_BLOCK_THRESHOLD_BYTES):
       Almost certainly a TLS fingerprint or IP-level reject before any HTML
       is served. Python's requests/urllib3 uses a TLS ClientHello that differs
       from a real Chrome browser — eBay's HUMAN Defense (PerimeterX) checks
       this fingerprint and rejects non-browser clients outright.

    2. Challenge page (>= threshold, has captcha/challenge form):
       eBay served a full challenge page. The IP passed the TLS check but
       triggered behavioral or IP-reputation detection.

    The distinction matters for choosing the next step:
    - Bare block     → try curl_cffi (Chrome TLS impersonation) first
    - Challenge page → curl_cffi may also help; Playwright needed if it does not
    """
    size = len(html)
    bare_block = size < BARE_BLOCK_THRESHOLD_BYTES
    return {
        "html_size_bytes": size,
        "bare_block": bare_block,
        "likely_root_cause": (
            "tls_fingerprint_or_ip_level_reject" if bare_block
            else "challenge_page_served"
        ),
        "explanation": (
            "Response body is very small — eBay rejected the connection before "
            "serving real content. Most likely cause: Python requests/urllib3 TLS "
            "ClientHello does not match a real Chrome browser fingerprint. "
            "HUMAN Defense (PerimeterX) checks JA3/JA4 TLS fingerprint among other "
            "signals. curl_cffi (Chrome TLS impersonation) is the lowest-cost next "
            "step before committing to full Playwright."
            if bare_block else
            "eBay served a challenge/CAPTCHA page. TLS fingerprint may have passed "
            "but IP reputation or behavioral signals triggered the challenge. "
            "curl_cffi may help; Playwright with stealth is the stronger fallback."
        ),
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
    # -----------------------------------------------------------------------
    # Phase 1b: curl_cffi — TLS impersonation without a browser
    # -----------------------------------------------------------------------
    "phase_1b_curl_cffi_recommended": True,
    "curl_cffi_rationale": (
        "Both the cloud agent (datacenter IP) and Raven (residential IP) returned "
        "HTTP 403 with ~535-546 bytes — a bare block, not a CAPTCHA page. "
        "CONFIRMED (2026-05-24): Raven residential IP + curl_cffi Chrome124 TLS "
        "impersonation also returned HTTP 403 / 546 bytes. "
        "TLS fingerprint impersonation alone is INSUFFICIENT. "
        "eBay's HUMAN Defense requires browser JavaScript execution to issue and "
        "validate a challenge token before the SRP is served. Non-browser HTTP "
        "clients cannot satisfy this regardless of IP or TLS fingerprint. "
        "curl_cffi is ruled out as a path to a production eBay adapter."
    ),
    "curl_cffi_install": "pip install curl_cffi",
    "curl_cffi_disk_impact": "~5 MB — negligible on Raven",
    "curl_cffi_usage_sketch": (
        "from curl_cffi import requests as cffi_requests; "
        "r = cffi_requests.get(url, impersonate='chrome124'); "
        "# then parse r.text with BeautifulSoup as normal"
    ),
    # -----------------------------------------------------------------------
    # Phase 2: Playwright — full browser automation
    # -----------------------------------------------------------------------
    "playwright_likely_viable_on_raven": True,
    "playwright_rationale": (
        "CONFIRMED (2026-05-24): Raven residential IP + curl_cffi Chrome124 TLS "
        "impersonation returned HTTP 403 / 546 bytes. "
        "All non-browser HTTP approaches are blocked. "
        "eBay's HUMAN Defense issues a JavaScript challenge that requires a real browser "
        "engine to execute and return a token — no HTTP client can fake this. "
        "Playwright running real Chromium is the only remaining non-API path. "
        "However, bare headless Playwright will also be detected via canvas/WebGL/navigator "
        "fingerprint checks. A stealth plugin (playwright-stealth or equivalent) is required. "
        "See ebay_playwright_probe.py for the next evaluation step."
    ),
    "playwright_complexity_estimate": "medium",
    "playwright_complexity_detail": (
        "Playwright is already present in requirements.txt. "
        "The main work is: (1) launching Chromium with stealth headers and a real viewport, "
        "(2) waiting for the SRP (Search Results Page) to hydrate, "
        "(3) extracting the same .s-item cards. "
        "No login is required for basic eBay search. "
        "A bare headless Playwright launch will likely also be blocked — "
        "a stealth plugin (playwright-stealth or similar) is needed to pass "
        "canvas/WebGL/navigator fingerprint checks."
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
        "IP reputation, TLS fingerprint (JA3/JA4), JavaScript challenge token, "
        "browser canvas/WebGL fingerprint, and behavioral timing signals are all checked. "
        "CONFIRMED: Raven residential IP + curl_cffi Chrome124 TLS impersonation = 403. "
        "JavaScript challenge execution in a real browser engine is required. "
        "A bare headless Playwright browser without stealth will likely also be blocked "
        "via canvas/WebGL/navigator fingerprint checks. "
        "Even with playwright-stealth, CAPTCHA escalation is possible at higher request rates."
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
        "curl_cffi is RULED OUT (2026-05-24 Raven residential IP test confirmed). "
        "The only remaining non-API path is Playwright with a stealth Chromium config. "
        "Next step: run ebay_playwright_probe.py from Raven. "
        "Install Chromium once: `playwright install chromium` (~300 MB). "
        "Evaluate whether bare headless Playwright returns HTTP 200 with listing cards. "
        "If bare Playwright is also blocked, install playwright-stealth and retry. "
        "Do NOT wire Playwright into the production hunt dispatch until at least "
        "5 stable runs confirm consistent listings from Raven. "
        "If Playwright succeeds, implement it as a separate optional code path "
        "behind an experimental flag, not as the default execution path."
    ),
    "verdict_for_vulture": (
        "eBay should remain EXPERIMENTAL. "
        "CONFIRMED blocked on Raven residential IP with both plain requests and "
        "curl_cffi Chrome TLS impersonation. "
        "Browser JavaScript execution is required — Playwright is the only viable path. "
        "Maintenance burden is high: stealth config breaks every 1-3 months. "
        "Do not promote to a stable adapter. "
        "The pipeline integrity (hunt -> adapter -> Listing -> rules -> alert) "
        "can be preserved with a Playwright adapter, but it comes with ongoing cost."
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
    if fetch["status_code"] == 403:
        diagnosis = diagnose_403(fetch["html"])
        print(f"  BLOCKED — HTTP 403 received.")
        print(f"  Bare block (tiny body): {diagnosis['bare_block']}")
        print(f"  Likely root cause     : {diagnosis['likely_root_cause']}")
        print()
        words = diagnosis["explanation"].split()
        line = "  "
        for word in words:
            if len(line) + len(word) + 1 > 72:
                print(line)
                line = "  " + word
            else:
                line += (" " if line.strip() else "") + word
        if line.strip():
            print(line)
        print()
        print("  requests-only is NOT viable. See Phase 1b (curl_cffi) below.")
        requests_viable = False
    elif challenge["challenge_detected"]:
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

    print_section("Phase 1b: curl_cffi TLS Impersonation — Next Step")
    print(f"  Recommended : {BROWSER_ASSESSMENT['phase_1b_curl_cffi_recommended']}")
    print(f"  Install     : {BROWSER_ASSESSMENT['curl_cffi_install']}")
    print(f"  Disk impact : {BROWSER_ASSESSMENT['curl_cffi_disk_impact']}")
    print()
    print("  Rationale:")
    words = BROWSER_ASSESSMENT["curl_cffi_rationale"].split()
    line = "    "
    for word in words:
        if len(line) + len(word) + 1 > 74:
            print(line)
            line = "    " + word
        else:
            line += (" " if line.strip() else "") + word
    if line.strip():
        print(line)
    print()
    print("  Usage sketch:")
    print(f"    {BROWSER_ASSESSMENT['curl_cffi_usage_sketch']}")

    print_section("Phase 2: Playwright Browser Escalation Assessment")
    skip_keys = {
        "phase_1b_curl_cffi_recommended",
        "curl_cffi_rationale",
        "curl_cffi_install",
        "curl_cffi_disk_impact",
        "curl_cffi_usage_sketch",
    }
    for key, val in BROWSER_ASSESSMENT.items():
        if key in skip_keys:
            continue
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
    print(f"  curl_cffi_next_step      : {BROWSER_ASSESSMENT['phase_1b_curl_cffi_recommended']}")
    print(f"  playwright_if_needed     : {BROWSER_ASSESSMENT['playwright_likely_viable_on_raven']}")
    print(f"  anti_bot_risk            : {BROWSER_ASSESSMENT['anti_bot_risk']}")
    print(f"  maintenance_burden       : {BROWSER_ASSESSMENT['maintenance_burden']}")
    print(f"  memory_impact_on_raven   : {BROWSER_ASSESSMENT['memory_runtime_impact_on_raven']}")
    print(f"  should_stay_experimental : True")
    print()
    print("  RECOMMENDATION:")
    for step in BROWSER_ASSESSMENT["recommendation"].split(". "):
        if step.strip():
            print(f"    - {step.strip()}.")
    print()


# ---------------------------------------------------------------------------
# Phase 1b: curl_cffi TLS-impersonation probe
# ---------------------------------------------------------------------------

def fetch_ebay_cffi(query: str) -> dict:
    """
    Attempt the same fetch using curl_cffi, which impersonates Chrome's TLS
    ClientHello (JA3/JA4 fingerprint). This is the lowest-cost escalation
    from plain requests — no browser binary required.

    Returns the same shape as fetch_ebay() so run_probe_cffi() can reuse
    the existing parsing/reporting logic.
    """
    try:
        from curl_cffi import requests as cffi_requests  # type: ignore[import]
    except ImportError:
        return {
            "query": query,
            "url": build_search_url(query),
            "final_url": None,
            "status_code": None,
            "redirected": False,
            "redirect_chain": [],
            "html": "",
            "html_length": 0,
            "error": "curl_cffi_not_installed: run `pip install curl_cffi` first",
            "elapsed_ms": 0,
        }

    url = build_search_url(query)
    redirect_chain = []
    status_code = None
    final_url = url
    html = ""
    error = None
    elapsed_ms = None

    t0 = time.monotonic()
    try:
        response = cffi_requests.get(
            url,
            impersonate="chrome124",
            timeout=REQUEST_TIMEOUT,
            allow_redirects=True,
        )
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        status_code = response.status_code
        final_url = str(response.url)
        # curl_cffi response.history is a list of responses
        redirect_chain = [str(r.url) for r in getattr(response, "history", [])]
        html = response.text
    except Exception as exc:
        error = f"cffi_error: {exc}"
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


def run_probe_cffi(query: str, limit: int = 20) -> None:
    """Run Phase 1b: same diagnostic flow using curl_cffi impersonation."""
    print_section("Phase 1b: curl_cffi TLS Impersonation Probe")
    print(f"  Query     : {query!r}")
    print(f"  Limit     : {limit}")
    print(f"  Impersonate: chrome124")

    fetch = fetch_ebay_cffi(query)

    print_section("HTTP Diagnostics (curl_cffi)")
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
        if "not_installed" in (fetch["error"] or ""):
            print()
            print("  To install: pip install curl_cffi")
        return

    soup = BeautifulSoup(fetch["html"], "lxml")
    challenge = detect_challenge(fetch["html"], soup)
    cards_info = detect_listing_cards(soup)

    print_section("Challenge Detection (curl_cffi)")
    print(f"  Challenge detected : {challenge['challenge_detected']}")
    print(f"  Page title         : {challenge['page_title']!r}")
    if challenge["triggered_signals"]:
        print(f"  Triggered signals  : {challenge['triggered_signals']}")

    print_section("Listing Cards (curl_cffi)")
    print(f"  Cards accessible : {cards_info['cards_accessible']}")
    print(f"  Selector used    : {cards_info['selector_used']}")
    print(f"  Card count       : {cards_info['card_count']}")

    candidates = extract_candidates(soup, query, limit=limit)
    print_section(f"Candidate Listings — curl_cffi ({len(candidates)} extracted)")
    if not candidates:
        print("  No candidates extracted.")
    else:
        for i, c in enumerate(candidates, 1):
            d = asdict(c)
            print(f"\n  [{i}]")
            for k, v in d.items():
                print(f"    {k:10s}: {v}")

    print_section("Phase 1b Verdict")
    if fetch["status_code"] == 200 and candidates:
        print(f"  SUCCESS — curl_cffi TLS impersonation bypassed the block.")
        print(f"  {len(candidates)} candidates extracted.")
        print("  A curl_cffi-based adapter is viable. Playwright not needed.")
    elif fetch["status_code"] == 200 and cards_info["cards_accessible"]:
        print("  PARTIAL — HTTP 200 but no candidates normalized.")
        print("  Selector drift or empty result set. Needs selector review.")
    elif fetch["status_code"] == 200:
        print("  PARTIAL — HTTP 200 but no listing cards found.")
        print("  Possible JS rendering still required. Escalate to Playwright.")
    elif fetch["status_code"] == 403:
        print("  STILL BLOCKED (from this host) — curl_cffi also returned 403.")
        print()
        print("  IMPORTANT: if this result was produced from a datacenter/cloud IP,")
        print("  it is NOT conclusive for Raven. eBay's IP blocklist flags datacenter")
        print("  ranges regardless of TLS fingerprint. The meaningful test is:")
        print()
        print("    On Raven (residential IP):")
        print("      pip install curl_cffi")
        print("      python3 experiments/adapters/ebay_probe.py 'rtx 3080' --cffi")
        print()
        print("  If Raven + curl_cffi also returns 403, TLS alone is insufficient")
        print("  and Playwright with stealth config is the next step.")
    else:
        print(f"  UNKNOWN — status {fetch['status_code']}. Manual review required.")


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
    parser.add_argument(
        "--cffi",
        action="store_true",
        help=(
            "Run Phase 1b: use curl_cffi Chrome TLS impersonation instead of "
            "plain requests. Requires: pip install curl_cffi"
        ),
    )
    args = parser.parse_args()
    if args.cffi:
        run_probe_cffi(query=args.query, limit=args.limit)
    else:
        run_probe(query=args.query, limit=args.limit)


if __name__ == "__main__":
    main()
