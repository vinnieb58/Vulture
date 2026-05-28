"""
Cars.com Playwright reconnaissance probe
=========================================
Reconnaissance only. Does NOT write to SQLite, send Discord alerts,
or touch the production adapter registry.

Usage:
    python experiments/adapters/carsdotcom_playwright_probe.py "toyota camry"
    python experiments/adapters/carsdotcom_playwright_probe.py "ford f-150" --headed
    python experiments/adapters/carsdotcom_playwright_probe.py "honda civic" --save-html --screenshot
    python experiments/adapters/carsdotcom_playwright_probe.py "subaru outback" --slow --headed

Flags:
    --headed       Run in a visible browser window instead of headless.
                   Requires a display (X server / Wayland). On Raven without
                   a display, use Xvfb: xvfb-run python3 ... --headed
    --slow         Add 500 ms slow-motion delay between Playwright actions.
                   Useful for watching behaviour in --headed mode.
    --save-html    Write the final rendered HTML to experiments/debug/carsdotcom/.
    --screenshot   Capture a full-page PNG to experiments/debug/carsdotcom/.

Goal: determine whether Playwright-controlled Chromium can reliably load
Cars.com search results, bypass anti-bot challenges, and yield parseable
listing cards — complementing the requests-based carsdotcom_probe.py.

Assessment produced at the end:
    - browser viable?
    - headless blocked?
    - headed works?
    - parser still works?
    - production adapter recommendation
"""

import argparse
import json
import logging
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote_plus, urljoin

from bs4 import BeautifulSoup

# Playwright is an optional dependency; fail loudly with instructions.
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
# Logging — stdout only
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.DEBUG,
    format="%(levelname)-8s %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("carsdotcom_pw_probe")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

BASE_URL = "https://www.cars.com/shopping/results/"
CARS_ORIGIN = "https://www.cars.com"

# Debug artefacts are written here; directory is created at runtime
DEBUG_DIR = Path("experiments/debug/carsdotcom")

# Fingerprint-like browser context settings
VIEWPORT = {"width": 1440, "height": 900}
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
LOCALE = "en-US"
TIMEZONE = "America/Chicago"

# How long to wait for listing cards to appear after page load (ms)
LISTING_WAIT_MS = 12_000
# Additional settle time after cards appear to allow lazy-loaded content
SETTLE_MS = 2_000
# Maximum total page-load time (ms)
NAV_TIMEOUT_MS = 60_000

MAX_LISTINGS = 5

# ---------------------------------------------------------------------------
# Data extraction notes (confirmed from live HTML, May 2026)
# ---------------------------------------------------------------------------
#
# Cars.com uses custom web components (<fuse-card>, <card-gallery>, etc.).
# Each listing card is:
#   <fuse-card data-listing-id="<uuid>" data-vehicle-details='<json>'>
#
# The `data-vehicle-details` JSON attribute carries the complete structured
# payload:  year, make, model, trim, vin, price, msrp, mileage, stockType,
# seller.zip, listingId, primaryThumbnail, etc.
#
# Additional DOM-only fields:
#   link     — <a data-card-link="" href="https://www.cars.com/vehicledetail/...">
#   location — div[slot="footer"] .datum-icon span  → e.g. "Chamblee, GA (519 mi)"
#   dealer   — span.fuse-body-small (inline style color:var(--fuse-color-text-weaker))
#   price    — also in DOM as span.fuse-body-larger, but JSON is cleaner
#   mileage  — also in DOM as div.datum-icon.mileage span, but JSON is cleaner
#
# Extraction priority:
#   1. data-vehicle-details JSON  (price, mileage, title parts, VIN)
#   2. DOM selectors              (link, location, dealer)
#   3. CSS fallback selectors     (generic title/price if JSON absent)

# ---------------------------------------------------------------------------
# Challenge / anti-bot detection strings
# ---------------------------------------------------------------------------

CHALLENGE_TITLE_FRAGMENTS = [
    "just a moment",
    "attention required",
    "access denied",
    "checking your browser",
    "ddos protection by cloudflare",
    "please wait",
    "security check",
    "verify you are human",
    "robot or human",
]

CHALLENGE_BODY_MARKERS = [
    # Cloudflare challenge page markers
    "cf-browser-verification",
    "cdn-cgi/challenge-platform",
    "Enable JavaScript and cookies to continue",
    "Checking your browser before accessing",
    "cf-challenge-running",
    # Akamai Bot Manager interactive challenge
    "akamai-bm-telemetry",
    "_akamai_edgescape",
    # PerimeterX
    "px-captcha",
    "PerimeterX",
    # DataDome
    "datadome",
    # Generic
    "prove you are human",
    "human verification",
    "unusual traffic",
    "automated queries",
    "bot detection",
]

# ---------------------------------------------------------------------------
# CSS selectors (kept in sync with carsdotcom_probe.py)
# ---------------------------------------------------------------------------

LISTING_SELECTORS = [
    "[data-listing-id]",
    "[data-vehicle-id]",
    "[data-testid='vehicle-listing']",
    "[data-testid='listing-card']",
    "[data-testid='vehicle-card']",
    "article.vehicle-card",
    "div.vehicle-card",
    ".vehicle-card",
    ".listing-row",
    "[class*='vehicle-card']",
    "[class*='listing-row']",
    "[class*='VehicleCard']",
    "[class*='ListingCard']",
    "div[class*='result-card']",
    "div[class*='search-result']",
    "li[class*='vehicle']",
    "li[class*='listing']",
]

FIELD_SELECTORS = {
    "title": [
        # Cars.com: title in an <h2> > <a data-card-link> > <span>
        "h2 a[data-card-link] span",
        "h2 a span",
        "h2",
        "h3",
        "[class*='title']",
        "[class*='Title']",
    ],
    "price": [
        # Cars.com confirmed (May 2026): price in span.fuse-body-larger
        "span.fuse-body-larger",
        ".primary-price",
        "[class*='primary-price']",
        "[class*='fuse-body-larger']",
        "[class*='price']",
        "[data-testid='price']",
    ],
    "mileage": [
        # Cars.com confirmed: div.datum-icon.mileage > span
        "div.mileage span",
        "div.datum-icon.mileage span",
        ".mileage span",
        "[class*='mileage']",
        "[class*='odometer']",
        "[class*='miles']",
    ],
    "location": [
        # Cars.com confirmed: footer datum-icon contains location "City, ST (X mi)"
        # The SVG label="Listing location" precedes the span with the text
        "div[slot='footer'] div.datum-icon span",
        "div[slot='footer'] span",
        "[class*='miles-from']",
        "[class*='distance-from']",
        "[class*='location']",
    ],
    "dealer": [
        # Cars.com confirmed: dealer name in span.fuse-body-small (weaker color)
        # It appears just before the review-star datum-icon
        "span.fuse-body-small",
        "[class*='dealer']",
        "[class*='seller']",
    ],
    "link": [
        # Cars.com confirmed: <a data-card-link="" href="https://www.cars.com/vehicledetail/...">
        "a[data-card-link]",
        "a[href*='/vehicledetail/']",
        "a[href*='/vehicle/']",
        "a[href]",
    ],
}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _timestamp() -> str:
    """UTC timestamp string suitable for filenames."""
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")


def _detect_challenge(title: str, html: str) -> dict[str, list[str]]:
    """
    Scan page title and rendered HTML for anti-bot / challenge markers.
    Returns a dict keyed by platform → list of matched evidence strings.
    """
    hits: dict[str, list[str]] = {}
    title_lower = title.lower()
    html_lower = html.lower()

    for fragment in CHALLENGE_TITLE_FRAGMENTS:
        if fragment in title_lower:
            hits.setdefault("challenge_title", []).append(fragment)

    for marker in CHALLENGE_BODY_MARKERS:
        if marker.lower() in html_lower:
            platform = (
                "cloudflare" if "cf-" in marker.lower() or "cdn-cgi" in marker.lower() or "cloudflare" in marker.lower()
                else "akamai" if "akamai" in marker.lower()
                else "perimeterx" if "px" in marker.lower() or "PerimeterX" in marker
                else "datadome" if "datadome" in marker.lower()
                else "generic"
            )
            hits.setdefault(platform, []).append(marker)

    return hits


def _normalize(raw: dict, query: str) -> dict:
    """
    Normalise a raw extraction dict to Vulture adapter shape + mileage.
    Mirrors _normalize() in carsdotcom_probe.py.
    """
    title: str | None = None
    if raw.get("title"):
        title = str(raw["title"]).strip()

    price: int | None = None
    if raw.get("price"):
        price_str = str(raw["price"]).replace(",", "").replace("$", "").strip()
        m = re.search(r"\d+", price_str)
        if m:
            try:
                price = int(m.group())
            except ValueError:
                pass

    mileage: int | None = None
    if raw.get("mileage"):
        mileage_str = str(raw["mileage"]).replace(",", "").strip()
        m = re.search(r"\d+", mileage_str)
        if m:
            try:
                mileage = int(m.group())
            except ValueError:
                pass

    # Location: prefer "City, ST (X mi)" string; fall back to dealer name
    location: str | None = None
    city_state = str(raw.get("location") or "").strip()
    dealer_name = str(raw.get("dealer") or "").strip()
    seller_zip = str(raw.get("seller_zip") or "").strip()
    if city_state:
        location = city_state
        if dealer_name:
            location = f"{dealer_name} — {city_state}"
    elif dealer_name:
        location = dealer_name + (f" (zip {seller_zip})" if seller_zip else "")
    elif seller_zip:
        location = f"zip {seller_zip}"

    link: str | None = None
    raw_link = str(raw.get("link") or "").strip()
    if raw_link:
        link = raw_link if raw_link.startswith("http") else urljoin(CARS_ORIGIN, raw_link)

    return {
        "source": "cars.com",
        "query": query,
        "title": title or None,
        "price": price,
        "mileage": mileage,
        "location": location,
        "link": link,
    }


def _extract_vehicle_details_json(card) -> dict:
    """
    Parse the `data-vehicle-details` JSON attribute from a <fuse-card> element.
    Returns a flat dict with vehicle fields, or {} if absent/invalid.

    Cars.com embeds a complete structured payload here:
      year, make, model, trim, vin, price, mileage, stockType,
      seller.zip, listingId, primaryThumbnail, etc.
    """
    raw_json = card.get("data-vehicle-details")
    if not raw_json:
        return {}
    try:
        d = json.loads(raw_json)
    except (json.JSONDecodeError, TypeError) as exc:
        log.debug("data-vehicle-details JSON parse error: %s", exc)
        return {}

    result: dict = {}

    # Build title from stock type + year + make + model + trim
    year = str(d.get("year") or "").strip()
    make = str(d.get("make") or "").strip()
    model = str(d.get("model") or "").strip()
    trim = str(d.get("trim") or "").strip()
    stock_type = str(d.get("stockType") or "").strip()  # "New" / "Used" / "Certified"
    parts = [p for p in [stock_type, year, make, model, trim] if p]
    if parts:
        result["title"] = " ".join(parts)

    # Price — prefer sale price; skip MSRP-only when price==0
    for price_key in ("price", "msrp"):
        val = d.get(price_key)
        if val and str(val) not in ("0", "0.0", ""):
            result["price"] = str(val)
            break

    # Mileage — "0" = brand-new car, not meaningful
    raw_mileage = d.get("mileage")
    if raw_mileage and str(raw_mileage) != "0":
        result["mileage"] = str(raw_mileage)

    # Listing ID for fallback link construction
    listing_id = d.get("listingId")
    if listing_id:
        result["listing_id"] = listing_id

    # VIN as a unique dedup key
    vin = d.get("vin")
    if vin:
        result["vin"] = vin

    # Seller zip (only location data in JSON; no city/state)
    seller = d.get("seller") or {}
    seller_zip = seller.get("zip")
    if seller_zip:
        result["seller_zip"] = seller_zip

    return result


def _extract_from_html(html: str, query: str) -> list[dict]:
    """
    Parse rendered HTML with BeautifulSoup and extract listing cards.

    Strategy:
      1. Find all [data-listing-id] cards.
      2. For each card, parse data-vehicle-details JSON for structured data
         (title, price, mileage, VIN).
      3. Supplement with DOM selectors for link, location, dealer name.
      4. Fall back to pure CSS extraction for cards that lack the JSON attr.

    Returns a list of normalised candidate dicts.
    """
    soup = BeautifulSoup(html, "lxml")
    results: list[dict] = []

    items: list = []
    matched_selector: str | None = None

    for selector in LISTING_SELECTORS:
        try:
            found = soup.select(selector)
        except Exception as exc:
            log.debug("Selector %r error: %s", selector, exc)
            continue
        if found:
            log.info("DOM selector matched: %r  (%d nodes)", selector, len(found))
            matched_selector = selector
            items = found
            break

    if not items:
        log.warning("No CSS selector matched any listing card.")
        return []

    log.info("Processing up to %d cards via selector %r", MAX_LISTINGS, matched_selector)

    for item in items[:MAX_LISTINGS]:
        raw: dict = {}

        # ---- Pass 1: data-vehicle-details JSON (highest confidence) ----
        json_data = _extract_vehicle_details_json(item)
        if json_data:
            raw.update(json_data)
            log.debug("JSON extraction yielded keys: %s", list(json_data.keys()))
        else:
            log.debug("No data-vehicle-details on card; CSS-only fallback.")

        # ---- Pass 2: DOM for fields not supplied by JSON ----

        # Link — not in JSON; must come from DOM
        if not raw.get("link"):
            for link_sel in FIELD_SELECTORS["link"]:
                el = item.select_one(link_sel)
                if el and el.get("href"):
                    raw["link"] = el["href"]
                    break
            # card-gallery[card-href] is an alternative anchor on some layouts
            if not raw.get("link"):
                gallery = item.find("card-gallery")
                if gallery and gallery.get("card-href"):
                    raw["link"] = gallery["card-href"]
            # Last resort: construct from listingId
            if not raw.get("link") and raw.get("listing_id"):
                raw["link"] = f"{CARS_ORIGIN}/vehicledetail/{raw['listing_id']}/"

        # Location — footer datum-icon span → "City, ST (X mi)"
        if not raw.get("location"):
            for loc_sel in FIELD_SELECTORS["location"]:
                el = item.select_one(loc_sel)
                if el:
                    text = el.get_text(" ", strip=True)
                    # Accept strings that contain a comma and letter chars
                    # (filters out pure-numeric or rating strings)
                    if text and "," in text and any(c.isalpha() for c in text):
                        raw["location"] = text
                        break

        # Dealer name — span.fuse-body-small (supplementary; used if no city/state)
        # Guard: skip spans that look like prices (contain "$"), MSRP labels, or
        # rating numbers — those also use fuse-body-small on Cars.com.
        if not raw.get("dealer"):
            for dlr_sel in FIELD_SELECTORS["dealer"]:
                for el in item.select(dlr_sel):
                    text = el.get_text(" ", strip=True)
                    if not text or len(text) <= 3:
                        continue
                    # Reject price-like strings and known non-dealer labels
                    if any(tok in text for tok in ("$", "MSRP", "Est.", "/mo", "%")):
                        continue
                    # Reject pure numeric / short rating strings like "4.8"
                    if text.replace(".", "").replace(" ", "").isdigit():
                        continue
                    raw["dealer"] = text
                    break
                if raw.get("dealer"):
                    break

        # Title from DOM if JSON didn't produce one
        if not raw.get("title"):
            for title_sel in FIELD_SELECTORS["title"]:
                el = item.select_one(title_sel)
                if el:
                    raw["title"] = el.get_text(" ", strip=True)
                    break

        # Price from DOM if JSON didn't produce one
        if not raw.get("price"):
            for price_sel in FIELD_SELECTORS["price"]:
                el = item.select_one(price_sel)
                if el:
                    raw["price"] = el.get_text(strip=True)
                    break

        # Mileage from DOM if JSON didn't produce one
        if not raw.get("mileage"):
            for mileage_sel in FIELD_SELECTORS["mileage"]:
                el = item.select_one(mileage_sel)
                if el:
                    raw["mileage"] = el.get_text(strip=True)
                    break

        if raw:
            results.append(_normalize(raw, query))

    return results


def _add_stealth_scripts(page: Page) -> None:
    """
    Inject lightweight JS patches before any page script runs to reduce
    obvious headless-mode fingerprints. Not a full stealth suite —
    playwright-stealth is not required — but removes the most-checked
    signals: navigator.webdriver and empty plugins/languages arrays.
    """
    page.add_init_script("""
        // Mask navigator.webdriver
        Object.defineProperty(navigator, 'webdriver', {
            get: () => undefined,
        });

        // Fake a non-empty plugins array (headless has 0 plugins)
        Object.defineProperty(navigator, 'plugins', {
            get: () => [
                { name: 'Chrome PDF Plugin', filename: 'internal-pdf-viewer' },
                { name: 'Chrome PDF Viewer', filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai' },
                { name: 'Native Client', filename: 'internal-nacl-plugin' },
            ],
        });

        // Fake languages list
        Object.defineProperty(navigator, 'languages', {
            get: () => ['en-US', 'en'],
        });

        // Remove automation-related Chrome flags
        window.chrome = {
            runtime: {},
            loadTimes: function() {},
            csi: function() {},
            app: {},
        };

        // Mask headless in userAgent hint (basic, not full UA-CH spoofing)
        Object.defineProperty(navigator, 'userAgent', {
            get: () =>
                'Mozilla/5.0 (Windows NT 10.0; Win64; x64) ' +
                'AppleWebKit/537.36 (KHTML, like Gecko) ' +
                'Chrome/124.0.0.0 Safari/537.36',
        });
    """)


def _save_html(html: str, query: str, ts: str) -> Path:
    DEBUG_DIR.mkdir(parents=True, exist_ok=True)
    safe_query = re.sub(r"[^\w\-]", "_", query)[:40]
    path = DEBUG_DIR / f"carsdotcom_{safe_query}_{ts}.html"
    path.write_text(html, encoding="utf-8")
    log.info("HTML saved to: %s  (%d bytes)", path, len(html.encode("utf-8")))
    return path


def _save_screenshot(page: Page, query: str, ts: str) -> Path:
    DEBUG_DIR.mkdir(parents=True, exist_ok=True)
    safe_query = re.sub(r"[^\w\-]", "_", query)[:40]
    path = DEBUG_DIR / f"carsdotcom_{safe_query}_{ts}.png"
    page.screenshot(path=str(path), full_page=True)
    log.info("Screenshot saved to: %s", path)
    return path


# ---------------------------------------------------------------------------
# Main probe
# ---------------------------------------------------------------------------


def probe(
    query: str,
    headed: bool,
    slow: bool,
    save_html: bool,
    take_screenshot: bool,
) -> None:
    sep = "=" * 72
    ts = _timestamp()

    print(sep)
    print(f"  CARS.COM PLAYWRIGHT PROBE   query={query!r}")
    print(f"  mode={'headed' if headed else 'headless'}  slow={slow}  "
          f"save_html={save_html}  screenshot={take_screenshot}")
    print(sep)

    param_str = (
        f"keyword={quote_plus(query)}&stock_type=all&maximum_distance=all"
    )
    search_url = f"{BASE_URL}?{param_str}"
    print(f"\n  Target URL : {search_url}\n")

    # Warn about headed mode on headless environments
    if headed:
        print("  NOTE: --headed requires a display (X server / Wayland).")
        print("        On Raven without a display:  xvfb-run python3 ... --headed\n")

    challenge_detected: dict[str, list[str]] = {}
    listings: list[dict] = []
    final_url: str = search_url
    page_title: str = "(unknown)"
    body_word_count: int = 0
    html: str = ""
    card_count: int = 0
    nav_succeeded: bool = False

    with sync_playwright() as pw:
        # ------------------------------------------------------------------
        # Step 1 — Launch browser
        # ------------------------------------------------------------------
        print("--- Step 1: launch browser ---")
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
            log.error("Browser launch failed: %s", exc)
            if headed:
                print("  => Headed launch failed. Is a display available?")
                print("     Try: xvfb-run python3 experiments/adapters/carsdotcom_playwright_probe.py "
                      f'"{query}" --headed')
            _print_assessment(
                browser_viable=False,
                headless_blocked=None,
                headed_works=False if headed else None,
                parser_works=False,
                listings_found=0,
                challenge=challenge_detected,
                extra=["Browser launch failed — see error above."],
            )
            return

        print(f"  Launched Chromium  headless={not headed}  slow_mo={launch_opts['slow_mo']}ms")

        # ------------------------------------------------------------------
        # Step 2 — Create browser context
        # ------------------------------------------------------------------
        print("\n--- Step 2: browser context ---")
        ctx: BrowserContext = browser.new_context(
            viewport=VIEWPORT,
            user_agent=USER_AGENT,
            locale=LOCALE,
            timezone_id=TIMEZONE,
            java_script_enabled=True,
            # Accept all cookies
            accept_downloads=False,
            # Bypass CSP so our init scripts run cleanly
            bypass_csp=True,
            # Extra HTTP headers that a real Chrome sends
            extra_http_headers={
                "Accept-Language": "en-US,en;q=0.9",
                "Accept-Encoding": "gzip, deflate, br",
                "DNT": "1",
                "Upgrade-Insecure-Requests": "1",
            },
        )
        print(f"  Context: viewport={VIEWPORT}  UA=Chrome/124  locale={LOCALE}  tz={TIMEZONE}")

        # ------------------------------------------------------------------
        # Step 3 — Navigate
        # ------------------------------------------------------------------
        print("\n--- Step 3: navigate to search URL ---")
        page: Page = ctx.new_page()
        _add_stealth_scripts(page)

        # Capture console errors for debugging
        console_errors: list[str] = []
        page.on("console", lambda msg: console_errors.append(f"[{msg.type}] {msg.text}")
                if msg.type in ("error", "warning") else None)

        http_status: int | None = None
        nav_error: str | None = None

        try:
            resp = page.goto(
                search_url,
                wait_until="domcontentloaded",
                timeout=NAV_TIMEOUT_MS,
            )
            nav_succeeded = True
            http_status = resp.status if resp else None
            print(f"  Navigation complete.  HTTP status={http_status}  URL={page.url[:80]}")
        except PlaywrightTimeout:
            nav_error = "timeout"
            log.error("Navigation timed out after %d ms", NAV_TIMEOUT_MS)
            print("  => Navigation timed out. Possible hard block or very slow response.")
        except Exception as exc:
            nav_error = str(exc)
            err_lower = nav_error.lower()
            # ERR_HTTP2_PROTOCOL_ERROR is Cloudflare's connection-drop block on
            # headless Chromium — it RST-streams before sending any HTTP response.
            if "http2_protocol_error" in err_lower or "err_http2" in err_lower:
                print("  => ERR_HTTP2_PROTOCOL_ERROR — Cloudflare/anti-bot RST-stream block.")
                print("     This is a hard headless block. The server refused to negotiate.")
                challenge_detected = {"cloudflare": ["ERR_HTTP2_PROTOCOL_ERROR (RST-stream block)"]}
            elif "connection_refused" in err_lower or "connection_reset" in err_lower:
                print("  => Connection refused/reset — server dropped the connection.")
                challenge_detected = {"generic": [f"connection error: {exc.__class__.__name__}"]}
            elif "net::err" in err_lower:
                print(f"  => Network error: {exc.__class__.__name__}")
                challenge_detected = {"network": [f"net error: {str(exc)[:80]}"]}
            else:
                log.error("Navigation error: %s", exc)
            print(f"  Nav error detail: {str(exc)[:200]}")

        if nav_error:
            # Try to capture whatever content the page holds (may be empty)
            try:
                html = page.content()
            except Exception:
                html = ""
            browser.close()
            _print_assessment(
                browser_viable=False,
                headless_blocked=(not headed) and bool(challenge_detected),
                headed_works=None,
                parser_works=False,
                listings_found=0,
                challenge=challenge_detected,
                extra=[
                    f"Navigation failed: {nav_error[:120]}",
                    f"Mode: {'headed' if headed else 'headless'}",
                    "If headless is blocked: try --headed (with xvfb-run on Raven)",
                    "If headed also fails: residential IP or playwright-stealth required",
                ],
            )
            return

        # ------------------------------------------------------------------
        # Step 4 — Capture early state
        # ------------------------------------------------------------------
        print("\n--- Step 4: early page state ---")
        final_url = page.url
        page_title = page.title()
        print(f"  Final URL  : {final_url}")
        print(f"  Page title : {page_title!r}")

        # Check for challenge right after navigation
        early_html = page.content()
        challenge_detected = _detect_challenge(page_title, early_html)
        if challenge_detected:
            print(f"  CHALLENGE DETECTED IMMEDIATELY: {challenge_detected}")
        else:
            print("  No challenge detected at page load.")

        # ------------------------------------------------------------------
        # Step 5 — Wait for listing cards
        # ------------------------------------------------------------------
        print("\n--- Step 5: wait for listing cards ---")
        listing_appeared = False

        for selector in LISTING_SELECTORS[:4]:  # try the most likely selectors
            try:
                page.wait_for_selector(selector, timeout=LISTING_WAIT_MS)
                listing_appeared = True
                print(f"  Selector appeared: {selector!r}")
                break
            except PlaywrightTimeout:
                log.debug("Selector %r not found within %d ms", selector, LISTING_WAIT_MS)

        if not listing_appeared:
            print(f"  WARNING: No listing selector appeared within {LISTING_WAIT_MS} ms.")
            print("  Checking if challenge is blocking...")
            # Re-check challenge after wait
            mid_html = page.content()
            challenge_now = _detect_challenge(page.title(), mid_html)
            if challenge_now:
                print(f"  Challenge confirmed after wait: {challenge_now}")
                challenge_detected.update(challenge_now)
            else:
                print("  No challenge found — listings may use different selector or lazy-load.")
        else:
            # Let lazy content settle
            page.wait_for_timeout(SETTLE_MS)
            print(f"  Settled after {SETTLE_MS} ms.")

        # ------------------------------------------------------------------
        # Step 6 — Re-check challenge after full settle
        # ------------------------------------------------------------------
        print("\n--- Step 6: post-settle challenge check ---")
        html = page.content()
        final_title = page.title()
        challenge_detected = _detect_challenge(final_title, html)

        if challenge_detected:
            print("  CHALLENGE SIGNALS PRESENT IN FINAL PAGE:")
            for platform, signals in challenge_detected.items():
                print(f"    [{platform.upper()}] {signals}")
        else:
            print("  No challenge signals in final page content.")

        # ------------------------------------------------------------------
        # Step 7 — Page stats
        # ------------------------------------------------------------------
        print("\n--- Step 7: page statistics ---")
        soup = BeautifulSoup(html, "lxml")
        body_text = soup.body.get_text(" ", strip=True) if soup.body else ""
        body_word_count = len(body_text.split())
        script_count = len(soup.find_all("script"))
        json_ld_count = len(soup.find_all("script", type="application/ld+json"))

        # Count listing cards before extraction
        for selector in LISTING_SELECTORS[:4]:
            try:
                found = soup.select(selector)
                if found:
                    card_count = len(found)
                    print(f"  Listing cards found ({selector!r}): {card_count}")
                    break
            except Exception:
                pass
        else:
            print("  Listing cards found: 0 (no selector matched)")

        print(f"  Page title (final) : {final_title!r}")
        print(f"  Final URL          : {page.url}")
        print(f"  Body word count    : {body_word_count}")
        print(f"  <script> tags      : {script_count}")
        print(f"  JSON-LD blobs      : {json_ld_count}")
        print(f"  HTML size          : {len(html):,} chars")

        if console_errors:
            print(f"  Console errors/warnings ({len(console_errors)}):")
            for e in console_errors[:5]:
                print(f"    {e[:120]}")

        # ------------------------------------------------------------------
        # Step 8 — Debug artefacts
        # ------------------------------------------------------------------
        if save_html or take_screenshot:
            print("\n--- Step 8: saving debug artefacts ---")
        else:
            print("\n--- Step 8: debug artefacts (skipped — use --save-html / --screenshot) ---")

        saved_html_path: Path | None = None
        saved_screenshot_path: Path | None = None

        if save_html:
            try:
                saved_html_path = _save_html(html, query, ts)
                print(f"  HTML  -> {saved_html_path}")
            except Exception as exc:
                log.error("Failed to save HTML: %s", exc)

        if take_screenshot:
            try:
                saved_screenshot_path = _save_screenshot(page, query, ts)
                print(f"  PNG   -> {saved_screenshot_path}")
            except Exception as exc:
                log.error("Failed to save screenshot: %s", exc)

        # ------------------------------------------------------------------
        # Step 9 — Extract listings
        # ------------------------------------------------------------------
        print("\n--- Step 9: extract and normalize listings ---")
        if body_word_count < 80:
            print("  Body too thin to parse — skipping extraction.")
        elif challenge_detected and card_count == 0:
            print("  Challenge present and no cards found — skipping extraction.")
        else:
            listings = _extract_from_html(html, query)
            if listings:
                print(f"  Extracted {len(listings)} candidate(s):\n")
                for i, listing in enumerate(listings, 1):
                    print(f"  [{i}]")
                    for field, value in listing.items():
                        print(f"       {field:<10}: {value!r}")
                    print()
            else:
                print("  No listings extracted from rendered HTML.")
                # Diagnostic: show which selectors found anything at all
                print("  Selector presence check:")
                for sel in LISTING_SELECTORS[:8]:
                    try:
                        n = len(soup.select(sel))
                        if n:
                            print(f"    {sel!r}  -> {n} node(s)")
                    except Exception:
                        pass

        browser.close()

    # ------------------------------------------------------------------
    # Step 10 — Final assessment
    # ------------------------------------------------------------------
    is_headless = not headed
    listings_found = len(listings)
    parser_works = listings_found > 0

    # Distinguish a *blocking* challenge (no content served) from a
    # *non-blocking* Cloudflare CDN signal (cdn-cgi/ path in page resources).
    # If listing cards are present AND body has substantial text, any Cloudflare
    # challenge marker is almost certainly a CDN resource URL, not an active block.
    cf_signals = challenge_detected.get("cloudflare", [])
    cf_is_cdn_only = (
        cf_signals
        and all("cdn-cgi/challenge-platform" in s or "header:" in s or "cookie:" in s
                for s in cf_signals)
        and card_count >= 5
        and body_word_count > 400
    )
    if cf_is_cdn_only:
        log.info(
            "Cloudflare signals appear non-blocking (CDN resources on real page). "
            "card_count=%d  body_words=%d", card_count, body_word_count
        )
        # Downgrade to informational only — not a real block
        effective_challenge = {k: v for k, v in challenge_detected.items()
                               if k != "cloudflare"}
        effective_challenge["cloudflare_cdn"] = cf_signals  # renamed key
    else:
        effective_challenge = challenge_detected

    has_hard_challenge = bool(effective_challenge) and not cf_is_cdn_only

    # browser_viable: succeeded if we got listings
    browser_viable = parser_works and nav_succeeded

    # headless_blocked: hard challenge in headless run with no listings
    headless_blocked = is_headless and has_hard_challenge and not parser_works

    # headed_works: if we ran headed without hard challenge
    headed_works = headed and not has_hard_challenge and parser_works

    extra_notes = [
        f"Mode: {'headed' if headed else 'headless'}",
        f"Challenge signals: {challenge_detected}",
        f"CF CDN-only (non-blocking): {cf_is_cdn_only}",
        f"Hard challenge: {has_hard_challenge}",
        f"Listing cards in DOM: {card_count}",
        f"Body word count: {body_word_count}",
        f"Nav succeeded: {nav_succeeded}",
    ]
    if saved_html_path:
        extra_notes.append(f"HTML saved: {saved_html_path}")
    if saved_screenshot_path:
        extra_notes.append(f"Screenshot saved: {saved_screenshot_path}")

    _print_assessment(
        browser_viable=browser_viable,
        headless_blocked=headless_blocked,
        headed_works=headed_works if headed else None,
        parser_works=parser_works,
        listings_found=listings_found,
        challenge=effective_challenge,
        extra=extra_notes,
    )


# ---------------------------------------------------------------------------
# Assessment printer
# ---------------------------------------------------------------------------


def _print_assessment(
    browser_viable: bool,
    headless_blocked: bool | None,
    headed_works: bool | None,
    parser_works: bool,
    listings_found: int,
    challenge: dict,
    extra: list[str] | None = None,
) -> None:
    sep = "=" * 72
    print()
    print(sep)
    print("  FINAL ASSESSMENT")
    print(sep)

    def yn(val: bool | None, unknown: str = "NOT TESTED THIS RUN") -> str:
        if val is True:
            return "YES"
        if val is False:
            return "NO"
        return unknown

    print(f"  browser viable?               : {yn(browser_viable)}")
    print(f"  headless blocked?             : {yn(headless_blocked)}")
    print(f"  headed works?                 : {yn(headed_works)}")
    print(f"  parser still works?           : {yn(parser_works)}")
    print(f"  candidate listings extracted  : {listings_found}")
    print(f"  anti-bot challenge present    : {'YES — ' + str(list(challenge.keys())) if challenge else 'NO'}")

    if extra:
        print("  notes:")
        for note in extra:
            print(f"    - {note}")

    print()

    # Determine recommendation
    if browser_viable and parser_works and not challenge:
        rec = (
            "READY TO BUILD — Playwright gets clean results with no challenge. "
            "Promote to adapters/carsdotcom.py using Playwright-backed fetch. "
            "Consider playwright-stealth for long-term stability."
        )
    elif browser_viable and parser_works and challenge:
        rec = (
            "VIABLE WITH CAVEATS — Listings extracted despite challenge signals. "
            "The challenge may be present but non-blocking (e.g. Akamai telemetry). "
            "Run --headed to confirm visually. Consider playwright-stealth."
        )
    elif headless_blocked and headed_works is None:
        rec = (
            "HEADLESS BLOCKED — Re-run with --headed (using xvfb-run on Raven) "
            "to test whether a visible browser bypasses the challenge. "
            "If headed works, the adapter will need playwright-stealth or "
            "undetected-playwright to operate headless in production."
        )
    elif headless_blocked is False and not parser_works:
        rec = (
            "HARD BLOCK — No challenge detected but still no listings. "
            "Inspect the saved HTML (--save-html) to find the actual DOM structure. "
            "Update LISTING_SELECTORS and FIELD_SELECTORS accordingly."
        )
    else:
        rec = (
            "UNCERTAIN — Listings not extracted but no definitive block. "
            "Run with --save-html and inspect the HTML. "
            "Also try --headed --screenshot for a visual snapshot."
        )

    print(f"  production adapter recommendation:")
    print(f"    {rec}")
    print(sep)
    print()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Cars.com Playwright reconnaissance probe (experiments only)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples (Windows):
  python experiments\\adapters\\carsdotcom_playwright_probe.py "toyota camry"
  python experiments\\adapters\\carsdotcom_playwright_probe.py "ford f-150" --save-html --screenshot
  python experiments\\adapters\\carsdotcom_playwright_probe.py "honda civic" --headed --slow

Examples (Raven / Linux):
  python3 experiments/adapters/carsdotcom_playwright_probe.py "toyota camry"
  python3 experiments/adapters/carsdotcom_playwright_probe.py "ford f-150" --save-html --screenshot
  xvfb-run python3 experiments/adapters/carsdotcom_playwright_probe.py "honda civic" --headed --slow
""",
    )
    p.add_argument("query", nargs="+", help="Search term (e.g. 'toyota camry')")
    p.add_argument(
        "--headed",
        action="store_true",
        default=False,
        help="Run in a visible browser window (requires a display; use xvfb-run on Raven)",
    )
    p.add_argument(
        "--slow",
        action="store_true",
        default=False,
        help="Add 500 ms slow-motion delay between Playwright actions",
    )
    p.add_argument(
        "--save-html",
        action="store_true",
        default=False,
        dest="save_html",
        help=f"Save final rendered HTML to {DEBUG_DIR}/",
    )
    p.add_argument(
        "--screenshot",
        action="store_true",
        default=False,
        help=f"Capture a full-page PNG screenshot to {DEBUG_DIR}/",
    )
    return p


if __name__ == "__main__":
    parser = _build_parser()
    args = parser.parse_args()
    search_query = " ".join(args.query)

    probe(
        query=search_query,
        headed=args.headed,
        slow=args.slow,
        save_html=args.save_html,
        take_screenshot=args.screenshot,
    )
