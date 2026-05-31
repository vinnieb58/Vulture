"""
Best Buy Playwright reconnaissance probe
=========================================
Reconnaissance only. Does NOT write to SQLite, send Discord alerts,
register in adapters/registry.py, or touch .env.

Raven testing (2026-05):
  - DNS/ping OK
  - curl HTTP/2: immediate error, 0 bytes
  - curl --http1.1: 30s stall, 0 bytes
  - requests probe: ~25s timeout, 0 bytes
  - Playwright Chromium: HTTP 200, ~1.6 MB HTML, ~17s

Verdict from Raven: browser automation required; plain HTTP clients fail.

Usage:
    python experiments/adapters/bestbuy_playwright_probe.py
    python experiments/adapters/bestbuy_playwright_probe.py "rtx 4070"
    python experiments/adapters/bestbuy_playwright_probe.py "macbook air" "gaming laptop"
    python experiments/adapters/bestbuy_playwright_probe.py --limit 10 --save-html
    python experiments/adapters/bestbuy_playwright_probe.py "rtx 4070" --headed

Flags:
    --headed     Visible browser (requires display; on Raven use xvfb-run)
    --save-html  Write rendered HTML to experiments/debug/bestbuy/ (gitignored)
    --limit N    Max products printed per query (default: 5)
    --slow       Extra settle time after cards appear (debugging)

Prerequisites on Raven:
    pip install playwright beautifulsoup4 lxml
    python -m playwright install chromium
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote_plus, urljoin

from bs4 import BeautifulSoup

try:
    from playwright.sync_api import (
        Browser,
        BrowserContext,
        Page,
        TimeoutError as PlaywrightTimeout,
        sync_playwright,
    )
except ImportError:
    print("ERROR: playwright is not installed.")
    print("Install with:  pip install playwright && python -m playwright install chromium")
    sys.exit(1)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)-8s %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("bestbuy_pw_probe")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

BESTBUY_ORIGIN = "https://www.bestbuy.com"
BESTBUY_SEARCH_BASE = f"{BESTBUY_ORIGIN}/site/searchpage.jsp"
DEFAULT_QUERIES = ["rtx 4070", "macbook air", "gaming laptop"]

DEBUG_DIR = Path("experiments/debug/bestbuy")

VIEWPORT = {"width": 1440, "height": 900}
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
LOCALE = "en-US"
TIMEZONE = "America/Chicago"

NAV_TIMEOUT_MS = 90_000
CARD_WAIT_MS = 20_000
SETTLE_MS = 2_000
SLOW_SETTLE_MS = 5_000

DEFAULT_LIMIT = 5

CHALLENGE_TITLE_FRAGMENTS = [
    "just a moment",
    "attention required",
    "access denied",
    "checking your browser",
    "security check",
    "verify you are human",
    "robot or human",
    "please verify",
]

CHALLENGE_BODY_MARKERS = [
    "cf-browser-verification",
    "cdn-cgi/challenge-platform",
    "Enable JavaScript and cookies to continue",
    "Checking your browser before accessing",
    "akamai-bm-telemetry",
    "_akamai_edgescape",
    "px-captcha",
    "PerimeterX",
    "datadome",
    "prove you are human",
    "human verification",
    "unusual traffic",
    "automated queries",
    "bot detection",
    "g-recaptcha",
    "hcaptcha",
]

# Best Buy PLP (May 2026) uses two card layouts:
#   Grid/list hybrid: `.list-item` + `a.sku-title` + `span.nc-product-title`
#   Product-list:     `li.product-list-item` + `a.product-list-item-link`
# Legacy `li.sku-item` may appear on older layouts.
PRODUCT_CARD_SELECTORS = [
    ".list-item",
    "li.product-list-item",
    "li.sku-item",
    ".sku-item",
    "section.sku-item-list li",
    "[data-testid='product-card']",
    ".shop-sku-list-item",
    "div[class*='sku-item']",
    "li[class*='sku-item']",
]

CARD_WAIT_SELECTORS = [
    ".list-item",
    "li.product-list-item",
    "a.product-list-item-link",
    "li.sku-item",
    ".sku-item",
    "a.sku-title",
    "[data-testid='product-card']",
]

FIELD_SELECTORS = {
    "title": [
        "a.sku-title span.nc-product-title",
        "a.sku-title",
        "a.product-list-item-link",
        "span.nc-product-title",
        ".sku-title a",
        ".sku-title",
        "h4.sku-title",
        "[data-testid='product-title']",
        "a[data-track='Product Title']",
    ],
    "price": [
        "span.font-500",
        ".priceView-customer-price span",
        ".priceView-hero-price span",
        "[data-testid='customer-price']",
        ".pricing-price__regular-price",
        "div[data-testid='price']",
        ".priceView-price",
        "[class*='priceView'] span",
    ],
    "link": [
        "a.sku-title",
        "a.product-list-item-link",
        ".sku-title a",
        "h4.sku-title a",
        "a.image-link",
        "a[href*='/product/']",
        "a[href*='/site/']",
    ],
    "availability": [
        ".fulfillment-add-to-cart-button",
        ".availability-text",
        "[data-testid='availability-message']",
        ".fulfillment-fulfillment-summary",
        ".c-button-add",
        "button[data-button-state]",
    ],
    "pickup": [
        "[class*='pickup']",
        "[data-testid*='pickup']",
        ".fulfillment-pickup",
        "[class*='fulfillment']",
    ],
}


@dataclass
class ProductCandidate:
    title: Optional[str]
    price: Optional[str]
    price_parsed: Optional[int]
    link: Optional[str]
    availability: Optional[str]
    store_pickup: Optional[str]
    raw_snippet: Optional[str]


@dataclass
class QueryResult:
    query: str
    search_url: str
    final_url: str
    http_status: Optional[int]
    page_title: str
    html_length: int
    load_ms: int
    challenge: dict[str, list[str]]
    selector_counts: dict[str, int]
    cards_selector: Optional[str]
    card_count: int
    extracted_count: int
    candidates: list[ProductCandidate]
    nav_error: Optional[str]
    redirected_off_search: bool
    empty_shell: bool


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")


def build_search_url(query: str) -> str:
    return f"{BESTBUY_SEARCH_BASE}?st={quote_plus(query)}"


def parse_price_int(raw: Optional[str]) -> Optional[int]:
    if not raw:
        return None
    match = re.search(r"\$?([\d,]+)(?:\.\d+)?", str(raw).replace(",", ""))
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
        return urljoin(BESTBUY_ORIGIN, href)
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
            href = normalize_link(found["href"])
            if href and "/product/" in href:
                return href
    return None


def _detect_challenge(title: str, html: str) -> dict[str, list[str]]:
    hits: dict[str, list[str]] = {}
    title_lower = title.lower()
    html_lower = html.lower()

    for fragment in CHALLENGE_TITLE_FRAGMENTS:
        if fragment in title_lower:
            hits.setdefault("challenge_title", []).append(fragment)

    for marker in CHALLENGE_BODY_MARKERS:
        if marker.lower() in html_lower:
            platform = (
                "cloudflare"
                if "cf-" in marker.lower() or "cdn-cgi" in marker.lower()
                else "akamai"
                if "akamai" in marker.lower()
                else "perimeterx"
                if "px" in marker.lower() or "perimeterx" in marker.lower()
                else "datadome"
                if "datadome" in marker.lower()
                else "captcha"
                if "captcha" in marker.lower()
                else "generic"
            )
            hits.setdefault(platform, []).append(marker)

    return hits


def _count_selectors(soup: BeautifulSoup) -> dict[str, int]:
    counts: dict[str, int] = {}
    for sel in PRODUCT_CARD_SELECTORS + ["a[href*='/site/']"]:
        try:
            n = len(soup.select(sel))
            if n:
                counts[sel] = n
        except Exception:
            pass
    return counts


def _find_product_cards(soup: BeautifulSoup) -> tuple[list, Optional[str]]:
    for selector in PRODUCT_CARD_SELECTORS:
        try:
            cards = soup.select(selector)
            if cards:
                return cards, selector
        except Exception:
            continue
    return [], None


def _price_from_card_text(card) -> Optional[str]:
    """Fallback: first $NNN price-like token in card text."""
    for el in card.find_all(["span", "div"]):
        text = el.get_text(strip=True)
        if re.match(r"^\$[\d,]+(?:\.\d{2})?$", text):
            return text
    match = re.search(r"\$[\d,]+(?:\.\d{2})?", card.get_text(" ", strip=True))
    return match.group(0) if match else None


def _extract_card(card, limit_snippet: int = 200) -> Optional[ProductCandidate]:
    title = _select_one_text(card, FIELD_SELECTORS["title"])
    price = _select_one_text(card, FIELD_SELECTORS["price"])
    if not price:
        price = _price_from_card_text(card)
    link = _select_one_href(card, FIELD_SELECTORS["link"])
    availability = _select_one_text(card, FIELD_SELECTORS["availability"])
    pickup = _select_one_text(card, FIELD_SELECTORS["pickup"])

    if not title and not link:
        return None

    raw = card.get_text(" ", strip=True)
    snippet = raw[:limit_snippet] + ("..." if len(raw) > limit_snippet else "")

    return ProductCandidate(
        title=title,
        price=price,
        price_parsed=parse_price_int(price),
        link=link,
        availability=availability,
        store_pickup=pickup,
        raw_snippet=snippet or None,
    )


def _extract_from_html(html: str, limit: int) -> tuple[list[ProductCandidate], Optional[str], int]:
    soup = BeautifulSoup(html, "lxml")
    cards, selector = _find_product_cards(soup)
    if not cards:
        return [], None, 0

    results: list[ProductCandidate] = []
    for card in cards:
        candidate = _extract_card(card)
        if candidate:
            results.append(candidate)
        if len(results) >= limit:
            break

    return results, selector, len(cards)


def _add_stealth_scripts(page: Page) -> None:
    page.add_init_script("""
        Object.defineProperty(navigator, 'webdriver', {
            get: () => undefined,
        });
        Object.defineProperty(navigator, 'plugins', {
            get: () => [
                { name: 'Chrome PDF Plugin', filename: 'internal-pdf-viewer' },
                { name: 'Chrome PDF Viewer', filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai' },
                { name: 'Native Client', filename: 'internal-nacl-plugin' },
            ],
        });
        Object.defineProperty(navigator, 'languages', {
            get: () => ['en-US', 'en'],
        });
        window.chrome = {
            runtime: {},
            loadTimes: function() {},
            csi: function() {},
            app: {},
        };
    """)


def _save_html(html: str, query: str, ts: str) -> Path:
    DEBUG_DIR.mkdir(parents=True, exist_ok=True)
    safe_query = re.sub(r"[^\w\-]", "_", query)[:40]
    path = DEBUG_DIR / f"bestbuy_{safe_query}_{ts}.html"
    path.write_text(html, encoding="utf-8")
    log.info("HTML saved to: %s  (%d bytes)", path, len(html.encode("utf-8")))
    return path


def _fields_extractable(candidates: list[ProductCandidate]) -> dict[str, bool]:
    if not candidates:
        return {"title": False, "price": False, "link": False}
    return {
        "title": any(c.title for c in candidates),
        "price": any(c.price or c.price_parsed for c in candidates),
        "link": any(c.link for c in candidates),
    }


# ---------------------------------------------------------------------------
# Per-query probe (uses shared page)
# ---------------------------------------------------------------------------


def probe_query_on_page(
    page: Page,
    query: str,
    limit: int,
    slow: bool,
    save_html: bool,
    ts: str,
) -> QueryResult:
    search_url = build_search_url(query)
    sep = "=" * 72

    print()
    print(sep)
    print(f"  BEST BUY PLAYWRIGHT PROBE   query={query!r}")
    print(sep)
    print(f"  Search URL : {search_url}")

    t0 = time.monotonic()
    http_status: Optional[int] = None
    nav_error: Optional[str] = None
    html = ""
    final_url = search_url
    page_title = ""

    try:
        resp = page.goto(
            search_url,
            wait_until="domcontentloaded",
            timeout=NAV_TIMEOUT_MS,
        )
        http_status = resp.status if resp else None
        final_url = page.url
        page_title = page.title()
        print(f"  Navigation OK  HTTP={http_status}  title={page_title!r}")
    except PlaywrightTimeout:
        nav_error = "navigation_timeout"
        print(f"  Navigation timed out after {NAV_TIMEOUT_MS} ms")
    except Exception as exc:
        nav_error = str(exc)
        err_lower = nav_error.lower()
        if "http2" in err_lower:
            print("  ERR_HTTP2 — edge reset (try --headed or Raven residential IP)")
        print(f"  Navigation error: {str(exc)[:200]}")

    load_ms = int((time.monotonic() - t0) * 1000)

    if not nav_error:
        # Nudge lazy-loaded result rows (macbook air product-list layout).
        try:
            page.evaluate("window.scrollTo(0, document.body.scrollHeight / 3)")
            page.wait_for_timeout(800)
            page.evaluate("window.scrollTo(0, 0)")
        except Exception:
            pass

        card_appeared = False
        for sel in CARD_WAIT_SELECTORS:
            try:
                page.wait_for_selector(sel, timeout=CARD_WAIT_MS)
                card_appeared = True
                print(f"  Selector appeared: {sel!r}")
                break
            except PlaywrightTimeout:
                log.debug("Selector %r not found within %d ms", sel, CARD_WAIT_MS)

        if not card_appeared:
            print(f"  No product selector within {CARD_WAIT_MS} ms — continuing with DOM parse")

        settle = SLOW_SETTLE_MS if slow else SETTLE_MS
        page.wait_for_timeout(settle)

        try:
            html = page.content()
            final_url = page.url
            page_title = page.title()
        except Exception as exc:
            nav_error = nav_error or f"content_error: {exc}"
            html = ""

    load_ms = int((time.monotonic() - t0) * 1000)
    html_length = len(html)

    challenge = _detect_challenge(page_title, html) if html else {}
    soup = BeautifulSoup(html, "lxml") if html else BeautifulSoup("", "lxml")
    body_text = soup.body.get_text(" ", strip=True) if soup.body else ""
    body_words = len(body_text.split())

    selector_counts = _count_selectors(soup) if html else {}
    candidates, cards_selector, card_count = _extract_from_html(html, limit) if html else ([], None, 0)

    redirected_off_search = (
        "bestbuy.com" not in final_url.lower()
        or ("searchpage.jsp" not in final_url.lower() and "bestbuy.com/site/" in final_url.lower())
    )
    empty_shell = html_length > 0 and card_count == 0 and body_words < 150

    print()
    print("--- Results ---")
    print(f"  Final URL      : {final_url}")
    print(f"  HTTP status    : {http_status}")
    print(f"  Page title     : {page_title!r}")
    print(f"  Body length    : {html_length:,} chars")
    print(f"  Body words     : {body_words}")
    print(f"  Load time      : {load_ms} ms")

    if challenge:
        print(f"  Blocking/challenge: {challenge}")
    else:
        print("  Blocking/challenge: none detected")

    if redirected_off_search:
        print("  Redirect warning : left Best Buy search results URL")

    if empty_shell:
        print("  Empty shell      : yes (thin body, no product cards)")

    print(f"  Selector counts  : {selector_counts or '(none matched)'}")
    print(f"  Cards selector   : {cards_selector}")
    print(f"  Card count       : {card_count}")
    print(f"  Extracted count  : {len(candidates)}")

    fields = _fields_extractable(candidates)
    print(f"  Fields OK        : title={fields['title']} price={fields['price']} link={fields['link']}")

    print()
    print(f"--- Rough candidates (up to {limit}) ---")
    if not candidates:
        print("  (none)")
    else:
        for i, c in enumerate(candidates, 1):
            print(f"\n  [{i}]")
            for k, v in asdict(c).items():
                print(f"    {k}: {v!r}")

    if save_html and html:
        path = _save_html(html, query, ts)
        print(f"\n  Debug HTML -> {path}")

    return QueryResult(
        query=query,
        search_url=search_url,
        final_url=final_url,
        http_status=http_status,
        page_title=page_title,
        html_length=html_length,
        load_ms=load_ms,
        challenge=challenge,
        selector_counts=selector_counts,
        cards_selector=cards_selector,
        card_count=card_count,
        extracted_count=len(candidates),
        candidates=candidates,
        nav_error=nav_error,
        redirected_off_search=redirected_off_search,
        empty_shell=empty_shell,
    )


# ---------------------------------------------------------------------------
# Overall assessment
# ---------------------------------------------------------------------------


def _print_overall_assessment(results: list[QueryResult]) -> None:
    sep = "=" * 72
    print()
    print(sep)
    print("  OVERALL ASSESSMENT")
    print(sep)

    loaded_real_html = [r for r in results if r.html_length > 50_000 and not r.nav_error]
    found_cards = [r for r in results if r.card_count > 0]
    extracted_any = [r for r in results if r.extracted_count > 0]
    title_ok = [r for r in results if _fields_extractable(r.candidates)["title"]]
    price_ok = [r for r in results if _fields_extractable(r.candidates)["price"]]
    link_ok = [r for r in results if _fields_extractable(r.candidates)["link"]]
    full_triple = [
        r for r in results
        if all(_fields_extractable(r.candidates).values())
    ]
    challenged = [r for r in results if r.challenge]
    nav_failed = [r for r in results if r.nav_error]

    print(f"  Queries probed              : {len(results)}")
    print(f"  Chromium loaded real HTML   : {len(loaded_real_html)}/{len(results)}")
    print(f"  Product cards found         : {len(found_cards)}/{len(results)}")
    print(f"  Candidates extracted        : {len(extracted_any)}/{len(results)}")
    print(f"  Title extractable           : {len(title_ok)}/{len(results)}")
    print(f"  Price extractable           : {len(price_ok)}/{len(results)}")
    print(f"  Link extractable            : {len(link_ok)}/{len(results)}")
    print(f"  Title+price+link (all three): {len(full_triple)}/{len(results)}")
    print(f"  Challenge signals           : {len(challenged)}/{len(results)}")
    print(f"  Navigation failures         : {len(nav_failed)}/{len(results)}")

    print()
    print("  Per-query:")
    for r in results:
        f = _fields_extractable(r.candidates)
        print(
            f"    {r.query!r}: status={r.http_status} html={r.html_length:,}B "
            f"cards={r.card_count} extracted={r.extracted_count} "
            f"title={f['title']} price={f['price']} link={f['link']} "
            f"load={r.load_ms}ms"
        )

    consistent = (
        len(results) >= 1
        and len(loaded_real_html) == len(results)
        and len(found_cards) == len(results)
        and len(full_triple) == len(results)
        and not nav_failed
    )

    requests_viable = False  # documented from Raven + cloud probes

    print()
    print(f"  Plain requests viable       : {requests_viable}")
    print(f"  Playwright loads HTML       : {len(loaded_real_html) > 0}")
    print(f"  All queries consistent      : {consistent}")
    print(f"  Should remain probe-only    : True")

    print()
    if consistent:
        rec = (
            "PROMISING — Playwright extracts title+price+link on all default queries. "
            "Next: run on Raven across multiple days, then sketch experimental "
            "adapters/bestbuy.py behind a flag. Do NOT register in registry yet."
        )
    elif len(loaded_real_html) > 0 and len(title_ok) > 0:
        rec = (
            "PARTIAL — Chromium loads pages and some fields extract, but not consistently "
            "across all queries. Refine selectors (--save-html), re-run on Raven, "
            "stay probe-only."
        )
    elif len(loaded_real_html) > 0:
        rec = (
            "HTML OK, PARSER WEAK — Browser bypasses Akamai but product selectors need work. "
            "Use --save-html on Raven, inspect li.sku-item DOM, update FIELD_SELECTORS."
        )
    else:
        rec = (
            "BLOCKED OR UNSTABLE — Playwright did not load usable search HTML in this run. "
            "Re-run on Raven residential IP. Stay probe-only."
        )

    print("  RECOMMENDATION:")
    print(f"    {rec}")
    print(sep)
    print()


# ---------------------------------------------------------------------------
# Main run — one browser/context for entire probe
# ---------------------------------------------------------------------------


def run_probe(
    queries: list[str],
    headed: bool,
    save_html: bool,
    limit: int,
    slow: bool,
) -> None:
    ts = _timestamp()
    sep = "=" * 72

    print(sep)
    print("  BEST BUY PLAYWRIGHT PROBE")
    print(f"  queries={queries}")
    print(f"  mode={'headed' if headed else 'headless'}  limit={limit}  "
          f"save_html={save_html}  slow={slow}")
    print(sep)

    if headed:
        print("\n  NOTE: --headed requires a display. On Raven without one:")
        print("        xvfb-run python3 experiments/adapters/bestbuy_playwright_probe.py ... --headed\n")

    results: list[QueryResult] = []

    with sync_playwright() as pw:
        print("\n--- Launch browser (one instance for full run) ---")
        launch_opts: dict = {
            "headless": not headed,
            "slow_mo": 500 if slow else 0,
            "args": [
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
                "--no-sandbox",
                "--disable-gpu",
                "--disable-infobars",
                "--window-size=1440,900",
            ],
        }
        try:
            browser: Browser = pw.chromium.launch(**launch_opts)
        except Exception as exc:
            print(f"  Browser launch failed: {exc}")
            print("  On Raven: python -m playwright install chromium")
            sys.exit(1)

        print(f"  Chromium launched  headless={not headed}")

        ctx: BrowserContext = browser.new_context(
            viewport=VIEWPORT,
            user_agent=USER_AGENT,
            locale=LOCALE,
            timezone_id=TIMEZONE,
            java_script_enabled=True,
            bypass_csp=True,
            extra_http_headers={
                "Accept-Language": "en-US,en;q=0.9",
                "Accept-Encoding": "gzip, deflate, br",
                "DNT": "1",
                "Upgrade-Insecure-Requests": "1",
            },
        )

        page: Page = ctx.new_page()
        _add_stealth_scripts(page)

        for query in queries:
            result = probe_query_on_page(
                page=page,
                query=query,
                limit=limit,
                slow=slow,
                save_html=save_html,
                ts=ts,
            )
            results.append(result)
            if query != queries[-1]:
                page.wait_for_timeout(1_500)

        ctx.close()
        browser.close()
        print("\n--- Browser closed ---")

    _print_overall_assessment(results)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Best Buy Playwright reconnaissance probe (experiments only)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python experiments/adapters/bestbuy_playwright_probe.py
  python experiments/adapters/bestbuy_playwright_probe.py "rtx 4070" --limit 10
  python experiments/adapters/bestbuy_playwright_probe.py --save-html
  xvfb-run python3 experiments/adapters/bestbuy_playwright_probe.py --headed --slow
""",
    )
    p.add_argument(
        "queries",
        nargs="*",
        help="Search term(s). Defaults: rtx 4070, macbook air, gaming laptop",
    )
    p.add_argument(
        "--headed",
        action="store_true",
        help="Run visible browser (use xvfb-run on headless Raven)",
    )
    p.add_argument(
        "--save-html",
        action="store_true",
        dest="save_html",
        help=f"Save rendered HTML to {DEBUG_DIR}/",
    )
    p.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_LIMIT,
        help=f"Max products printed per query (default: {DEFAULT_LIMIT})",
    )
    p.add_argument(
        "--slow",
        action="store_true",
        help="Longer settle time after cards appear",
    )
    return p


def main() -> None:
    args = _build_parser().parse_args()
    queries = args.queries if args.queries else DEFAULT_QUERIES
    run_probe(
        queries=queries,
        headed=args.headed,
        save_html=args.save_html,
        limit=args.limit,
        slow=args.slow,
    )


if __name__ == "__main__":
    main()
