"""
Facebook Marketplace Playwright reconnaissance probe
======================================================
Reconnaissance only. Does NOT write to SQLite, send Discord alerts,
register in adapters/registry.py, touch .env, or add Facebook as a runtime source.

No login automation, stored cookies, or production adapter integration.

Usage:
    python experiments/adapters/facebook_marketplace_probe.py
    python experiments/adapters/facebook_marketplace_probe.py --query "rtx 3080"
    python experiments/adapters/facebook_marketplace_probe.py --query "rtx 3080" --limit 10 --screenshot-on-fail
    python experiments/adapters/facebook_marketplace_probe.py --headed --slowmo 100 --screenshot-on-fail

Prerequisites:
    pip install playwright beautifulsoup4 lxml
    python -m playwright install chromium
"""

from __future__ import annotations

import argparse
import re
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Optional
from urllib.parse import quote_plus, urljoin

from bs4 import BeautifulSoup

try:
    from playwright.sync_api import (
        Page,
        TimeoutError as PlaywrightTimeout,
        sync_playwright,
    )
except ImportError:
    print("ERROR: playwright is not installed.")
    print("Install: pip install playwright beautifulsoup4 lxml")
    print("Then:    python -m playwright install chromium")
    sys.exit(1)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

FACEBOOK_ORIGIN = "https://www.facebook.com"
MARKETPLACE_SEARCH_BASE = f"{FACEBOOK_ORIGIN}/marketplace/search"

DEFAULT_QUERIES = ["rtx 3080", "toyota sequoia", "75 inch tv"]

ARTIFACTS_DIR = Path("artifacts/facebook_marketplace_probe")

VIEWPORT = {"width": 1280, "height": 900}
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
LOCALE = "en-US"
TIMEZONE = "America/Chicago"

INITIAL_LOAD_MS = 3_500
SCROLL_PAUSE_MS = 1_200
SCROLL_COUNT = 3
DEFAULT_LIMIT = 10
DEFAULT_TIMEOUT_MS = 90_000

LOGIN_URL_FRAGMENTS = ["login.facebook.com", "/login.php", "/login/"]
LOGIN_TEXT_MARKERS = [
    "log in to facebook",
    "log into facebook",
    "log in to continue",
    "you must log in",
    "create new account",
    "forgot password",
]
LOGIN_DOM_MARKERS = [
    'id="loginform"',
    'name="login"',
    'data-testid="royal_login_form"',
]

CHECKPOINT_URL_FRAGMENTS = ["checkpoint", "/challenge/"]
CHECKPOINT_TEXT_MARKERS = [
    "confirm it's you",
    "confirm its you",
    "security check",
    "help us confirm",
    "enter security code",
    "two-factor authentication",
    "approve this login",
    "suspicious activity",
]

MARKETPLACE_CONTENT_MARKERS = [
    "/marketplace/item/",
    "marketplace/search",
    "marketplace/browse",
    "marketplace/you",
]

LISTING_CARD_SELECTORS = [
    'a[href*="/marketplace/item/"]',
    '[data-pagelet*="Marketplace"]',
    '[role="article"]',
    'div[class*="marketplace"]',
    'div[aria-label*="Marketplace"]',
]

FIELD_SELECTORS = {
    "title": [
        "span[dir='auto']",
        "span",
        "a[href*='/marketplace/item/']",
    ],
    "price": [
        "span:contains('$')",
    ],
    "location": [
        "span[dir='auto']",
    ],
}


@dataclass
class CandidateListing:
    title: Optional[str]
    price: Optional[str]
    location: Optional[str]
    link: Optional[str]


@dataclass
class QueryDiagnostics:
    query: str
    requested_url: str
    final_url: str
    page_title: str
    http_status: Optional[int]
    html_length: int
    load_ms: int
    login_required: Literal["yes", "no", "unknown"]
    challenge_detected: Literal["yes", "no", "unknown"]
    marketplace_content: bool
    candidate_elements: int
    listings: list[CandidateListing]
    nav_error: Optional[str]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")


def build_search_url(query: str) -> str:
    return f"{MARKETPLACE_SEARCH_BASE}/?query={quote_plus(query)}"


def _safe_text(el) -> Optional[str]:
    if el is None:
        return None
    try:
        text = el.get_text(" ", strip=True)
        return text if text else None
    except Exception:
        return None


def normalize_marketplace_link(href: Optional[str]) -> Optional[str]:
    if not href:
        return None
    try:
        href = str(href).strip()
        if not href or href in ("#", "javascript:void(0)"):
            return None
        if href.startswith("/"):
            href = urljoin(FACEBOOK_ORIGIN, href)
        if "/marketplace/item/" not in href:
            return None
        # Strip tracking query params but keep path
        match = re.match(r"(https?://[^?#]+/marketplace/item/\d+)", href)
        if match:
            return match.group(1)
        if href.startswith("http"):
            return href.split("?")[0]
        return href
    except Exception:
        return None


def _detect_login(final_url: str, title: str, html: str) -> Literal["yes", "no", "unknown"]:
    url_lower = (final_url or "").lower()
    title_lower = (title or "").lower()
    html_lower = (html or "").lower()

    if any(frag in url_lower for frag in LOGIN_URL_FRAGMENTS):
        return "yes"
    if "checkpoint" in url_lower and "login" in url_lower:
        return "yes"
    if any(m in title_lower for m in LOGIN_TEXT_MARKERS):
        return "yes"
    if any(m in html_lower for m in LOGIN_TEXT_MARKERS[:4]):
        return "yes"
    if any(m in html_lower for m in LOGIN_DOM_MARKERS):
        return "yes"

    if "/marketplace/" in url_lower and "log in" not in title_lower:
        return "no"
    if not html:
        return "unknown"
    return "unknown"


def _detect_challenge(final_url: str, title: str, html: str) -> Literal["yes", "no", "unknown"]:
    url_lower = (final_url or "").lower()
    title_lower = (title or "").lower()
    html_lower = (html or "").lower()

    # Login wall is not a checkpoint — see login_required.
    if any(frag in url_lower for frag in LOGIN_URL_FRAGMENTS):
        return "no"

    if any(frag in url_lower for frag in CHECKPOINT_URL_FRAGMENTS):
        return "yes"
    if any(m in title_lower or m in html_lower for m in CHECKPOINT_TEXT_MARKERS):
        return "yes"
    if "captcha" in html_lower and "marketplace/item" not in html_lower:
        return "yes"

    if "/marketplace/" in url_lower:
        return "no"
    if not html:
        return "unknown"
    return "unknown"


def _marketplace_content_present(html: str, final_url: str) -> bool:
    combined = f"{html} {final_url}".lower()
    return any(m in combined for m in MARKETPLACE_CONTENT_MARKERS)


def _count_candidate_elements(soup: BeautifulSoup) -> int:
    best = 0
    for sel in LISTING_CARD_SELECTORS:
        try:
            if ":contains" in sel:
                continue
            n = len(soup.select(sel))
            if n > best:
                best = n
        except Exception:
            continue
    try:
        item_links = soup.select('a[href*="/marketplace/item/"]')
        if len(item_links) > best:
            best = len(item_links)
    except Exception:
        pass
    return best


def _price_like(text: Optional[str]) -> bool:
    if not text:
        return False
    return bool(re.search(r"\$\s*[\d,]+", text))


def _extract_price_from_text(text: str) -> Optional[str]:
    match = re.search(r"\$\s*[\d,]+(?:\.\d{2})?", text)
    return match.group(0).strip() if match else None


def _find_card_for_link(link_el, soup: BeautifulSoup):
    """Walk up from item link to a reasonable listing container."""
    parent = link_el.parent
    for _ in range(8):
        if parent is None or parent.name in ("body", "html", "[document]"):
            break
        if parent.name in ("div", "li", "article", "span") and parent.get("role") == "article":
            return parent
        spans = parent.find_all("span", limit=20) if hasattr(parent, "find_all") else []
        if len(spans) >= 2:
            return parent
        parent = getattr(parent, "parent", None)
    return link_el.parent


def _extract_listings_from_html(html: str, limit: int) -> tuple[list[CandidateListing], int]:
    soup = BeautifulSoup(html, "lxml")
    candidate_count = _count_candidate_elements(soup)

    seen_links: set[str] = set()
    results: list[CandidateListing] = []

    try:
        item_links = soup.select('a[href*="/marketplace/item/"]')
    except Exception:
        item_links = []

    for link_el in item_links:
        if len(results) >= limit:
            break
        try:
            href = link_el.get("href")
            link = normalize_marketplace_link(href)
            if not link or link in seen_links:
                continue
            seen_links.add(link)

            card = _find_card_for_link(link_el, soup)
            card_text = ""
            if card is not None:
                try:
                    card_text = card.get_text(" ", strip=True)
                except Exception:
                    card_text = ""

            title = _safe_text(link_el)
            if title and len(title) < 3:
                title = None
            if not title and card is not None:
                for span in card.find_all("span", limit=15):
                    t = _safe_text(span)
                    if t and len(t) > 5 and not _price_like(t) and "mi away" not in t.lower():
                        title = t
                        break

            price = _extract_price_from_text(card_text) if card_text else None
            if not price and card is not None:
                for span in card.find_all("span", limit=20):
                    t = _safe_text(span)
                    if _price_like(t):
                        price = _extract_price_from_text(t or "")
                        break

            location = None
            if card_text:
                loc_match = re.search(
                    r"([A-Za-z][A-Za-z\s.'-]+,\s*[A-Z]{2})\b",
                    card_text,
                )
                if loc_match:
                    location = loc_match.group(1).strip()
                else:
                    away_match = re.search(r"(\d+(?:\.\d+)?\s*(?:mi|km)\s+away)", card_text, re.I)
                    if away_match:
                        location = away_match.group(1).strip()

            if not location and card is not None:
                for span in card.find_all("span", limit=20):
                    t = _safe_text(span)
                    if not t:
                        continue
                    if re.search(r"\b(?:mi|km)\s+away\b", t, re.I) or re.search(
                        r",\s*[A-Z]{2}\b", t
                    ):
                        location = t
                        break

            results.append(
                CandidateListing(
                    title=title,
                    price=price,
                    location=location,
                    link=link,
                )
            )
        except Exception:
            continue

    return results, candidate_count


def _scroll_page(page: Page, scroll_count: int, pause_ms: int) -> None:
    for i in range(scroll_count):
        try:
            page.evaluate(
                """() => {
                    const h = document.body ? document.body.scrollHeight : 0;
                    window.scrollTo(0, Math.min(h, (window.scrollY || 0) + window.innerHeight * 0.85));
                }"""
            )
            page.wait_for_timeout(pause_ms)
        except Exception:
            break


def _save_screenshot(page: Page, query: str, reason: str) -> Optional[Path]:
    try:
        ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
        safe_query = re.sub(r"[^\w\-]+", "_", query)[:40]
        path = ARTIFACTS_DIR / f"fb_marketplace_{safe_query}_{_timestamp()}_{reason}.png"
        page.screenshot(path=str(path), full_page=True)
        print(f"  Screenshot saved: {path}")
        return path
    except Exception as exc:
        print(f"  Screenshot failed: {exc}")
        return None


def _extraction_quality(listings: list[CandidateListing]) -> Literal["none", "partial", "good"]:
    if not listings:
        return "none"
    with_title = sum(1 for x in listings if x.title)
    with_price = sum(1 for x in listings if x.price)
    with_link = sum(1 for x in listings if x.link)
    if with_title >= 3 and with_price >= 2 and with_link >= 3:
        return "good"
    if with_link >= 1 or with_title >= 1:
        return "partial"
    return "none"


def _recommended_next_step(
    reachable: bool,
    login_required: str,
    challenge_detected: str,
    extraction_quality: str,
    headed: bool,
) -> str:
    if not reachable:
        return "retry headed"
    if login_required == "yes":
        return "try browser profile"
    if challenge_detected == "yes":
        return "retry headed"
    if extraction_quality == "good":
        return "build experimental adapter"
    if extraction_quality == "partial":
        return "build experimental adapter"
    if not headed:
        return "retry headed"
    return "abandon"


def _print_query_diagnostics(d: QueryDiagnostics) -> None:
    sep = "=" * 72
    print()
    print(sep)
    print(f"  FACEBOOK MARKETPLACE PROBE   query={d.query!r}")
    print(sep)
    print(f"  Query                  : {d.query}")
    print(f"  Requested URL          : {d.requested_url}")
    print(f"  Final URL              : {d.final_url}")
    print(f"  Page title             : {d.page_title!r}")
    print(f"  HTTP status            : {d.http_status}")
    print(f"  HTML length            : {d.html_length:,} bytes")
    print(f"  Load time              : {d.load_ms} ms")
    print(f"  Login required         : {d.login_required}")
    print(f"  Challenge detected     : {d.challenge_detected}")
    print(f"  Marketplace content    : {'yes' if d.marketplace_content else 'no'}")
    print(f"  Candidate elements     : ~{d.candidate_elements}")
    if d.nav_error:
        print(f"  Navigation error       : {d.nav_error}")

    print()
    print(f"--- Extracted listings (up to {len(d.listings)}) ---")
    if not d.listings:
        print("  (none)")
    else:
        for i, listing in enumerate(d.listings, 1):
            print(f"\n  [{i}]")
            for k, v in asdict(listing).items():
                print(f"    {k}: {v!r}")


# ---------------------------------------------------------------------------
# Probe run
# ---------------------------------------------------------------------------


def probe_query(
    page: Page,
    query: str,
    limit: int,
    timeout_ms: int,
    screenshot_on_fail: bool,
) -> QueryDiagnostics:
    requested_url = build_search_url(query)
    t0 = time.monotonic()
    http_status: Optional[int] = None
    nav_error: Optional[str] = None
    html = ""
    final_url = requested_url
    page_title = ""

    try:
        response = page.goto(
            requested_url,
            wait_until="domcontentloaded",
            timeout=timeout_ms,
        )
        http_status = response.status if response else None
        final_url = page.url
        page_title = page.title() or ""
    except PlaywrightTimeout as exc:
        nav_error = f"navigation_timeout: {exc}"
    except Exception as exc:
        nav_error = f"navigation_error: {exc}"

    if not nav_error:
        try:
            page.wait_for_timeout(INITIAL_LOAD_MS)
            _scroll_page(page, SCROLL_COUNT, SCROLL_PAUSE_MS)
            page.wait_for_timeout(SCROLL_PAUSE_MS)
            html = page.content()
            final_url = page.url
            page_title = page.title() or ""
        except Exception as exc:
            nav_error = nav_error or f"content_error: {exc}"
            try:
                html = page.content()
            except Exception:
                html = ""

    load_ms = int((time.monotonic() - t0) * 1000)
    html_length = len(html)

    login_required = _detect_login(final_url, page_title, html)
    challenge_detected = _detect_challenge(final_url, page_title, html)
    marketplace_content = _marketplace_content_present(html, final_url)

    listings: list[CandidateListing] = []
    candidate_elements = 0
    if html:
        try:
            listings, candidate_elements = _extract_listings_from_html(html, limit)
        except Exception:
            listings = []
            candidate_elements = 0

    diag = QueryDiagnostics(
        query=query,
        requested_url=requested_url,
        final_url=final_url,
        page_title=page_title,
        http_status=http_status,
        html_length=html_length,
        load_ms=load_ms,
        login_required=login_required,
        challenge_detected=challenge_detected,
        marketplace_content=marketplace_content,
        candidate_elements=candidate_elements,
        listings=listings,
        nav_error=nav_error,
    )

    _print_query_diagnostics(diag)

    should_screenshot = screenshot_on_fail and (
        nav_error
        or login_required == "yes"
        or challenge_detected == "yes"
        or not marketplace_content
        or len(listings) == 0
    )
    if should_screenshot:
        reason = "fail" if nav_error else "diagnostic"
        _save_screenshot(page, query, reason)

    return diag


def run_probe(
    queries: list[str],
    limit: int,
    headed: bool,
    slowmo: int,
    timeout_ms: int,
    screenshot_on_fail: bool,
) -> list[QueryDiagnostics]:
    sep = "=" * 72
    print(sep)
    print("  FACEBOOK MARKETPLACE PLAYWRIGHT PROBE (experimental, isolated)")
    print(f"  queries={queries}")
    print(
        f"  mode={'headed' if headed else 'headless'}  limit={limit}  "
        f"slowmo={slowmo}  timeout_ms={timeout_ms}  screenshot_on_fail={screenshot_on_fail}"
    )
    print(sep)

    if headed:
        print("\n  NOTE: --headed requires a display. On headless servers try:")
        print("        xvfb-run python experiments/adapters/facebook_marketplace_probe.py ... --headed\n")

    results: list[QueryDiagnostics] = []

    try:
        with sync_playwright() as pw:
            launch_opts: dict[str, Any] = {
                "headless": not headed,
                "slow_mo": slowmo,
                "args": [
                    "--disable-blink-features=AutomationControlled",
                    "--disable-dev-shm-usage",
                    "--no-sandbox",
                    "--disable-infobars",
                ],
            }
            try:
                browser = pw.chromium.launch(**launch_opts)
            except Exception as exc:
                print(f"\nERROR: Browser launch failed: {exc}")
                print("Install browsers: python -m playwright install chromium")
                sys.exit(1)

            context = browser.new_context(
                viewport=VIEWPORT,
                user_agent=USER_AGENT,
                locale=LOCALE,
                timezone_id=TIMEZONE,
                extra_http_headers={
                    "Accept-Language": "en-US,en;q=0.9",
                    "Accept-Encoding": "gzip, deflate, br",
                },
            )
            context.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
            )
            page = context.new_page()

            for query in queries:
                results.append(
                    probe_query(
                        page=page,
                        query=query,
                        limit=limit,
                        timeout_ms=timeout_ms,
                        screenshot_on_fail=screenshot_on_fail,
                    )
                )
                if query != queries[-1]:
                    try:
                        page.wait_for_timeout(1_500)
                    except Exception:
                        pass

            context.close()
            browser.close()
    except Exception as exc:
        print(f"\nFATAL: Probe failed: {exc}")
        sys.exit(1)

    return results


def _print_summary(results: list[QueryDiagnostics], headed: bool) -> None:
    total_listings = sum(len(r.listings) for r in results)
    reachable = any(
        r.http_status == 200 or (r.html_length > 5_000 and not r.nav_error) for r in results
    )
    login_vals = {r.login_required for r in results}
    challenge_vals = {r.challenge_detected for r in results}

    if login_vals == {"yes"}:
        login_required: Literal["yes", "no", "unknown"] = "yes"
    elif login_vals <= {"no"}:
        login_required = "no"
    else:
        login_required = "unknown"

    if challenge_vals == {"yes"}:
        challenge_detected: Literal["yes", "no", "unknown"] = "yes"
    elif challenge_vals <= {"no"}:
        challenge_detected = "no"
    else:
        challenge_detected = "unknown"

    qualities = [_extraction_quality(r.listings) for r in results]
    if "good" in qualities:
        extraction_quality: Literal["none", "partial", "good"] = "good"
    elif "partial" in qualities:
        extraction_quality = "partial"
    else:
        extraction_quality = "none"

    recommended = _recommended_next_step(
        reachable=reachable,
        login_required=login_required,
        challenge_detected=challenge_detected,
        extraction_quality=extraction_quality,
        headed=headed,
    )

    print()
    print("=" * 72)
    print("FACEBOOK MARKETPLACE PROBE SUMMARY")
    print(f"- reachable: {'yes' if reachable else 'no'}")
    print(f"- login_required: {login_required}")
    print(f"- challenge_detected: {challenge_detected}")
    print(f"- listings_found: {total_listings}")
    print(f"- extraction_quality: {extraction_quality}")
    print(f"- recommended_next_step: {recommended}")
    print("=" * 72)
    print()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Facebook Marketplace Playwright recon probe (experiments only)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python experiments/adapters/facebook_marketplace_probe.py
  python experiments/adapters/facebook_marketplace_probe.py --query "rtx 3080" --limit 10
  python experiments/adapters/facebook_marketplace_probe.py --query "rtx 3080" --headed --slowmo 100
""",
    )
    p.add_argument(
        "--query",
        action="append",
        dest="queries",
        metavar="TEXT",
        help="Search term (repeatable). Default: rtx 3080, toyota sequoia, 75 inch tv",
    )
    p.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_LIMIT,
        help=f"Max listings to extract per query (default: {DEFAULT_LIMIT})",
    )
    p.add_argument(
        "--headed",
        action="store_true",
        help="Run visible browser (use xvfb-run on headless servers)",
    )
    p.add_argument(
        "--slowmo",
        type=int,
        default=0,
        metavar="MS",
        help="Playwright slow_mo delay in milliseconds (default: 0)",
    )
    p.add_argument(
        "--timeout-ms",
        type=int,
        default=DEFAULT_TIMEOUT_MS,
        dest="timeout_ms",
        help=f"Navigation timeout in ms (default: {DEFAULT_TIMEOUT_MS})",
    )
    p.add_argument(
        "--screenshot-on-fail",
        action="store_true",
        dest="screenshot_on_fail",
        help=f"Save PNG under {ARTIFACTS_DIR}/ on failure or empty extraction",
    )
    return p


def main() -> None:
    args = _build_parser().parse_args()
    queries = args.queries if args.queries else DEFAULT_QUERIES
    results = run_probe(
        queries=queries,
        limit=args.limit,
        headed=args.headed,
        slowmo=args.slowmo,
        timeout_ms=args.timeout_ms,
        screenshot_on_fail=args.screenshot_on_fail,
    )
    _print_summary(results, headed=args.headed)
    sys.exit(0)


if __name__ == "__main__":
    main()
