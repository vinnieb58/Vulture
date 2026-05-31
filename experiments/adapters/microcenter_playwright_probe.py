"""
Micro Center Playwright reconnaissance probe
==============================================
Reconnaissance only. Micro Center remains **probe-only** until this script
(and/or the requests probe) reliably returns stable normalized listing data
on a target host. Do NOT register adapters/microcenter.py or touch
adapters/registry.py until then.

Does NOT write to SQLite, send Discord alerts, touch .env, DB schema, or
production runtime (scheduler, Discord bot, hunt engine).

Complements experiments/adapters/microcenter_probe.py (requests + curl_cffi),
which confirmed Cloudflare 403 / "Just a moment..." with no product HTML.

Usage:
    python experiments/adapters/microcenter_playwright_probe.py
    python experiments/adapters/microcenter_playwright_probe.py --query "rtx 4070" --storeid 115
    python experiments/adapters/microcenter_playwright_probe.py --query "rtx 4070" --storeid 141 --limit 10
    python experiments/adapters/microcenter_playwright_probe.py --compare-stores
    python experiments/adapters/microcenter_playwright_probe.py --headful --debug-html /tmp/mc.html

Raven setup:
    python -m playwright install chromium
    xvfb-run python3 ... --headful   # if no display

Store comparison (Brooklyn vs Columbus):
    python experiments/adapters/microcenter_playwright_probe.py --compare-stores --query "rtx 4070"
    python experiments/adapters/microcenter_playwright_probe.py --compare-stores 115 141 --query "rtx 4070"
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlencode, urljoin

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

logging.basicConfig(
    level=logging.DEBUG,
    format="%(levelname)-8s %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("microcenter_pw_probe")

MICROCENTER_ORIGIN = "https://www.microcenter.com"
SEARCH_PATH = "/search/search_results.aspx"

VIEWPORT = {"width": 1440, "height": 900}
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
LOCALE = "en-US"
TIMEZONE = "America/New_York"

NAV_TIMEOUT_MS = 60_000
LISTING_WAIT_MS = 20_000
SETTLE_MS = 3_000
CF_CHALLENGE_WAIT_MS = 8_000

DEFAULT_QUERY = "rtx 4070"
DEFAULT_LIMIT = 10

STORE_COMPARE_IDS = {
    "115": "Brooklyn, NY",
    "141": "Columbus, OH",
}

CHALLENGE_TITLE_FRAGMENTS = [
    "just a moment",
    "attention required",
    "access denied",
    "checking your browser",
    "please wait",
    "security check",
    "verify you are human",
]

CHALLENGE_BODY_MARKERS = [
    "cf-browser-verification",
    "cdn-cgi/challenge-platform",
    "enable javascript and cookies",
    "checking your browser before accessing",
    "__cf_chl",
    "cf-challenge-running",
    "px-captcha",
    "datadome",
    "prove you are human",
    "unusual traffic",
    "access denied",
]

LISTING_SELECTORS = [
    "#productGrid li.product_wrapper",
    "#productGrid .product_wrapper",
    "li.product_wrapper",
    ".product_wrapper",
    "#productGrid li",
    "#productGrid a.productClick",
    "a.productClick",
    ".productClick",
    ".SearchResultProduct",
    ".search-result-product",
    "div[class*='SearchResult']",
    "tr.SearchResultProduct",
    "[data-product-id]",
    "article.product",
]

FIELD_SELECTORS = {
    "title": [
        "a.productClickItemV2[data-name]",
        "a[data-name]",
        "a.productClick",
        "a.SearchResultProductName",
        "img.SearchResultProductImage",
        "h2 a",
        ".productClick span",
        "[class*='productName']",
    ],
    "price": [
        "span[itemprop='price']",
        ".price_wrapper .price",
        ".price",
        "[class*='price']",
        "[data-price]",
    ],
    "link": [
        "a.productClick",
        "a[href*='/product/']",
        "h2 a[href]",
    ],
    "availability": [
        ".storePickup",
        ".instock",
        ".inventory",
        "[class*='inventory']",
        "[class*='pickup']",
        "[class*='InStock']",
        "[class*='availability']",
    ],
}

STORE_TEXT_MARKERS = [
    "my store",
    "current store",
    "your store",
    "store pickup",
    "in-store pickup",
    "select a store",
    "change store",
]


def build_search_url(query: str, store_id: Optional[str] = None) -> str:
    params: dict[str, str] = {"Ntt": query}
    if store_id:
        params["storeid"] = store_id
    return f"{MICROCENTER_ORIGIN}{SEARCH_PATH}?{urlencode(params)}"


def detect_challenge(title: str, html: str) -> dict[str, list[str]]:
    hits: dict[str, list[str]] = {}
    title_lower = (title or "").lower()
    html_lower = html.lower()

    for fragment in CHALLENGE_TITLE_FRAGMENTS:
        if fragment in title_lower:
            hits.setdefault("challenge_title", []).append(fragment)

    for marker in CHALLENGE_BODY_MARKERS:
        if marker.lower() in html_lower:
            platform = (
                "cloudflare"
                if any(x in marker.lower() for x in ("cf-", "cdn-cgi", "__cf"))
                else "generic"
            )
            hits.setdefault(platform, []).append(marker)

    return hits


def is_challenge_blocking(challenge: dict[str, list[str]], card_count: int, body_words: int) -> bool:
    if not challenge:
        return False
    if card_count > 0 and body_words > 200:
        return False
    return True


def _normalize_link(href: Optional[str]) -> Optional[str]:
    if not href:
        return None
    href = href.strip()
    if href.startswith("/"):
        return urljoin(MICROCENTER_ORIGIN, href)
    if href.startswith("http"):
        return href
    return urljoin(MICROCENTER_ORIGIN + "/", href)


def parse_price_int(raw: Optional[str]) -> Optional[int]:
    if not raw:
        return None
    m = re.search(r"\$?([\d,]+)", str(raw).replace(",", ""))
    if m:
        try:
            return int(m.group(1))
        except ValueError:
            return None
    return None


def normalize_candidate(raw: dict[str, Any], query: str, store_id: Optional[str]) -> dict[str, Any]:
    return {
        "source": "microcenter",
        "query": query,
        "storeid": store_id,
        "title": raw.get("title"),
        "price": parse_price_int(raw.get("price")) if raw.get("price") else None,
        "price_display": raw.get("price"),
        "link": raw.get("link"),
        "availability": raw.get("availability"),
        "card_preview": raw.get("card_preview"),
    }


def extract_store_snippet(html: str, store_id: Optional[str]) -> dict[str, Any]:
    soup = BeautifulSoup(html, "lxml")
    text = soup.get_text(" ", strip=True)
    text_lower = text.lower()

    found_markers = [m for m in STORE_TEXT_MARKERS if m in text_lower]
    store_name_hits: list[str] = []
    for _sid, label in STORE_COMPARE_IDS.items():
        city = label.split(",")[0].lower()
        if city in text_lower:
            store_name_hits.append(label)

    return {
        "storeid_param": store_id,
        "store_markers_in_body": found_markers,
        "store_name_hits": store_name_hits,
        "body_word_count": len(text.split()),
    }


def extract_candidates(
    html: str, query: str, store_id: Optional[str], limit: int
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    soup = BeautifulSoup(html, "lxml")
    meta: dict[str, Any] = {"selector_used": None, "card_count": 0, "selector_hits": {}}

    cards: list = []
    for selector in LISTING_SELECTORS:
        try:
            found = soup.select(selector)
        except Exception as exc:
            log.debug("Selector %r error: %s", selector, exc)
            continue
        if found:
            meta["selector_used"] = selector
            meta["card_count"] = len(found)
            cards = found
            log.info("Matched %r (%d nodes)", selector, len(found))
            break
        meta["selector_hits"][selector] = 0

    if not cards:
        for selector in LISTING_SELECTORS:
            try:
                n = len(soup.select(selector))
                if n:
                    meta["selector_hits"][selector] = n
            except Exception:
                pass
        return [], meta

    results: list[dict[str, Any]] = []
    for card in cards[:limit]:
        title = price = link = availability = None

        # Micro Center embeds structured fields on data-* attrs (title often not in visible text).
        data_anchor = card.select_one("a[data-name][href*='/product/']") or card.select_one(
            "a[data-name]"
        )
        if data_anchor:
            title = (data_anchor.get("data-name") or "").strip() or None
            link = _normalize_link(data_anchor.get("href"))
            raw_price = (data_anchor.get("data-price") or "").strip()
            if raw_price and raw_price not in ("0", "0.00"):
                price = f"${raw_price}" if not raw_price.startswith("$") else raw_price

        if not title:
            img = card.select_one("img.SearchResultProductImage, img[alt]")
            if img and img.get("alt"):
                title = img.get("alt", "").strip()

        for sel in FIELD_SELECTORS["title"]:
            if title:
                break
            el = card.select_one(sel)
            if el:
                title = (el.get("data-name") or el.get_text(" ", strip=True) or "").strip() or None
                if el.name == "a" and not link:
                    link = _normalize_link(el.get("href"))
                break

        for sel in FIELD_SELECTORS["link"]:
            if link:
                break
            el = card.select_one(sel)
            if el and el.get("href") and "/product/" in el.get("href", ""):
                link = _normalize_link(el.get("href"))
                if not title:
                    title = (el.get("data-name") or el.get_text(" ", strip=True) or "").strip() or None

        if not price:
            for el in card.select("[data-price]"):
                raw_price = (el.get("data-price") or "").strip()
                if raw_price and raw_price not in ("0", "0.00"):
                    price = f"${raw_price}" if not raw_price.startswith("$") else raw_price
                    break

        for sel in FIELD_SELECTORS["price"]:
            if price:
                break
            el = card.select_one(sel)
            if el:
                text = el.get_text(" ", strip=True)
                if text and "$" in text:
                    price = text
                    break

        pw = card.select_one(".price_wrapper")
        if pw:
            pw_text = pw.get_text(" ", strip=True)
            if pw_text:
                if any(
                    tok in pw_text.lower()
                    for tok in ("not carried", "out of stock", "pickup", "in stock", "ship")
                ):
                    availability = pw_text[:200]
                elif not price and "$" in pw_text:
                    m = re.search(r"\$[\d,]+(?:\.\d{2})?", pw_text)
                    if m:
                        price = m.group(0)

        for sel in FIELD_SELECTORS["availability"]:
            if availability:
                break
            el = card.select_one(sel)
            if el:
                availability = el.get_text(" ", strip=True)
                break

        if not link:
            el = card.select_one("a[href*='/product/']")
            if el:
                link = _normalize_link(el.get("href"))

        preview = card.get_text(" ", strip=True)
        if len(preview) > 240:
            preview = preview[:240] + "…"

        if title or link:
            results.append(
                normalize_candidate(
                    {
                        "title": title,
                        "price": price,
                        "link": link,
                        "availability": availability,
                        "card_preview": preview or None,
                    },
                    query,
                    store_id,
                )
            )

    return results, meta


def _add_stealth_scripts(page: Page) -> None:
    page.add_init_script("""
        Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
        Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
        window.chrome = { runtime: {} };
    """)


def _wait_for_products_or_challenge(page: Page) -> bool:
    for selector in LISTING_SELECTORS[:6]:
        try:
            page.wait_for_selector(selector, timeout=LISTING_WAIT_MS)
            log.info("Product selector appeared: %s", selector)
            return True
        except PlaywrightTimeout:
            continue
    page.wait_for_timeout(CF_CHALLENGE_WAIT_MS)
    return False


@dataclass
class RunSnapshot:
    query: str
    store_id: Optional[str]
    initial_url: str
    final_url: str = ""
    page_title: str = ""
    http_status: Optional[int] = None
    challenge: dict[str, list[str]] = field(default_factory=dict)
    challenge_blocking: bool = False
    selector_meta: dict[str, Any] = field(default_factory=dict)
    candidates: list[dict[str, Any]] = field(default_factory=list)
    store_snippet: dict[str, Any] = field(default_factory=dict)
    body_words: int = 0
    html_bytes: int = 0
    nav_error: Optional[str] = None


def run_probe(
    query: str,
    store_id: Optional[str],
    limit: int,
    headful: bool,
    debug_html: Optional[Path],
) -> RunSnapshot:
    initial_url = build_search_url(query, store_id)
    snap = RunSnapshot(query=query, store_id=store_id, initial_url=initial_url)

    try:
        with sync_playwright() as pw:
            browser = _launch_browser(pw, headful)
            ctx = _new_context(browser)
            page = ctx.new_page()
            _add_stealth_scripts(page)

            try:
                resp = page.goto(
                    initial_url,
                    wait_until="domcontentloaded",
                    timeout=NAV_TIMEOUT_MS,
                )
                snap.http_status = resp.status if resp else None
            except PlaywrightTimeout:
                snap.nav_error = "navigation_timeout"
            except Exception as exc:
                snap.nav_error = f"{type(exc).__name__}: {exc}"

            snap.final_url = page.url
            snap.page_title = page.title() or ""

            _wait_for_products_or_challenge(page)
            page.wait_for_timeout(SETTLE_MS)

            html = page.content()
            snap.html_bytes = len(html.encode("utf-8", errors="replace"))
            soup = BeautifulSoup(html, "lxml")
            body_text = soup.body.get_text(" ", strip=True) if soup.body else ""
            snap.body_words = len(body_text.split())

            snap.challenge = detect_challenge(snap.page_title, html)
            candidates, selector_meta = extract_candidates(html, query, store_id, limit)
            snap.selector_meta = selector_meta
            snap.candidates = candidates
            snap.store_snippet = extract_store_snippet(html, store_id)
            snap.challenge_blocking = is_challenge_blocking(
                snap.challenge,
                selector_meta.get("card_count", 0),
                snap.body_words,
            )

            if debug_html:
                debug_html.parent.mkdir(parents=True, exist_ok=True)
                debug_html.write_text(html, encoding="utf-8")
                log.info("Saved rendered HTML to %s (%d bytes)", debug_html, snap.html_bytes)

            browser.close()

    except Exception as exc:
        snap.nav_error = snap.nav_error or f"{type(exc).__name__}: {exc}"
        log.exception("Probe run failed")

    return snap


def _launch_browser(pw, headful: bool) -> Browser:
    return pw.chromium.launch(
        headless=not headful,
        args=[
            "--disable-blink-features=AutomationControlled",
            "--disable-dev-shm-usage",
            "--no-sandbox",
            "--disable-gpu",
            "--disable-infobars",
            "--window-size=1440,900",
        ],
    )


def _new_context(browser: Browser) -> BrowserContext:
    return browser.new_context(
        viewport=VIEWPORT,
        user_agent=USER_AGENT,
        locale=LOCALE,
        timezone_id=TIMEZONE,
        java_script_enabled=True,
        bypass_csp=True,
        extra_http_headers={
            "Accept-Language": "en-US,en;q=0.9",
            "DNT": "1",
            "Upgrade-Insecure-Requests": "1",
        },
    )


def print_run_report(snap: RunSnapshot) -> None:
    sep = "-" * 72
    print(sep)
    print(f"QUERY              : {snap.query!r}")
    print(f"STOREID            : {snap.store_id or '(none)'}")
    print(f"INITIAL URL        : {snap.initial_url}")
    print(f"FINAL URL          : {snap.final_url or snap.initial_url}")
    print(f"HTTP STATUS        : {snap.http_status}")
    print(f"PAGE TITLE         : {snap.page_title!r}")
    print(f"HTML BYTES         : {snap.html_bytes:,}")
    print(f"BODY WORDS         : {snap.body_words}")

    if snap.nav_error:
        print(f"NAV ERROR          : {snap.nav_error}")

    print("\n--- Cloudflare / challenge ---")
    print(f"  challenge_signals    : {snap.challenge or '(none)'}")
    print(f"  challenge_blocking   : {snap.challenge_blocking}")

    print("\n--- Product selectors ---")
    sel = snap.selector_meta.get("selector_used")
    count = snap.selector_meta.get("card_count", 0)
    print(f"  selector_matched     : {sel or '(none)'}")
    print(f"  candidate_card_count : {count}")
    hits = snap.selector_meta.get("selector_hits") or {}
    if hits and not sel:
        top = sorted(hits.items(), key=lambda x: -x[1])[:5]
        print(f"  partial_hits         : {top}")

    print("\n--- Store / location snippet ---")
    print(f"  {json.dumps(snap.store_snippet, ensure_ascii=False)}")

    print(f"\n--- Candidates ({len(snap.candidates)}) ---")
    if not snap.candidates:
        print("  (none — blocked, challenge page, or selectors missed)")
    else:
        for i, cand in enumerate(snap.candidates, 1):
            print(f"  [{i}] {json.dumps(cand, ensure_ascii=False)}")

    print(sep)


def print_store_comparison(a: RunSnapshot, b: RunSnapshot) -> None:
    label_a = f"{a.store_id} ({STORE_COMPARE_IDS.get(a.store_id or '', 'store A')})"
    label_b = f"{b.store_id} ({STORE_COMPARE_IDS.get(b.store_id or '', 'store B')})"
    print("\n" + "=" * 72)
    print(f"STORE COMPARISON — {label_a} vs {label_b}")
    print("=" * 72)

    def _summary(s: RunSnapshot) -> dict[str, Any]:
        titles = [c.get("title") for c in s.candidates[:3] if c.get("title")]
        prices = [c.get("price_display") or c.get("price") for c in s.candidates[:3]]
        availability = [
            (c.get("availability") or "")[:80] for c in s.candidates[:3]
        ]
        return {
            "storeid": s.store_id,
            "final_url": s.final_url,
            "challenge_blocking": s.challenge_blocking,
            "candidates": len(s.candidates),
            "sample_titles": titles,
            "sample_prices": prices,
            "sample_availability": availability,
            "store_snippet": s.store_snippet,
        }

    sa = _summary(a)
    sb = _summary(b)
    print(f"\n  {label_a}:\n{json.dumps(sa, indent=4, ensure_ascii=False)}")
    print(f"\n  {label_b}:\n{json.dumps(sb, indent=4, ensure_ascii=False)}")

    urls_differ = sa["final_url"] != sb["final_url"]
    counts_differ = sa["candidates"] != sb["candidates"]
    titles_differ = sa["sample_titles"] != sb["sample_titles"]
    prices_differ = sa["sample_prices"] != sb["sample_prices"]
    avail_differ = sa["sample_availability"] != sb["sample_availability"]

    print("\n  Observations:")
    print(f"    URLs differ              : {urls_differ}")
    print(f"    Candidate counts differ  : {counts_differ}")
    print(f"    Sample titles differ     : {titles_differ}")
    print(f"    Sample prices differ     : {prices_differ}")
    print(f"    Sample availability differ: {avail_differ}")

    if not sa["candidates"] and not sb["candidates"]:
        print("\n  No candidates — blocked or selectors missed.")
    elif avail_differ and not prices_differ:
        print(
            "\n  Store scoping works: same SKUs/prices, availability text differs "
            "(e.g. Brooklyn vs Columbus in NOT CARRIED message)."
        )
    elif titles_differ or prices_differ:
        print("\n  Store-specific titles or prices differ between runs.")
    else:
        print("\n  No obvious delta — try a query with in-stock items at both stores.")

    print("=" * 72)


def print_final_assessment(snaps: list[RunSnapshot]) -> None:
    print("\n" + "=" * 72)
    print("FINAL ASSESSMENT")
    print("=" * 72)

    any_candidates = any(len(s.candidates) > 0 for s in snaps)
    any_blocking = any(s.challenge_blocking for s in snaps)
    any_nav_fail = any(s.nav_error for s in snaps)

    print(f"  Playwright bypassed block? : {'YES' if any_candidates else 'NO'}")
    print(f"  Challenge still blocking?  : {'YES' if any_blocking and not any_candidates else 'NO' if any_candidates else 'LIKELY'}")
    print(f"  Navigation errors?         : {'YES' if any_nav_fail else 'NO'}")
    print(f"  Total candidates (all runs): {sum(len(s.candidates) for s in snaps)}")
    print(
        "  Remain probe-only?         : YES — experimental adapter only after "
        "repeatable Raven runs + in-stock validation"
    )

    working = [s.selector_meta.get("selector_used") for s in snaps if s.selector_meta.get("selector_used")]
    if working:
        print(f"  Selectors that worked      : {list(dict.fromkeys(working))}")
    else:
        print("  Selectors that worked      : (none this run)")

    print("\n  Recommended next step:")
    if any_candidates:
        print(
            "    Playwright path validated. Next on Raven: "
            "python scripts/smoke_microcenter_adapter.py --query \"ryzen 7 7800x3d\" "
            "--storeid 141 --limit 5"
        )
    else:
        print(
            "    Re-run with --headful on residential IP; save --debug-html; "
            "consider cf_clearance session reuse or official API before runtime adapter."
        )
    print("=" * 72)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Micro Center Playwright recon probe (experiments only)",
    )
    parser.add_argument("--query", default=DEFAULT_QUERY, help="Search term (default: rtx 4070)")
    parser.add_argument("--storeid", default=None, help="Store ID, e.g. 115 Brooklyn, 141 Columbus")
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT, help="Max candidates to print")
    parser.add_argument(
        "--headful",
        action="store_true",
        help="Visible browser (use xvfb-run on headless servers)",
    )
    parser.add_argument(
        "--debug-html",
        metavar="PATH",
        default=None,
        help="Write final rendered HTML to this path",
    )
    parser.add_argument(
        "--compare-stores",
        nargs="*",
        default=None,
        metavar="STOREID",
        help=(
            "Compare two stores in one run. Default IDs: 115 (Brooklyn) and 141 (Columbus). "
            "Examples: --compare-stores  |  --compare-stores 115 141"
        ),
    )
    args = parser.parse_args()

    compare_store_ids: list[str] | None = None
    if args.compare_stores is not None:
        compare_store_ids = args.compare_stores if args.compare_stores else ["115", "141"]
        if len(compare_store_ids) != 2:
            parser.error("--compare-stores requires zero args (defaults 115 141) or exactly two STOREIDs")

    debug_path = Path(args.debug_html) if args.debug_html else None

    print("=" * 72)
    print("MICRO CENTER PLAYWRIGHT PROBE (probe-only)")
    print("=" * 72)
    print(f"  mode={'headful' if args.headful else 'headless'}")

    snapshots: list[RunSnapshot] = []

    if compare_store_ids is not None:
        store_ids = compare_store_ids
        paths: list[Optional[Path]] = [None, None]
        if debug_path:
            stem = debug_path.stem
            suffix = debug_path.suffix or ".html"
            parent = debug_path.parent
            paths = [parent / f"{stem}_{sid}{suffix}" for sid in store_ids]
        for sid, path in zip(store_ids, paths):
            print(f"\n>>> Run storeid={sid} ({STORE_COMPARE_IDS.get(sid, sid)})")
            snap = run_probe(
                args.query,
                sid,
                args.limit,
                args.headful,
                path,
            )
            snapshots.append(snap)
            print_run_report(snap)
        if len(snapshots) == 2:
            print_store_comparison(snapshots[0], snapshots[1])
    else:
        snap = run_probe(
            args.query,
            args.storeid,
            args.limit,
            args.headful,
            debug_path,
        )
        snapshots.append(snap)
        print_run_report(snap)

    print_final_assessment(snapshots)
    return 0


if __name__ == "__main__":
    sys.exit(main())
