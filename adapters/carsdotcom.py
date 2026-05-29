"""
adapters/carsdotcom.py

Cars.com vehicle search adapter for Vulture.

Status: EXPERIMENTAL — usable on residential-IP hosts (e.g. Raven).
----------------------------------------------------------------------
Do not set ``stable=True`` until the adapter has passed multiple
production hunt cycles across different query types and network
conditions.

Parsing strategy
----------------
Playwright-backed Chromium fetch → BeautifulSoup + ``data-vehicle-details``
JSON attribute extraction.

Cars.com is server-rendered (no Next.js SPA shell), but it sits behind
Cloudflare Bot Management and Akamai Bot Manager which block requests from
datacenter IPs.  Playwright-controlled Chromium with lightweight
``navigator.webdriver`` masking passes through cleanly from residential
IPs like Raven.

Each listing card is a ``<fuse-card data-listing-id="..." data-vehicle-details='...'>``
custom element.  The ``data-vehicle-details`` JSON attribute carries the
complete structured payload: year, make, model, trim, vin, price, mileage,
stockType, seller.zip, listingId.  DOM selectors are used as fallback and
to retrieve fields not present in the JSON (link, city/state location,
dealer name).

Confirmed from live HTML captured by Playwright probe (May 2026):
    - 48+ listing cards per search-results page
    - ``[data-listing-id]`` is the stable card anchor selector
    - ``data-vehicle-details`` JSON present on ~60 % of cards (organic
      listings); absent on ad/promoted placements (CSS fallback handles those)
    - Link: ``<a data-card-link="" href="https://www.cars.com/vehicledetail/...">``
    - Location: ``div[slot="footer"] .datum-icon span`` → ``"City, ST (X mi)"``
    - Dealer: ``span.fuse-body-small`` (filtered against price/MSRP strings)

Anti-bot behaviour
------------------
* Datacenter IPs: ERR_HTTP2_PROTOCOL_ERROR on first cold headless hit;
  HTTP 403 on subsequent ``requests`` attempts.
* Residential IP (Raven): HTTP 200, 48+ cards, no interactive challenge.
  The ``cdn-cgi/challenge-platform`` string visible in the HTML is a
  Cloudflare CDN resource reference, not an active bot challenge.
* Recommendation: run only from Raven or another residential-IP host.
  Proxy support can be added later via Playwright ``launch(proxy=...)`` if
  cloud-based operation is ever required.

Geography / zip
---------------
Cars.com supports explicit zip-code location targeting via ``&zip=XXXXX``
in the search URL.  The server auto-detects the requesting IP's zip if
none is supplied and redirects accordingly (e.g. ``zip=77471`` from Raven).

Zip selection priority (highest to lowest):
  1. ``city`` argument is a 5-digit zip string (e.g. ``city="77002"``)
  2. ``_DEFAULT_ZIP`` constant (``"77471"`` — Rosenberg, TX;
     Raven's residential GeoIP area)

A future improvement is to accept ``adapter_options["zip"]`` once the
hunt execution model supports per-adapter option dicts.

Mileage note
------------
The ``Listing`` model does not have a ``mileage`` field (DB schema is
unchanged).  Mileage is extracted from ``data-vehicle-details`` JSON but
is currently discarded at the ``Listing`` construction step.  If mileage
is ever added to ``Listing``, the ``_card_to_listing`` function already
extracts it and the ``_parse_int`` helper normalises it.

Requirements
------------
* playwright >= 1.60.0
* Ubuntu 26.04: set ``PLAYWRIGHT_HOST_PLATFORM_OVERRIDE=ubuntu24.04-x64``
  in the environment (or add to ``~/.bashrc`` on Raven).
  This module sets the env var automatically at import time if it is absent.
* Chromium browser binary: ``python -m playwright install chromium``

Manual smoke tests
------------------
Direct adapter smoke (no .env required)::

    python3 -c "
    from adapters.carsdotcom import search_carsdotcom
    results = search_carsdotcom('toyota camry', city='77471', limit=5)
    for r in results: print(r)
    "

Expected: up to 5 ``Listing`` objects with ``source='carsdotcom'``.

Direct smoke with default zip::

    python3 -c "
    from adapters.carsdotcom import search_carsdotcom
    for r in search_carsdotcom('honda civic', limit=3):
        print(r.title, '|', r.price, '|', r.location, '|', r.link[:60])
    "

Full hunt cycle on Raven (requires .env + active hunt in DB)::

    VULTURE_HUNT_SOURCE=db python3 main.py

Or via the YAML hunt config (add a ``carsdotcom`` hunt to config/hunts.yaml)::

    VULTURE_HUNT_SOURCE=yaml python3 main.py

Verify the result in logs/vulture.log — look for lines containing
``carsdotcom GET``, ``redirected``, and ``NEW:`` / ``OLD:`` / ``FILTERED:``.

Geography enforcement note
--------------------------
``maximum_distance=all`` (or 9999) causes Cars.com to return nationwide
results sorted by relevance, ignoring the zip code for geographic ranking.
The adapter now uses ``_DEFAULT_RADIUS_MILES = 100`` to enforce locality.
Pass ``radius_miles=300`` for a wider search or ``radius_miles=50`` for
strict locality::

    python3 -c "
    from adapters.carsdotcom import search_carsdotcom
    for r in search_carsdotcom('toyota camry', city='77471', limit=5, radius_miles=50):
        print(r.title, '|', r.price, '|', r.location)
    "
"""

import json
import logging
import os
import re
from urllib.parse import quote_plus, urljoin

from bs4 import BeautifulSoup

from models.listing import Listing

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Ubuntu 26.04 Playwright compatibility
# ---------------------------------------------------------------------------
# Ubuntu 26.04 is not yet in Playwright's platform matrix.  The override
# tells Playwright to use Ubuntu 24.04 browser binaries, which are
# dynamically-linked and run correctly on 26.04.  Set only if not already
# provided by the caller's environment so that explicit overrides win.
if not os.environ.get("PLAYWRIGHT_HOST_PLATFORM_OVERRIDE"):
    os.environ["PLAYWRIGHT_HOST_PLATFORM_OVERRIDE"] = "ubuntu24.04-x64"

try:
    from playwright.sync_api import (
        Browser,
        BrowserContext,
        Page,
        TimeoutError as PlaywrightTimeout,
        sync_playwright,
    )
    _PLAYWRIGHT_AVAILABLE = True
except ImportError:
    _PLAYWRIGHT_AVAILABLE = False
    log.warning(
        "playwright package not found. Cars.com adapter will return empty results. "
        "Install with: pip install playwright && python -m playwright install chromium"
    )

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

_CARS_ORIGIN = "https://www.cars.com"
_SEARCH_URL = "https://www.cars.com/shopping/results/"

# Default zip: 77471 (Rosenberg, TX) — matches Raven's residential GeoIP area.
# Replaced by the hunt's city param if it looks like a zip code, or by an
# explicit zip_override keyword argument.
_DEFAULT_ZIP = "77471"

# Default search radius in miles.
#
# Root cause of nationwide results: "maximum_distance=all" is interpreted
# by Cars.com as 9999 (no limit), which returns results from anywhere in the
# country sorted by relevance rather than by distance to the supplied zip.
# Using an explicit mile radius enforces geographic locality.
#
# 100 miles is the sweet spot for Houston-area searches — covers the full
# metro and adjacent markets without going national.  Override this constant
# or pass radius_miles to search_carsdotcom() for different coverage areas.
_DEFAULT_RADIUS_MILES = 100

# Regex to detect 5-digit US zip codes (used for city and query scanning)
_ZIP_RE = re.compile(r"^\d{5}$")

# Regex to extract a 5-digit zip embedded in the hunt query string
# (e.g. "toyota camry 77471" or "honda civic zip:77002")
_ZIP_IN_QUERY_RE = re.compile(r"(?:^|[\s,;:])(\d{5})(?:\s|$)")

# Browser fingerprint settings
_VIEWPORT = {"width": 1440, "height": 900}
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
_LOCALE = "en-US"
_TIMEZONE = "America/Chicago"

# Timeouts (milliseconds)
_NAV_TIMEOUT_MS = 45_000
_LISTING_WAIT_MS = 12_000
_SETTLE_MS = 1_500

# Primary card selector — stable as of May 2026
_CARD_SELECTOR = "[data-listing-id]"

# ---------------------------------------------------------------------------
# DOM field selectors (confirmed from live HTML, May 2026)
# ---------------------------------------------------------------------------

_LINK_SELECTORS = [
    "a[data-card-link]",
    "a[href*='/vehicledetail/']",
    "a[href*='/vehicle/']",
    "a[href]",
]

_LOCATION_SELECTORS = [
    "div[slot='footer'] div.datum-icon span",
    "div[slot='footer'] span",
    "[class*='miles-from']",
    "[class*='distance-from']",
]

_DEALER_SELECTORS = [
    "span.fuse-body-small",
    "[class*='dealer']",
    "[class*='seller']",
]

_TITLE_FALLBACK_SELECTORS = [
    "h2 a[data-card-link] span",
    "h2 a span",
    "h2",
    "h3",
]

_PRICE_FALLBACK_SELECTORS = [
    "span.fuse-body-larger",
    ".primary-price",
    "[class*='primary-price']",
    "[class*='fuse-body-larger']",
    "[class*='price']",
]

_MILEAGE_FALLBACK_SELECTORS = [
    "div.mileage span",
    "div.datum-icon.mileage span",
    ".mileage span",
    "[class*='mileage']",
    "[class*='odometer']",
]

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _resolve_zip(
    city: str,
    zip_override: "str | None" = None,
    query: str = "",
) -> str:
    """
    Determine the 5-digit US zip code to use for the search.

    Resolution priority (highest to lowest):
      1. *zip_override* — explicit zip passed by the caller (future
         adapter_options["zip"] path once the execution model supports it).
      2. *city* arg is a 5-digit zip string (e.g. ``city="77002"``).
      3. A 5-digit number embedded in the *query* string
         (e.g. ``"toyota camry 77471"``).
      4. ``_DEFAULT_ZIP`` (``"77471"`` — Raven GeoIP fallback).
    """
    if zip_override and _ZIP_RE.match(zip_override.strip()):
        log.debug("carsdotcom: zip from zip_override=%s", zip_override.strip())
        return zip_override.strip()

    if _ZIP_RE.match(city.strip()):
        log.debug("carsdotcom: zip from city arg=%s", city.strip())
        return city.strip()

    m = _ZIP_IN_QUERY_RE.search(query)
    if m:
        extracted = m.group(1)
        log.debug("carsdotcom: zip extracted from query=%r -> %s", query, extracted)
        return extracted

    log.debug(
        "carsdotcom: no zip found in zip_override=%r city=%r query=%r; "
        "using default zip=%s",
        zip_override, city, query, _DEFAULT_ZIP,
    )
    return _DEFAULT_ZIP


def _build_search_url(query: str, zip_code: str, radius_miles: int = _DEFAULT_RADIUS_MILES) -> str:
    """
    Build the Cars.com search URL.

    The ``maximum_distance`` parameter is critical for geographic relevance.
    Using ``all`` (or 9999) means no radius limit → Cars.com returns
    nationwide results sorted by relevance, not by distance to the zip.
    An explicit mile value enforces geographic locality.

    Cars.com URL shape after redirect (observed May 2026):
        ?keyword[]=<query>&zip=<zip>&maximum_distance=<miles>&sort=best_match_desc
    """
    return (
        f"{_SEARCH_URL}"
        f"?keyword={quote_plus(query)}"
        f"&stock_type=all"
        f"&zip={zip_code}"
        f"&maximum_distance={radius_miles}"
    )


def _inject_stealth(page: "Page") -> None:
    """
    Add lightweight JS init script to mask the most-checked headless signals.
    Runs before any page script; does not require playwright-stealth package.
    """
    page.add_init_script("""
        Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
        Object.defineProperty(navigator, 'plugins', {
            get: () => [
                { name: 'Chrome PDF Plugin', filename: 'internal-pdf-viewer' },
                { name: 'Chrome PDF Viewer', filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai' },
                { name: 'Native Client', filename: 'internal-nacl-plugin' },
            ],
        });
        Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
        window.chrome = { runtime: {}, loadTimes: function(){}, csi: function(){}, app: {} };
    """)


def _fetch_html(query: str, zip_code: str, radius_miles: int = _DEFAULT_RADIUS_MILES) -> "str | None":
    """
    Use Playwright Chromium to load the Cars.com search results page and
    return the rendered HTML as a string.  Returns None on any unrecoverable
    error so callers can degrade gracefully.
    """
    if not _PLAYWRIGHT_AVAILABLE:
        log.error(
            "carsdotcom: playwright not available; cannot fetch. "
            "Install with: pip install playwright && python -m playwright install chromium"
        )
        return None

    url = _build_search_url(query, zip_code, radius_miles)
    log.info("carsdotcom: GET %s", url)

    try:
        with sync_playwright() as pw:
            browser: Browser = pw.chromium.launch(
                headless=True,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--disable-dev-shm-usage",
                    "--no-sandbox",
                    "--disable-gpu",
                ],
            )
            ctx: BrowserContext = browser.new_context(
                viewport=_VIEWPORT,
                user_agent=_USER_AGENT,
                locale=_LOCALE,
                timezone_id=_TIMEZONE,
                java_script_enabled=True,
                bypass_csp=True,
                extra_http_headers={
                    "Accept-Language": "en-US,en;q=0.9",
                    "DNT": "1",
                    "Upgrade-Insecure-Requests": "1",
                },
            )
            page: Page = ctx.new_page()
            _inject_stealth(page)

            try:
                resp = page.goto(
                    url, wait_until="domcontentloaded", timeout=_NAV_TIMEOUT_MS
                )
            except PlaywrightTimeout:
                log.error("carsdotcom: navigation timed out after %d ms for query %r", _NAV_TIMEOUT_MS, query)
                browser.close()
                return None
            except Exception as exc:
                err = str(exc).lower()
                if "http2_protocol_error" in err or "err_http2" in err:
                    log.error(
                        "carsdotcom: ERR_HTTP2_PROTOCOL_ERROR for query %r — "
                        "Cloudflare RST-stream block (HTTP/2). "
                        "Run from a residential IP (Raven).",
                        query,
                    )
                else:
                    log.error(
                        "carsdotcom: navigation error for query %r: %s",
                        query,
                        str(exc)[:200],
                    )
                browser.close()
                return None

            http_status = resp.status if resp else None
            final_url = page.url
            if final_url != url:
                log.info("carsdotcom: redirected -> %s", final_url)
            else:
                log.info("carsdotcom: no redirect (status=%s)", http_status)

            if http_status == 403:
                log.error(
                    "carsdotcom: HTTP 403 for query %r — "
                    "Cloudflare/Akamai block. Run from a residential IP (Raven).",
                    query,
                )
                browser.close()
                return None

            if http_status not in (200, 206, None):
                log.error("carsdotcom: HTTP %s for query %r", http_status, query)
                browser.close()
                return None

            # Wait for listing cards or settle after timeout
            try:
                page.wait_for_selector(_CARD_SELECTOR, timeout=_LISTING_WAIT_MS)
            except PlaywrightTimeout:
                log.warning(
                    "carsdotcom: listing cards not found within %d ms for query %r — "
                    "proceeding with whatever HTML is available",
                    _LISTING_WAIT_MS, query,
                )

            page.wait_for_timeout(_SETTLE_MS)
            html = page.content()
            browser.close()

            log.info(
                "carsdotcom: fetched %d chars of HTML for query=%r zip=%s radius=%dmi (status=%s)",
                len(html), query, zip_code, radius_miles, http_status,
            )
            return html

    except Exception as exc:
        log.error("carsdotcom: unexpected Playwright error for query %r: %s", query, exc)
        return None


# ---------------------------------------------------------------------------
# Card parsing
# ---------------------------------------------------------------------------


def _parse_vehicle_details_json(card) -> dict:
    """
    Parse the ``data-vehicle-details`` JSON attribute on a ``<fuse-card>``
    element.  Returns a flat dict with vehicle fields, or {} if absent.

    Confirmed shape (May 2026):
      year, make, model, trim, vin, price, msrp, mileage, stockType,
      seller.zip, listingId.
    """
    raw_json = card.get("data-vehicle-details")
    if not raw_json:
        return {}
    try:
        d = json.loads(raw_json)
    except (json.JSONDecodeError, TypeError) as exc:
        log.debug("carsdotcom: data-vehicle-details parse error: %s", exc)
        return {}

    result: dict = {}

    # Title: stockType + year + make + model + trim
    parts = [
        str(d.get("stockType") or "").strip(),
        str(d.get("year") or "").strip(),
        str(d.get("make") or "").strip(),
        str(d.get("model") or "").strip(),
        str(d.get("trim") or "").strip(),
    ]
    title = " ".join(p for p in parts if p)
    if title:
        result["title"] = title

    # Price: prefer sale price, skip when zero (MSRP-only listings)
    for price_key in ("price", "msrp"):
        val = d.get(price_key)
        if val and str(val) not in ("0", "0.0", ""):
            result["price_raw"] = str(val)
            break

    # Mileage (extracted but not stored in Listing — no mileage field in model)
    mileage_val = d.get("mileage")
    if mileage_val and str(mileage_val) != "0":
        result["mileage_raw"] = str(mileage_val)

    # Identifiers
    if d.get("listingId"):
        result["listing_id"] = d["listingId"]
    if d.get("vin"):
        result["vin"] = d["vin"]
    if (d.get("seller") or {}).get("zip"):
        result["seller_zip"] = d["seller"]["zip"]

    return result


def _parse_int(raw: "str | int | None") -> "int | None":
    """Parse a raw string or number into an integer, stripping commas and $."""
    if raw is None:
        return None
    s = str(raw).replace(",", "").replace("$", "").strip()
    m = re.search(r"\d+", s)
    if not m:
        return None
    try:
        return int(m.group())
    except ValueError:
        return None


def _extract_text(element, selectors: list[str]) -> "str | None":
    """Try each selector in order; return the text of the first match."""
    for sel in selectors:
        try:
            el = element.select_one(sel)
            if el:
                return el.get_text(" ", strip=True) or None
        except Exception:
            pass
    return None


def _extract_link(card) -> "str | None":
    """
    Extract the vehicle detail URL from a listing card.

    Priority:
      1. <a data-card-link href="...">  (confirmed Cars.com pattern)
      2. Fallback link selectors
      3. <card-gallery card-href="...">
      4. Construct from listingId if already parsed
    """
    for sel in _LINK_SELECTORS:
        try:
            el = card.select_one(sel)
            if el and el.get("href"):
                href = el["href"]
                return href if href.startswith("http") else urljoin(_CARS_ORIGIN, href)
        except Exception:
            pass

    gallery = card.find("card-gallery")
    if gallery and gallery.get("card-href"):
        href = gallery["card-href"]
        return href if href.startswith("http") else urljoin(_CARS_ORIGIN, href)

    return None


def _extract_location(card) -> "str | None":
    """
    Extract location string (city/state + distance) and dealer name from card.

    Combines dealer name + city/state when both are available, e.g.:
    ``"Ed Voyles Acura — Chamblee, GA (519 mi)"``

    Falls back to dealer only, then seller zip, then None.
    """
    # City/state from footer datum-icon: "Chamblee, GA (519 mi)"
    city_state: str | None = None
    for sel in _LOCATION_SELECTORS:
        try:
            el = card.select_one(sel)
            if el:
                text = el.get_text(" ", strip=True)
                # Accept strings with a comma and at least one letter
                if text and "," in text and any(c.isalpha() for c in text):
                    city_state = text
                    break
        except Exception:
            pass

    # Dealer name from span.fuse-body-small (weaker color)
    # Guard: skip price/MSRP/rating strings that also use this class
    dealer: str | None = None
    for sel in _DEALER_SELECTORS:
        try:
            for el in card.select(sel):
                text = el.get_text(" ", strip=True)
                if not text or len(text) <= 3:
                    continue
                if any(tok in text for tok in ("$", "MSRP", "Est.", "/mo", "%")):
                    continue
                if text.replace(".", "").replace(" ", "").isdigit():
                    continue
                dealer = text
                break
        except Exception:
            pass
        if dealer:
            break

    if city_state and dealer:
        return f"{dealer} — {city_state}"
    if city_state:
        return city_state
    if dealer:
        return dealer
    return None


def _card_to_listing(card, query: str) -> "Listing | None":
    """
    Convert a parsed BeautifulSoup listing card element to a ``Listing``.

    Pass 1: ``data-vehicle-details`` JSON for title, price, identifiers.
    Pass 2: DOM selectors for link, location, dealer name.
    Pass 3: CSS fallback for any field still missing (ad cards lack JSON attr).

    Returns None if no usable title or link can be extracted.
    """
    # Pass 1: JSON attribute
    json_data = _parse_vehicle_details_json(card)

    title = json_data.get("title")
    price = _parse_int(json_data.get("price_raw"))
    listing_id = json_data.get("listing_id")

    # Pass 2: DOM fields
    link = _extract_link(card)
    if not link and listing_id:
        link = f"{_CARS_ORIGIN}/vehicledetail/{listing_id}/"

    location = _extract_location(card)

    # Pass 3: CSS fallback for missing title / price
    if not title:
        title = _extract_text(card, _TITLE_FALLBACK_SELECTORS)

    if price is None:
        raw_price_text = _extract_text(card, _PRICE_FALLBACK_SELECTORS)
        price = _parse_int(raw_price_text)

    # Require at minimum a title to produce a Listing
    if not title:
        log.debug(
            "carsdotcom: skipping card (listing_id=%s) — no title extracted",
            listing_id or "(unknown)",
        )
        return None

    # Require a link to make the listing actionable
    if not link:
        log.debug(
            "carsdotcom: skipping card %r — no link extracted", title[:60]
        )
        return None

    return Listing(
        source="carsdotcom",
        title=title,
        price=price,
        location=location,
        link=link,
    )


def _parse_listings(html: str, query: str, limit: int) -> list[Listing]:
    """
    Parse rendered HTML and return up to *limit* ``Listing`` objects.
    """
    soup = BeautifulSoup(html, "lxml")
    cards = soup.select(_CARD_SELECTOR)

    if not cards:
        log.warning(
            "carsdotcom: selector %r matched 0 elements — "
            "page structure may have changed or bot block may be active",
            _CARD_SELECTOR,
        )
        return []

    log.debug("carsdotcom: found %d card(s) in HTML for query %r", len(cards), query)

    listings: list[Listing] = []
    seen_links: set[str] = set()

    for card in cards:
        if len(listings) >= limit:
            break
        listing = _card_to_listing(card, query)
        if listing is None:
            continue
        # Deduplicate by link
        if listing.link in seen_links:
            continue
        seen_links.add(listing.link)
        listings.append(listing)

    return listings


# ---------------------------------------------------------------------------
# Public adapter function
# ---------------------------------------------------------------------------


def search_carsdotcom(
    query: str,
    city: str = _DEFAULT_ZIP,
    limit: int = 10,
    *,
    zip_override: "str | None" = None,
    radius_miles: int = _DEFAULT_RADIUS_MILES,
) -> list[Listing]:
    """
    Search Cars.com for *query* and return up to *limit* ``Listing`` objects.

    Parameters
    ----------
    query:
        Search term, e.g. ``"toyota camry"`` or ``"honda civic under 15000"``.
        A 5-digit zip embedded in the query (e.g. ``"toyota camry 77471"``) is
        automatically extracted and used as the search zip.
    city:
        Supply a 5-digit US zip code for location-targeted results
        (e.g. ``"77002"`` for downtown Houston).  Any non-zip string falls
        back to the default zip (``"77471"``).
    limit:
        Maximum number of ``Listing`` objects to return.
    zip_override:
        Explicit zip that takes priority over *city*.  Intended for future
        ``adapter_options["zip"]`` support once the hunt execution model
        passes per-adapter option dicts.  Ignored when not a valid 5-digit zip.
    radius_miles:
        Search radius in miles from the resolved zip.  Defaults to
        ``_DEFAULT_RADIUS_MILES`` (100).  Use a larger value (e.g. 300) for
        nationwide vehicle searches, or smaller (e.g. 50) for strict locality.

        **Why this matters:** passing ``maximum_distance=all`` (or 9999) tells
        Cars.com to return nationwide results sorted only by relevance, not by
        distance.  An explicit radius is required for geographically coherent
        results.

    Returns
    -------
    list[Listing]
        Up to *limit* de-duplicated listings, or ``[]`` on any failure.
        Never raises; all errors are logged and return an empty list.

    Notes
    -----
    * Requires Playwright Chromium on a **residential IP** host.
      Datacenter IPs will encounter Cloudflare Bot Management blocks.
    * Each call starts a fresh Chromium instance (~1–2 s overhead).
    * Does not write to SQLite.  Does not send Discord alerts.
    * The ``city``, ``zip_override``, and embedded-in-query zip are all checked
      by ``_resolve_zip()``.  The first valid 5-digit zip found wins.
    """
    zip_code = _resolve_zip(city, zip_override=zip_override, query=query)
    log.info(
        "carsdotcom search: query=%r zip=%s radius=%dmi city_arg=%r limit=%d",
        query, zip_code, radius_miles, city, limit,
    )

    html = _fetch_html(query, zip_code, radius_miles)
    if html is None:
        log.warning("carsdotcom: fetch returned no HTML for query %r", query)
        return []

    listings = _parse_listings(html, query, limit)

    if not listings:
        log.warning(
            "carsdotcom: query=%r zip=%s returned 0 usable listings — "
            "check logs above for block/timeout signals",
            query, zip_code,
        )
        return []

    observed_locations = sorted({lst.location for lst in listings if lst.location})
    log.info(
        "carsdotcom: query=%r zip=%s radius=%dmi -> %d listing(s). "
        "observed_locations=%s",
        query, zip_code, radius_miles, len(listings), observed_locations,
    )
    return listings
