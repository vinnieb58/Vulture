"""
Newegg Playwright reconnaissance probe
=======================================
Reconnaissance only. Does NOT write to SQLite, send Discord alerts,
or touch the production adapter registry.

Escalation path when requests-based probe fails or for JS-render validation.

Usage:
    python experiments/adapters/newegg_playwright_probe.py
    python experiments/adapters/newegg_playwright_probe.py "rtx 4070"
    python experiments/adapters/newegg_playwright_probe.py "rtx 4070" --limit 5

Prerequisites:
    pip install playwright beautifulsoup4 lxml
    python -m playwright install chromium
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

# Allow importing shared probe helpers when run as a script
sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    from playwright.sync_api import TimeoutError as PlaywrightTimeout
    from playwright.sync_api import sync_playwright
except ImportError:
    print("ERROR: playwright is not installed.")
    print("Install: pip install playwright && python -m playwright install chromium")
    sys.exit(1)

from bs4 import BeautifulSoup

from newegg_probe import (
    DEFAULT_QUERIES,
    build_search_url,
    count_selectors,
    detect_blocking,
    extract_candidates,
    print_section,
)

VIEWPORT = {"width": 1280, "height": 800}
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
NAV_TIMEOUT_MS = 60_000
SETTLE_MS = 2_000


def run_playwright_probe(query: str, limit: int = 5) -> dict:
    url = build_search_url(query)
    print_section(f"NEWEGG PLAYWRIGHT PROBE — query={query!r}")
    print(f"  Target URL : {url}")

    t0 = time.monotonic()
    final_url = None
    page_title = None
    html = ""
    status_code = None
    error = None

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
            ],
        )
        context = browser.new_context(
            viewport=VIEWPORT,
            user_agent=USER_AGENT,
            locale="en-US",
            timezone_id="America/Chicago",
            extra_http_headers={"Accept-Language": "en-US,en;q=0.9"},
        )
        context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )
        page = context.new_page()

        try:
            response = page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
            status_code = response.status if response else None
            page.wait_for_timeout(SETTLE_MS)
            final_url = page.url
            page_title = page.title()
            html = page.content()
        except PlaywrightTimeout as exc:
            error = f"playwright_timeout: {exc}"
        except Exception as exc:
            error = f"playwright_error: {exc}"
        finally:
            context.close()
            browser.close()

    elapsed_ms = int((time.monotonic() - t0) * 1000)

    print(f"  Page title     : {page_title!r}")
    print(f"  Final URL      : {final_url}")
    print(f"  HTTP status    : {status_code}")
    print(f"  Body length    : {len(html):,} bytes")
    print(f"  Load time      : {elapsed_ms} ms")

    if error:
        print(f"  ERROR          : {error}")
        return {"query": query, "error": error, "candidates": []}

    soup = BeautifulSoup(html, "lxml")
    selector_counts = count_selectors(soup)
    product_card_count = selector_counts.get(".item-cell", 0)

    blocking = detect_blocking(
        status_code,
        page_title or "",
        html,
        final_url or url,
        url,
        product_card_count=product_card_count,
    )
    print(f"  Blocking detected : {blocking['blocking_detected']}")
    if blocking["triggered_indicators"]:
        print(f"  Blocking indicators: {blocking['triggered_indicators']}")

    print("  Selector counts:")
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

    return {
        "query": query,
        "page_title": page_title,
        "final_url": final_url,
        "body_length": len(html),
        "load_time_ms": elapsed_ms,
        "selector_counts": selector_counts,
        "candidates": candidates,
        "blocking_detected": blocking["blocking_detected"],
        "error": None,
    }


def print_assessment(results: list[dict]) -> None:
    print_section("PLAYWRIGHT FINAL ASSESSMENT")
    with_candidates = sum(1 for r in results if r.get("candidates"))
    total = len(results)
    print(f"  Queries with candidates : {with_candidates}/{total}")
    browser_required = with_candidates == 0
    print(f"  Browser automation required? : {'YES' if browser_required else 'NO — requests likely sufficient'}")
    print(f"  Should remain probe-only?    : YES")
    print()


def main() -> None:
    parser = argparse.ArgumentParser(description="Newegg Playwright recon probe (experimental)")
    parser.add_argument("query", nargs="*", help="Search term(s). Default: built-in query list.")
    parser.add_argument("--limit", type=int, default=5, help="Max candidates per query (default: 5)")
    args = parser.parse_args()

    queries = args.query if args.query else DEFAULT_QUERIES[:1]  # one query by default for Playwright

    print_section("Newegg Playwright Probe")
    print(f"  Queries : {queries}")
    print(f"  Limit   : {args.limit}")

    results = [run_playwright_probe(q, limit=args.limit) for q in queries]
    print_assessment(results)


if __name__ == "__main__":
    main()
