"""
Cars.com candidate-source probe
================================
Reconnaissance only. Does NOT write to SQLite, send Discord alerts,
or touch the production adapter registry.

Usage:
    python experiments/adapters/carsdotcom_probe.py "toyota camry"
    python experiments/adapters/carsdotcom_probe.py "honda civic under 15000"
    python experiments/adapters/carsdotcom_probe.py "ford f-150"

Goal: determine whether Cars.com is a viable future Vulture vehicle
adapter using plain requests, and characterize any anti-bot measures.

Assessment produced at the end:
  - requests viable?
  - browser automation likely required?
  - anti-bot severity
  - suitability as a future stable adapter

This script is intentionally verbose. Every diagnostic step is printed
so the output itself serves as the reconnaissance record.
"""

import json
import logging
import re
import sys
from urllib.parse import quote_plus, urljoin

import requests
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# Logging — stdout only, no file handlers, no SQLite, no Discord
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.DEBUG,
    format="%(levelname)-8s %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("carsdotcom_probe")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

BASE_URL = "https://www.cars.com/shopping/results/"
CARS_ORIGIN = "https://www.cars.com"

# Mimic a real browser as closely as possible at the HTTP layer
HEADERS = {
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

# Known SPA / JS-framework fingerprints
SPA_MARKERS = [
    "__NEXT_DATA__",
    "__NUXT__",
    "data-reactroot",
    "__REDUX_STORE__",
    "window.__INITIAL_STATE__",
    "window.__PRELOADED_STATE__",
    "window.__APP_STATE__",
    "ng-version",          # Angular
    "data-ng-app",         # Angular legacy
    "svelte-",             # Svelte
    "__vue",               # Vue
    "ember-application",   # Ember
]


# CSS selectors to probe for listing cards (rough priority order)
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

# Sub-selectors to pull fields from a matched listing card.
# Confirmed against live Cars.com HTML (May 2026 Playwright probe).
#
# Key finding: each <fuse-card> element carries a `data-vehicle-details`
# JSON attribute with all structured fields. CSS selectors below serve as
# fallback for cards that lack that attribute (ads, promoted placements).
FIELD_SELECTORS = {
    "title": [
        # Cars.com confirmed: title in <h2><a data-card-link><span>
        "h2 a[data-card-link] span",
        "h2 a span",
        "h2",
        "h3",
        "[class*='title']",
        "[class*='Title']",
    ],
    "price": [
        # Cars.com confirmed: price in span.fuse-body-larger
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
        # Cars.com confirmed: footer datum-icon span → "City, ST (X mi)"
        "div[slot='footer'] div.datum-icon span",
        "div[slot='footer'] span",
        "[class*='miles-from']",
        "[class*='distance-from']",
        "[class*='location']",
    ],
    "dealer": [
        # Cars.com confirmed: dealer name in span.fuse-body-small (weaker color)
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

MAX_LISTINGS = 5

# ---------------------------------------------------------------------------
# Detection helpers
# ---------------------------------------------------------------------------


def _detect_spa_markers(html: str) -> list[str]:
    found = []
    for marker in SPA_MARKERS:
        if marker in html:
            found.append(marker)
    return found


def _detect_block_signals(
    html: str,
    headers: dict,
    cookie_names: list[str],
) -> dict[str, list[str]]:
    """
    Scan HTTP response headers, cookie names, and HTML body for known
    anti-bot platform signals.

    Design note: cookie-name checks are done against the actual cookie jar,
    NOT via HTML string search, to avoid false positives where a cookie name
    merely appears as page text (e.g. "cf_clearance" mentioned in docs).

    Returns a dict mapping platform name -> list of matched evidence strings.
    """
    hits: dict[str, list[str]] = {}
    combined_html = html.lower()
    cookie_set = set(cookie_names)

    # ------------------------------------------------------------------
    # Header-based checks (highest confidence)
    # ------------------------------------------------------------------
    header_str = " ".join(f"{k}:{v}" for k, v in headers.items()).lower()
    header_checks = {
        "cloudflare": ["cf-ray", "cf-cache-status", "cf-request-id", "cf-mitigated"],
        "akamai": ["x-akamai-session-id", "x-akamai-transformed", "akamai-grn", "x-akamai-request-id"],
        "datadome": ["x-datadome", "x-datadome-cid"],
        "perimeterx": ["x-px-orig-status", "x-px-enforcer-telemetry"],
        "incapsula": ["x-iinfo", "x-cdn-geo"],
    }
    for platform, hdr_keys in header_checks.items():
        for hk in hdr_keys:
            if hk in header_str:
                hits.setdefault(platform, []).append(f"header:{hk}")

    # ------------------------------------------------------------------
    # Cookie-name checks (high confidence — server actually set these)
    # ------------------------------------------------------------------
    cookie_checks = {
        "cloudflare": ["cf_clearance", "__cf_bm", "__cflb"],
        "akamai": ["_abck", "bm_sz", "ak_bmsc"],
        "datadome": ["datadome", "dd_cookie_test_"],
        "perimeterx": ["_pxhd", "_pxvid"],
        "incapsula": ["incap_ses", "visid_incap"],
    }
    for platform, cookie_keys in cookie_checks.items():
        for ck in cookie_keys:
            # Prefix match to handle session-specific suffixes (e.g. incap_ses_*)
            if any(name == ck or name.startswith(ck) for name in cookie_set):
                hits.setdefault(platform, []).append(f"cookie:{ck}")

    # ------------------------------------------------------------------
    # HTML body challenge-page markers (medium confidence)
    # These indicate the *current page* is a challenge, not just that a
    # platform is present. Only fire on strings that only appear in
    # challenge pages, not in normal content.
    # ------------------------------------------------------------------
    hard_challenge_markers = {
        "cloudflare": [
            "cf-browser-verification",
            "cloudflare-static",
            "Checking your browser",
            "Enable JavaScript and cookies to continue",
            "cdn-cgi/challenge-platform",
        ],
        "perimeterx": [
            "px-captcha",
            "PerimeterX",
            "pxCaptcha",
        ],
        "recaptcha": [
            "g-recaptcha",
            "www.google.com/recaptcha",
            "grecaptcha.execute",
        ],
        "generic_block": [
            "access denied",
            "unusual traffic",
            "too many requests",
            "human verification",
            "verify you are not a robot",
            "prove you are human",
            "please complete the security check",
            "ddos protection by",
        ],
    }
    for platform, markers in hard_challenge_markers.items():
        for m in markers:
            if m.lower() in combined_html:
                hits.setdefault(platform, []).append(f"html:{m}")

    return hits


def _extract_next_data(soup: BeautifulSoup) -> dict | None:
    """Pull Next.js embedded JSON from __NEXT_DATA__ script tag."""
    tag = soup.find("script", id="__NEXT_DATA__")
    if tag and tag.string:
        try:
            return json.loads(tag.string)
        except json.JSONDecodeError as exc:
            log.warning("__NEXT_DATA__ found but JSON parse failed: %s", exc)
    return None


def _extract_json_ld(soup: BeautifulSoup) -> list[dict]:
    """
    Extract JSON-LD structured data blocks.
    Car listing pages often embed schema.org/Vehicle or schema.org/Offer data.
    """
    results = []
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or "")
            if isinstance(data, dict):
                results.append(data)
            elif isinstance(data, list):
                results.extend(data)
        except (json.JSONDecodeError, TypeError):
            pass
    return results


def _walk_for_vehicle_listings(obj, depth: int = 0, max_depth: int = 10) -> list[dict]:
    """
    Recursively walk a JSON blob looking for vehicle-listing-shaped objects.

    Recognizes two shapes:
      A) Cars.com API response objects — typically have 'vin', 'stockType',
         'year'/'make'/'model'/'trim', 'listPrice' / 'price', 'mileage',
         'dealerName', and a 'vdpUrl' or 'stockUrl' link.
      B) Generic shape — has title/name + (price or url or id).

    We intentionally keep the matching broad and let _normalize() clean up.
    """
    if depth > max_depth:
        return []
    found = []

    if isinstance(obj, dict):
        keys_lower = {k.lower() for k in obj}

        # Shape A: vehicle-specific keys
        is_vehicle = (
            "vin" in keys_lower
            or ("year" in keys_lower and "make" in keys_lower)
            or ("stocktype" in keys_lower)
        )
        # Shape B: generic listing-ish
        is_generic = (
            ("title" in keys_lower or "name" in keys_lower)
            and ("price" in keys_lower or "url" in keys_lower or "id" in keys_lower)
        )

        if is_vehicle or is_generic:
            candidate = {}
            for k, v in obj.items():
                lk = k.lower()
                # Title-ish fields
                if lk == "title":
                    candidate["title"] = v
                elif lk == "name" and "title" not in candidate:
                    candidate["title"] = v
                # Composite title from year/make/model/trim
                elif lk == "year" and "year" not in candidate:
                    candidate["year"] = v
                elif lk == "make":
                    candidate["make"] = v
                elif lk == "model":
                    candidate["model"] = v
                elif lk == "trim":
                    candidate["trim"] = v
                # Price fields
                elif lk in ("listprice", "price", "askingprice", "salesprice"):
                    candidate.setdefault("price", v)
                # Mileage fields
                elif lk in ("mileage", "miles", "odometer", "odometerreading"):
                    candidate.setdefault("mileage", v)
                # Location / dealer fields
                elif lk in ("dealername", "dealer_name", "dealer"):
                    candidate.setdefault("dealer", v)
                elif lk in ("city", "state", "location", "locationname", "dealercity"):
                    candidate.setdefault("location", v)
                # Link fields
                elif lk in ("vdpurl", "stockurl", "url", "link", "href", "detailurl"):
                    candidate.setdefault("link", v)
                elif lk in ("listingid", "listing_id", "id", "stocknumber") and "id" not in candidate:
                    candidate["id"] = v
                # VIN as a unique identifier
                elif lk == "vin":
                    candidate["vin"] = v

            if candidate.get("title") or (candidate.get("year") and candidate.get("make")):
                found.append(candidate)

        # Always recurse into values regardless of match
        for v in obj.values():
            found.extend(_walk_for_vehicle_listings(v, depth + 1, max_depth))

    elif isinstance(obj, list):
        for item in obj:
            found.extend(_walk_for_vehicle_listings(item, depth + 1, max_depth))

    return found


def _normalize(raw: dict, query: str) -> dict:
    """
    Produce a clean candidate dict in Vulture adapter shape, extended with
    mileage which is relevant for vehicle listings.
    """
    # Build title from parts if explicit title absent
    title: str | None = None
    if raw.get("title"):
        title = str(raw["title"]).strip()
    elif raw.get("year") or raw.get("make"):
        parts = [
            str(raw.get("year") or "").strip(),
            str(raw.get("make") or "").strip(),
            str(raw.get("model") or "").strip(),
            str(raw.get("trim") or "").strip(),
        ]
        title = " ".join(p for p in parts if p) or None

    # Price — accept int, float, or string like "$18,999"
    price: int | None = None
    raw_price = raw.get("price")
    if raw_price is not None:
        price_str = str(raw_price).replace(",", "").replace("$", "").strip()
        m = re.search(r"\d+", price_str)
        if m:
            try:
                price = int(m.group())
            except ValueError:
                pass

    # Mileage — similar parsing
    mileage: int | None = None
    raw_mileage = raw.get("mileage")
    if raw_mileage is not None:
        mileage_str = str(raw_mileage).replace(",", "").strip()
        m = re.search(r"\d+", mileage_str)
        if m:
            try:
                mileage = int(m.group())
            except ValueError:
                pass

    # Location — prefer "City, ST (X mi)" + dealer name; fallback to seller zip
    location: str | None = None
    city_state = str(raw.get("location") or "").strip()
    dealer_name = str(raw.get("dealer") or "").strip()
    seller_zip = str(raw.get("seller_zip") or "").strip()
    if city_state:
        location = f"{dealer_name} — {city_state}" if dealer_name else city_state
    elif dealer_name:
        location = dealer_name + (f" (zip {seller_zip})" if seller_zip else "")
    elif seller_zip:
        location = f"zip {seller_zip}"

    # Link — prefer absolute URLs, build from relative if needed
    link: str | None = None
    raw_link = str(raw.get("link") or "").strip()
    if raw_link:
        if raw_link.startswith("http"):
            link = raw_link
        elif raw_link.startswith("/"):
            link = urljoin(CARS_ORIGIN, raw_link)
    # Fallback: construct from listing id if present
    if not link and raw.get("id"):
        link = f"{CARS_ORIGIN}/vehicledetail/{raw['id']}/"

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

    Cars.com embeds a complete structured payload on each listing card:
      year, make, model, trim, vin, price, msrp, mileage, stockType,
      seller.zip, listingId, primaryThumbnail.

    Returns a flat dict, or {} if the attribute is absent or unparseable.
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

    year = str(d.get("year") or "").strip()
    make = str(d.get("make") or "").strip()
    model = str(d.get("model") or "").strip()
    trim = str(d.get("trim") or "").strip()
    stock_type = str(d.get("stockType") or "").strip()
    parts = [p for p in [stock_type, year, make, model, trim] if p]
    if parts:
        result["title"] = " ".join(parts)

    for price_key in ("price", "msrp"):
        val = d.get(price_key)
        if val and str(val) not in ("0", "0.0", ""):
            result["price"] = str(val)
            break

    raw_mileage = d.get("mileage")
    if raw_mileage and str(raw_mileage) != "0":
        result["mileage"] = str(raw_mileage)

    listing_id = d.get("listingId")
    if listing_id:
        result["listing_id"] = listing_id

    vin = d.get("vin")
    if vin:
        result["vin"] = vin

    seller_zip = (d.get("seller") or {}).get("zip")
    if seller_zip:
        result["seller_zip"] = seller_zip

    return result


def _try_dom_selectors(soup: BeautifulSoup, query: str) -> list[dict]:
    """
    Attempt extraction from server-rendered HTML.

    Strategy per card:
      1. Parse data-vehicle-details JSON for title, price, mileage, VIN.
      2. Use CSS selectors for link, location (city/state), dealer name.
      3. Pure CSS fallback for cards without the JSON attribute.
    """
    for selector in LISTING_SELECTORS:
        try:
            items = soup.select(selector)
        except Exception as exc:
            log.debug("Selector %r raised: %s", selector, exc)
            continue

        if not items:
            continue

        log.info("DOM selector matched: %r  (%d nodes)", selector, len(items))

        results = []
        for item in items[:MAX_LISTINGS]:
            raw: dict = {}

            # Pass 1: data-vehicle-details JSON
            json_data = _extract_vehicle_details_json(item)
            if json_data:
                raw.update(json_data)

            # Pass 2: DOM for link (not in JSON)
            if not raw.get("link"):
                for link_sel in FIELD_SELECTORS["link"]:
                    el = item.select_one(link_sel)
                    if el and el.get("href"):
                        raw["link"] = el["href"]
                        break
                if not raw.get("link"):
                    gallery = item.find("card-gallery")
                    if gallery and gallery.get("card-href"):
                        raw["link"] = gallery["card-href"]
                if not raw.get("link") and raw.get("listing_id"):
                    raw["link"] = f"{CARS_ORIGIN}/vehicledetail/{raw['listing_id']}/"

            # Pass 2: DOM for location — "City, ST (X mi)" in footer
            if not raw.get("location"):
                for loc_sel in FIELD_SELECTORS["location"]:
                    el = item.select_one(loc_sel)
                    if el:
                        text = el.get_text(" ", strip=True)
                        if text and "," in text and any(c.isalpha() for c in text):
                            raw["location"] = text
                            break

            # Pass 2: DOM for dealer name — span.fuse-body-small (weaker color)
            if not raw.get("dealer"):
                for dlr_sel in FIELD_SELECTORS["dealer"]:
                    for el in item.select(dlr_sel):
                        text = el.get_text(" ", strip=True)
                        if not text or len(text) <= 3:
                            continue
                        if any(tok in text for tok in ("$", "MSRP", "Est.", "/mo", "%")):
                            continue
                        if text.replace(".", "").replace(" ", "").isdigit():
                            continue
                        raw["dealer"] = text
                        break
                    if raw.get("dealer"):
                        break

            # Fallback CSS for fields still missing (cards without JSON attr)
            if not raw.get("title"):
                for title_sel in FIELD_SELECTORS["title"]:
                    el = item.select_one(title_sel)
                    if el:
                        raw["title"] = el.get_text(" ", strip=True)
                        break

            if not raw.get("price"):
                for price_sel in FIELD_SELECTORS["price"]:
                    el = item.select_one(price_sel)
                    if el:
                        raw["price"] = el.get_text(strip=True)
                        break

            if not raw.get("mileage"):
                for mileage_sel in FIELD_SELECTORS["mileage"]:
                    el = item.select_one(mileage_sel)
                    if el:
                        raw["mileage"] = el.get_text(strip=True)
                        break

            if raw:
                results.append(raw)

        if results:
            return results

    return []


def _try_inline_json_blobs(soup: BeautifulSoup) -> list[dict]:
    """
    Scan application/json script blobs for vehicle-shaped data.
    Cars.com may embed search-result data in inline JSON outside __NEXT_DATA__.
    """
    candidates: list[dict] = []
    blobs = soup.find_all("script", type="application/json")
    log.debug("Found %d application/json script blob(s)", len(blobs))

    for i, blob in enumerate(blobs[:10]):
        try:
            data = json.loads(blob.string or "")
            hits = _walk_for_vehicle_listings(data)
            if hits:
                log.info("Inline JSON blob #%d yielded %d candidate(s)", i, len(hits))
                candidates.extend(hits)
        except (json.JSONDecodeError, TypeError) as exc:
            log.debug("Inline JSON blob #%d parse error: %s", i, exc)

    return candidates


def _scan_inline_script_globals(html: str) -> list[dict]:
    """
    Regex-scan raw HTML for known Cars.com JavaScript global patterns
    that embed listing data as window.* assignments or similar constructs.

    Example patterns:
        window.__CARS_COM_INITIAL_STATE__ = {...}
        window.CarsState = {...}
        var initialState = {...}
    """
    patterns = [
        r"window\.__[A-Z_]+(?:STATE|DATA|STORE|PROPS|CONFIG)__\s*=\s*(\{.*?\});",
        r"window\.[A-Z][a-zA-Z]+State\s*=\s*(\{.*?\});",
        r"var\s+(?:initialState|pageData|searchResults)\s*=\s*(\{.*?\});",
        r'"listings"\s*:\s*(\[.*?\])',
        r'"vehicles"\s*:\s*(\[.*?\])',
        r'"searchResults"\s*:\s*(\[.*?\])',
    ]
    candidates: list[dict] = []
    for pat in patterns:
        for match in re.finditer(pat, html, re.DOTALL):
            raw_json = match.group(1)
            # Guard against enormous blobs
            if len(raw_json) > 2_000_000:
                log.debug("Skipping oversized match for pattern: %s", pat[:60])
                continue
            try:
                data = json.loads(raw_json)
                hits = _walk_for_vehicle_listings(data)
                if hits:
                    log.info(
                        "Inline script global pattern %r yielded %d candidate(s)",
                        pat[:50],
                        len(hits),
                    )
                    candidates.extend(hits)
            except (json.JSONDecodeError, TypeError):
                pass
    return candidates


# ---------------------------------------------------------------------------
# Main probe
# ---------------------------------------------------------------------------


def probe(query: str) -> None:
    sep = "=" * 72
    print(sep)
    print(f"  CARS.COM PROBE   query={query!r}")
    print(sep)

    # Build search URL — Cars.com accepts a `keyword` param for free-text search
    params = {
        "keyword": query,
        "stock_type": "all",      # new + used
        "maximum_distance": "all",
    }
    param_str = "&".join(f"{k}={quote_plus(str(v))}" for k, v in params.items())
    search_url = f"{BASE_URL}?{param_str}"

    print(f"\n  Target URL : {search_url}\n")

    # ------------------------------------------------------------------
    # Step 1 — HTTP fetch
    # ------------------------------------------------------------------
    print("--- Step 1: HTTP fetch ---")
    session = requests.Session()

    try:
        resp = session.get(search_url, headers=HEADERS, timeout=25, allow_redirects=True)
    except requests.exceptions.ConnectionError as exc:
        log.error("Connection failed: %s", exc)
        print("  => Cannot reach Cars.com. Network block or DNS failure.")
        _print_assessment(
            requests_viable=False,
            needs_browser=True,
            block_severity="unknown",
            block_detail="Connection refused / DNS error",
            listings_found=0,
        )
        return
    except requests.exceptions.Timeout:
        log.error("Request timed out after 25 s")
        print("  => Request timed out. Possible rate-limit or slow anti-bot challenge.")
        _print_assessment(
            requests_viable=False,
            needs_browser=True,
            block_severity="medium",
            block_detail="Timeout",
            listings_found=0,
        )
        return

    print(f"  HTTP status   : {resp.status_code}")
    print(f"  Final URL     : {resp.url}")
    print(f"  Content-Type  : {resp.headers.get('Content-Type', 'unknown')}")
    print(f"  Response size : {len(resp.content):,} bytes  /  {len(resp.text):,} chars")

    # Log redirect chain
    if resp.history:
        print(f"  Redirect chain ({len(resp.history)} hop(s)):")
        for r in resp.history:
            print(f"    {r.status_code} -> {r.url}")
    else:
        print("  No redirects.")

    # Notable response headers
    notable_hdrs = [
        "Server", "X-Powered-By", "CF-Ray", "X-Cache",
        "X-Amz-Cf-Id", "Via", "X-Frame-Options", "Content-Security-Policy",
        "Strict-Transport-Security", "Set-Cookie",
    ]
    print("  Notable headers:")
    for hdr in notable_hdrs:
        val = resp.headers.get(hdr)
        if val:
            # Truncate very long values (e.g. CSP) for readability
            display_val = val if len(val) < 120 else val[:117] + "..."
            print(f"    {hdr}: {display_val}")

    # Session cookies
    cookie_names = [c.name for c in session.cookies]
    if cookie_names:
        print(f"  Cookies set   : {cookie_names}")
    else:
        print("  Cookies set   : (none)")

    # Store for use in block-signal detection
    _cookie_names = cookie_names

    # Run block detection early so it's included in hard-block assessments
    _early_block_signals = _detect_block_signals(
        resp.text, dict(resp.headers), _cookie_names
    )
    if _early_block_signals:
        print(f"  Anti-bot platforms detected : {list(_early_block_signals.keys())}")

    if resp.status_code == 403:
        print("\n  RESULT: 403 Forbidden — IP or user-agent block in place.")
        _print_assessment(
            requests_viable=False,
            needs_browser=True,
            block_severity="high",
            block_detail=f"HTTP 403  platforms={list(_early_block_signals.keys()) or 'unknown'}",
            listings_found=0,
        )
        return
    if resp.status_code == 429:
        print("\n  RESULT: 429 Too Many Requests — rate-limit hit.")
        _print_assessment(
            requests_viable=False,
            needs_browser=True,
            block_severity="high",
            block_detail=f"HTTP 429  platforms={list(_early_block_signals.keys()) or 'unknown'}",
            listings_found=0,
        )
        return
    if resp.status_code not in (200, 206):
        print(f"\n  Non-200 status ({resp.status_code}). Stopping.")
        _print_assessment(
            requests_viable=False,
            needs_browser=True,
            block_severity="medium",
            block_detail=f"HTTP {resp.status_code}",
            listings_found=0,
        )
        return

    html = resp.text
    soup = BeautifulSoup(html, "lxml")

    # ------------------------------------------------------------------
    # Step 2 — Page title
    # ------------------------------------------------------------------
    print("\n--- Step 2: page title ---")
    page_title = soup.title.get_text(strip=True) if soup.title else "(no <title> tag)"
    print(f"  Title: {page_title}")

    # Login / CAPTCHA in title is a hard signal
    title_lower = page_title.lower()
    if any(w in title_lower for w in ("sign in", "log in", "login", "captcha", "verify")):
        log.warning("Login/captcha wall detected in page title.")
        _print_assessment(
            requests_viable=False,
            needs_browser=True,
            block_severity="high",
            block_detail=f"Login/captcha wall (title: {page_title!r})",
            listings_found=0,
        )
        return

    # ------------------------------------------------------------------
    # Step 3 — Anti-bot / blocking signals
    # ------------------------------------------------------------------
    print("\n--- Step 3: anti-bot / challenge detection ---")
    # Re-use the detection result already computed for the early-exit check above
    block_signals = _early_block_signals

    if block_signals:
        print("  BLOCK SIGNALS DETECTED:")
        for platform, signals in block_signals.items():
            print(f"    [{platform.upper()}] {signals}")
        block_severity = "high"
    else:
        print("  No known anti-bot platform fingerprints found.")
        block_severity = "none"

    # Soft signals: presence of challenge-related text in body
    soft_signals = []
    body_lower = html.lower()
    for phrase in ("please complete the security check", "prove you are human",
                   "unusual activity", "verify you are not a robot",
                   "browser check", "ddos protection by"):
        if phrase in body_lower:
            soft_signals.append(phrase)
    if soft_signals:
        print(f"  Soft challenge text found: {soft_signals}")
        if block_severity == "none":
            block_severity = "medium"

    # ------------------------------------------------------------------
    # Step 4 — JS / SPA rendering analysis
    # ------------------------------------------------------------------
    print("\n--- Step 4: SPA / JS rendering analysis ---")
    found_spa = _detect_spa_markers(html)
    if found_spa:
        print(f"  SPA markers    : {found_spa}")
    else:
        print("  SPA markers    : none detected")

    body_el = soup.body
    body_text = body_el.get_text(" ", strip=True) if body_el else ""
    body_word_count = len(body_text.split())
    script_count = len(soup.find_all("script"))
    inline_json_count = len(soup.find_all("script", type="application/json"))
    json_ld_count = len(soup.find_all("script", type="application/ld+json"))

    print(f"  Body word count : {body_word_count}")
    print(f"  <script> tags   : {script_count}")
    print(f"  application/json blobs : {inline_json_count}")
    print(f"  application/ld+json blobs : {json_ld_count}")

    if body_word_count < 80:
        print("  => Very thin body text. Likely a JS-only shell page.")
        js_shell = True
    elif found_spa:
        print("  => SPA markers present but body has content. May have SSR/hydration.")
        js_shell = False
    else:
        print("  => Body has substantial text. Likely server-rendered HTML.")
        js_shell = False

    # ------------------------------------------------------------------
    # Step 5 — JSON-LD structured data
    # ------------------------------------------------------------------
    print("\n--- Step 5: JSON-LD structured data ---")
    json_ld_objects = _extract_json_ld(soup)
    if json_ld_objects:
        print(f"  Found {len(json_ld_objects)} JSON-LD object(s).")
        for i, obj in enumerate(json_ld_objects[:3]):
            schema_type = obj.get("@type", "unknown")
            print(f"    [{i+1}] @type={schema_type!r}  keys={list(obj.keys())[:10]}")
    else:
        print("  No JSON-LD structured data found.")

    # ------------------------------------------------------------------
    # Step 6 — __NEXT_DATA__ JSON extraction
    # ------------------------------------------------------------------
    print("\n--- Step 6: __NEXT_DATA__ extraction ---")
    next_data = _extract_next_data(soup)
    json_candidates: list[dict] = []

    if next_data:
        print("  __NEXT_DATA__ found. Walking JSON tree for vehicle-shaped objects...")
        raw_hits = _walk_for_vehicle_listings(next_data)
        # Deduplicate by VIN first, then by title
        seen: set[str] = set()
        for h in raw_hits:
            key = str(h.get("vin") or h.get("title") or "")
            if key and key not in seen:
                seen.add(key)
                json_candidates.append(h)
        print(f"  Distinct vehicle-shaped objects : {len(json_candidates)}")
    else:
        print("  No __NEXT_DATA__ script tag found.")

    # ------------------------------------------------------------------
    # Step 7 — Inline JSON blob scan
    # ------------------------------------------------------------------
    print("\n--- Step 7: inline JSON blob scan ---")
    if not json_candidates:
        blob_hits = _try_inline_json_blobs(soup)
        if blob_hits:
            seen_keys: set[str] = set()
            for h in blob_hits:
                key = str(h.get("vin") or h.get("title") or "")
                if key and key not in seen_keys:
                    seen_keys.add(key)
                    json_candidates.append(h)
            print(f"  Inline JSON blobs yielded {len(json_candidates)} candidate(s).")
        else:
            print("  No vehicle candidates found in inline JSON blobs.")
    else:
        print("  Skipped (already have candidates from __NEXT_DATA__).")

    # ------------------------------------------------------------------
    # Step 8 — Inline script global regex scan
    # ------------------------------------------------------------------
    print("\n--- Step 8: inline script global variable scan ---")
    if not json_candidates:
        global_hits = _scan_inline_script_globals(html)
        if global_hits:
            seen_keys2: set[str] = set()
            for h in global_hits:
                key = str(h.get("vin") or h.get("title") or "")
                if key and key not in seen_keys2:
                    seen_keys2.add(key)
                    json_candidates.append(h)
            print(f"  Script global scan yielded {len(json_candidates)} candidate(s).")
        else:
            print("  No vehicle candidates found via script global scan.")
    else:
        print("  Skipped (already have candidates from JSON extraction).")

    # ------------------------------------------------------------------
    # Step 9 — DOM CSS selector extraction
    # ------------------------------------------------------------------
    print("\n--- Step 9: DOM CSS selector extraction ---")
    dom_candidates: list[dict] = []

    if js_shell:
        print("  Skipped — body is a JS shell. DOM selectors will find nothing useful.")
    else:
        dom_candidates = _try_dom_selectors(soup, query)
        if dom_candidates:
            print(f"  DOM extraction yielded {len(dom_candidates)} candidate(s).")
        else:
            print("  No CSS selectors matched. Probing selector presence individually:")
            for sel in LISTING_SELECTORS[:8]:
                try:
                    count = len(soup.select(sel))
                    if count:
                        print(f"    {sel!r}  -> {count} node(s)")
                except Exception:
                    pass

    # ------------------------------------------------------------------
    # Step 10 — Normalize and print best candidates
    # ------------------------------------------------------------------
    print("\n--- Step 10: normalized candidate listings ---")
    all_raw = json_candidates[:MAX_LISTINGS] or dom_candidates[:MAX_LISTINGS]

    listings_found = len(all_raw)
    if not all_raw:
        print("  No candidates extracted by any method.")
    else:
        source_label = "JSON" if json_candidates else "DOM"
        print(f"  Source: {source_label}. Showing up to {MAX_LISTINGS} result(s).\n")
        for i, raw in enumerate(all_raw, 1):
            norm = _normalize(raw, query)
            print(f"  [{i}]")
            for field, value in norm.items():
                print(f"       {field:<10}: {value!r}")
            print()

    # ------------------------------------------------------------------
    # Step 11 — Additional page-content diagnostics
    # ------------------------------------------------------------------
    print("--- Step 11: page-content diagnostics ---")

    # Count how many listing-related keywords appear in visible text
    listing_keyword_count = sum(
        1 for w in ("for sale", "miles", "dealer", "used", "new", "price", "msrp", "vin")
        if w in body_lower
    )
    print(f"  Listing-related keywords in body text : {listing_keyword_count}/8")

    # Check for pagination markers
    paging_signals = []
    for p in ("next page", "page 2", "pagination", 'aria-label="next"', "load more"):
        if p in body_lower:
            paging_signals.append(p)
    print(f"  Pagination signals : {paging_signals or 'none detected'}")

    # Check for zip-code / location prompts
    zip_prompts = []
    for z in ("enter zip", "your zip", "set location", "enter location"):
        if z in body_lower:
            zip_prompts.append(z)
    if zip_prompts:
        print(f"  Location/zip prompt detected: {zip_prompts}")
        print("    => Adapter may need to supply a zip code for localized results.")
    else:
        print("  No location/zip prompt detected.")

    # ------------------------------------------------------------------
    # Final assessment
    # ------------------------------------------------------------------
    needs_browser = js_shell or (block_severity in ("high",) and listings_found == 0)
    requests_viable = listings_found > 0 and block_severity in ("none", "medium")

    _print_assessment(
        requests_viable=requests_viable,
        needs_browser=needs_browser,
        block_severity=block_severity,
        block_detail=str(block_signals) if block_signals else "none",
        listings_found=listings_found,
        extra_notes=[
            f"SPA markers present: {bool(found_spa)} ({found_spa})",
            f"JS shell (thin body): {js_shell}",
            f"Body word count: {body_word_count}",
            f"JSON-LD objects: {len(json_ld_objects)}",
            f"__NEXT_DATA__ found: {next_data is not None}",
            f"Listing keyword density: {listing_keyword_count}/8",
        ],
    )


# ---------------------------------------------------------------------------
# Assessment printer
# ---------------------------------------------------------------------------


def _print_assessment(
    requests_viable: bool,
    needs_browser: bool,
    block_severity: str,
    block_detail: str,
    listings_found: int,
    extra_notes: list[str] | None = None,
) -> None:
    sep = "=" * 72
    print()
    print(sep)
    print("  FINAL ASSESSMENT")
    print(sep)
    print(f"  requests viable?              : {'YES' if requests_viable else 'NO'}")
    print(f"  browser automation required?  : {'YES' if needs_browser else 'UNKNOWN — test further'}")
    print(f"  anti-bot severity             : {block_severity.upper()}")
    print(f"  anti-bot detail               : {block_detail}")
    print(f"  candidate listings extracted  : {listings_found}")

    if extra_notes:
        print("  additional notes:")
        for note in extra_notes:
            print(f"    - {note}")

    print()
    if listings_found > 0 and requests_viable:
        suitability = "PROMISING — plain requests returned parseable listings."
        next_move = (
            "Validate across multiple queries and IPs. "
            "Check whether the DOM/JSON shape is stable across pages. "
            "Promote to adapters/carsdotcom.py returning list[Listing]."
        )
    elif listings_found > 0 and not requests_viable:
        suitability = "CONDITIONAL — listings extracted but anti-bot risk is present."
        next_move = (
            "Test from a residential IP. If results are consistent, "
            "promote with caution and add retry + backoff. "
            "Plan a Playwright fallback."
        )
    elif needs_browser:
        suitability = "HARD — page requires browser execution (JS shell or hard block)."
        next_move = (
            "Write a carsdotcom_playwright_probe.py using Playwright. "
            "Evaluate whether headless Chromium with stealth mode bypasses the block."
        )
    else:
        suitability = "UNCERTAIN — page loaded but no listings extracted."
        next_move = (
            "Inspect the raw HTML saved to disk (add --save-html flag). "
            "Identify the actual CSS class names used for listing cards "
            "and update LISTING_SELECTORS accordingly."
        )

    print(f"  suitability as stable adapter : {suitability}")
    print(f"  recommended next move         : {next_move}")
    print(sep)
    print()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python experiments/adapters/carsdotcom_probe.py <search term>")
        print('Examples:')
        print('  python experiments/adapters/carsdotcom_probe.py "toyota camry"')
        print('  python experiments/adapters/carsdotcom_probe.py "honda civic under 15000"')
        print('  python experiments/adapters/carsdotcom_probe.py "ford f-150"')
        sys.exit(1)

    search_query = " ".join(sys.argv[1:])
    probe(search_query)
