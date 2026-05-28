"""
Mercari candidate-source probe
===============================
Reconnaissance only. Does NOT write to SQLite, send Discord alerts,
or touch the production adapter registry.

Usage:
    python experiments/adapters/mercari_probe.py "rtx 3080"
    python experiments/adapters/mercari_probe.py "75 inch tv"
    python experiments/adapters/mercari_probe.py "nintendo switch"

Goal: determine whether Mercari is a viable future Vulture adapter for
general used-goods deals (electronics, gaming, home, etc.).

Probe strategy (progressive):
    Strategy A — plain requests + BeautifulSoup
    Strategy B — browser-like session with enriched headers + cookies
    Strategy C — lightweight Playwright check (isolated; not wired to production)
    Strategy D — requests-only direct GraphQL call to /v1/api
                 Uses CSRF token from /v1/initialize to bypass Socure JS gating.

Viability questions answered:
    1. Can results be fetched reliably with plain requests?
    2. Is content server-rendered or JS-rendered?
    3. Can we extract title / price / link / image / location?
    4. Does Mercari aggressively block scraping?
    5. Are listings stable enough for link-based dedupe?
    6. Would Playwright / cookies / anti-bot mitigation be required?
    7. Is Mercari realistic as stable, experimental, or not-worth-pursuing?
"""

import json
import re
import sys
import time
from urllib.parse import quote_plus

import requests
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

SEARCH_URL = "https://www.mercari.com/search/"

# Strategy A — minimal but plausible UA
HEADERS_PLAIN = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
}

# Strategy B — enriched browser-like headers that better mimic Chrome
HEADERS_BROWSER = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.6367.155 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Cache-Control": "max-age=0",
    "DNT": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
    "sec-ch-ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "Connection": "keep-alive",
}

# SPA / JS-framework fingerprints
SPA_MARKERS = [
    "__NEXT_DATA__",
    "__NUXT__",
    "data-reactroot",
    "__REDUX_STORE__",
    "window.__INITIAL_STATE__",
    "window.__PRELOADED_STATE__",
    "window.__APP_STATE__",
    "window.INITIAL_STATE",
]

# Known anti-bot service fingerprints
ANTIBOT_MARKERS = {
    "Cloudflare": [
        "cf-ray",              # response header
        "cloudflare",          # body text
        "cf-browser-verification",
        "checking your browser",
        "please wait while we check",
        "__cf_chl",
        "cf_chl_prog",
    ],
    "DataDome": [
        "datadome",
        "dd_referrer",
        "dd_cookie",
        "_dd_s",
    ],
    "Akamai": [
        "akamai",
        "_abck",
        "ak_bmsc",
    ],
    "reCAPTCHA": [
        "recaptcha",
        "www.google.com/recaptcha",
    ],
    "PerimeterX": [
        "perimeterx",
        "_pxhd",
        "_px3",
    ],
}

# CSS selectors likely to match Mercari listing cards
# Mercari uses React with class-hashed names; data-testid is more stable
LISTING_SELECTORS = [
    "[data-testid='ItemCell']",
    "[data-testid='item-cell']",
    "[data-testid='SearchResults'] li",
    "li[data-testid]",
    "[class*='ItemThumbnail']",
    "[class*='item-thumbnail']",
    "[class*='SearchResult']",
    "[class*='ProductThumbnail']",
    "[class*='merListItem']",
    "ul[class*='search'] li",
    "div[class*='list'] > div[class*='item']",
    "article",
]

MAX_DISPLAY = 5
REQUEST_TIMEOUT = 25

# URL patterns that flag a Playwright network call as worth capturing.
# Any URL containing at least one of these strings (case-insensitive) is kept.
XHR_CANDIDATE_PATTERNS = [
    "mercari.com",
    "mercdn.net",
    "/search",
    "/v1/",
    "/v2/",
    "graphql",
    "/items",
    "/entities",
]

# Extensions that are never data endpoints — excluded regardless of domain.
XHR_EXCLUDE_EXTENSIONS = frozenset({
    ".js", ".css", ".png", ".jpg", ".jpeg", ".gif", ".webp",
    ".svg", ".woff", ".woff2", ".ttf", ".otf", ".ico",
    ".map", ".ts", ".mp4", ".mp3",
})


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _log(level: str, msg: str) -> None:
    """Emit a tagged log line — mirrors the output format in the task spec."""
    print(f"[{level}] {msg}")


def _detect_antibot(html: str, headers: dict) -> list[str]:
    """Return list of detected anti-bot service signals."""
    signals = []
    html_lower = html.lower()
    headers_lower = {k.lower(): v.lower() for k, v in headers.items()}

    for service, markers in ANTIBOT_MARKERS.items():
        for marker in markers:
            m = marker.lower()
            if m in html_lower or m in str(headers_lower):
                signals.append(f"{service} ({marker})")
                break
    return signals


def _detect_spa_markers(html: str) -> list[str]:
    """Return SPA framework markers found in the page source."""
    return [m for m in SPA_MARKERS if m in html]


def _extract_next_data(soup: BeautifulSoup) -> dict | None:
    """Pull Next.js embedded JSON from the __NEXT_DATA__ script tag."""
    tag = soup.find("script", id="__NEXT_DATA__")
    if tag and tag.string:
        try:
            return json.loads(tag.string)
        except json.JSONDecodeError:
            return None
    return None


def _strip_xssi(text: str) -> str:
    """
    Strip any XSSI / protection prefix from a JSON response body.

    Handles multiple prefix forms:
      - )]}'\\n          (common Angular/Mercari XSSI prefix)
      - \\n)]}'           (prefix preceded by leading whitespace)
      - &&&START&&&\\n   (older Angular style)
      - any leading garbage before the first { or [

    Strategy: strip leading whitespace, check explicit prefixes, then fall
    back to scanning forward to the first character that can start a JSON
    value ({, [, ", digit, t/f/n for true/false/null).
    """
    # Strip leading whitespace that may precede the XSSI token
    stripped = text.lstrip(" \t\r\n")
    for prefix in (")]}'\n", ")]}'\r\n", ")]}'", "&&&START&&&\n"):
        if stripped.startswith(prefix):
            return stripped[len(prefix):].lstrip(" \t\r\n")

    # Fallback: find the first character that can legitimately open a JSON value
    for i, ch in enumerate(text[:40]):
        if ch in '{["0123456789tfn-':
            if i > 0:
                return text[i:]
            break  # already at position 0 — no stripping needed

    return text


def _extract_mercari_search_items(body: dict) -> list[dict]:
    """
    Navigate Mercari's searchFacetQuery response to the items list.

    Tries known paths in priority order:
        data.search.items          (observed in Playwright GET intercept)
        data.search.itemsList      (alternate field name variant)
        data.searchFacet.items     (older API variant)

    Mercari item IDs are strings starting with 'm' (e.g. 'm12345678').
    Category nav objects have integer IDs (4, 7, 10...).  We filter those
    out so only real listing objects are returned.
    """
    candidates: list[dict] = []
    data = body.get("data") if isinstance(body, dict) else None
    if not isinstance(data, dict):
        return candidates

    # Try known paths to items list
    for path in (
        ("search", "items"),
        ("search", "itemsList"),
        ("searchFacet", "items"),
    ):
        obj: object = data
        for key in path:
            obj = obj.get(key) if isinstance(obj, dict) else None
        if isinstance(obj, list) and obj:
            candidates = [i for i in obj if isinstance(i, dict)]
            break

    # Filter: keep objects that look like listings, not category nav nodes.
    # Real listings: id starts with 'm', have a numeric price, have thumbnails.
    # Category nodes: integer id, no price, no thumbnails.
    if candidates:
        real = [
            c for c in candidates
            if (isinstance(c.get("id"), str) and c["id"].startswith("m"))
            or (isinstance(c.get("price"), (int, float)) and c.get("price", 0) > 0)
            or bool(c.get("thumbnails"))
        ]
        if real:
            candidates = real

    return candidates


def _extract_inline_json_blobs(soup: BeautifulSoup) -> list[dict]:
    """Return parsed JSON from all application/json script tags."""
    blobs = []
    for tag in soup.find_all("script", type="application/json"):
        try:
            blobs.append(json.loads(tag.string or ""))
        except (json.JSONDecodeError, TypeError):
            pass
    return blobs


def _walk_for_listings(obj, depth: int = 0, max_depth: int = 10) -> list[dict]:
    """
    Recursively walk a JSON blob looking for objects shaped like marketplace
    listings (have title/name + price and/or a URL-like key).

    Mercari API shapes seen in the wild:
      {"id": "m123456789", "name": "RTX 3080 10GB", "price": 55000,
       "thumbnails": ["https://..."], "seller": {...}}
    """
    if depth > max_depth:
        return []
    results = []

    if isinstance(obj, dict):
        keys_lower = {k.lower() for k in obj}
        has_title = "name" in keys_lower or "title" in keys_lower
        has_price = "price" in keys_lower or "amount" in keys_lower
        has_id = "id" in keys_lower

        # Accept if it looks listing-shaped: (title/name) + (price or id)
        if has_title and (has_price or has_id):
            candidate: dict = {}
            for k, v in obj.items():
                lk = k.lower()
                if lk == "name" and "title" not in candidate:
                    candidate["title"] = v
                elif lk == "title":
                    candidate["title"] = v
                elif lk == "price":
                    candidate["price"] = v
                elif lk == "amount" and "price" not in candidate:
                    candidate["price"] = v
                elif lk == "id":
                    candidate["item_id"] = v
                elif lk in ("url", "link", "href"):
                    candidate["link"] = v
                elif lk == "thumbnails":
                    # Mercari stores thumbnail URLs as a list
                    if isinstance(v, list) and v:
                        candidate["image"] = v[0]
                elif lk in ("thumbnail", "image", "photo", "imageurl", "image_url"):
                    candidate.setdefault("image", v)
                elif lk in ("sellercity", "city", "region", "location"):
                    candidate.setdefault("location", v)
                elif lk == "seller" and isinstance(v, dict):
                    # Mercari seller object may carry location
                    seller_loc = v.get("sellercity") or v.get("region") or v.get("city")
                    if seller_loc:
                        candidate.setdefault("location", seller_loc)
            if candidate.get("title"):
                results.append(candidate)

        for v in obj.values():
            results.extend(_walk_for_listings(v, depth + 1, max_depth))

    elif isinstance(obj, list):
        for item in obj:
            results.extend(_walk_for_listings(item, depth + 1, max_depth))

    return results


def _try_dom_selectors(soup: BeautifulSoup) -> list[dict]:
    """Attempt CSS-selector extraction from the rendered HTML."""
    for selector in LISTING_SELECTORS:
        items = soup.select(selector)
        if items:
            _log("INFO", f"DOM selector matched: {selector!r} ({len(items)} nodes)")
            results = []
            for item in items[:MAX_DISPLAY]:
                title_el = item.select_one(
                    "h2, h3, [class*='title'], [class*='Title'], [class*='name'], [class*='Name']"
                )
                price_el = item.select_one(
                    "[class*='price'], [class*='Price'], [class*='amount'], [class*='Amount']"
                )
                link_el = item.select_one("a[href]")
                img_el = item.select_one("img")
                loc_el = item.select_one(
                    "[class*='location'], [class*='Location'], [class*='city'], [class*='seller']"
                )

                results.append({
                    "title": title_el.get_text(strip=True) if title_el else None,
                    "price": price_el.get_text(strip=True) if price_el else None,
                    "link": link_el.get("href") if link_el else None,
                    "image": img_el.get("src") or img_el.get("data-src") if img_el else None,
                    "location": loc_el.get_text(strip=True) if loc_el else None,
                })
            return results
    return []


def _xhr_is_candidate(url: str) -> bool:
    """
    Return True if a Playwright network URL looks like a Mercari data endpoint.
    Excludes obvious static asset extensions regardless of domain.
    """
    path = url.lower().split("?")[0]
    for ext in XHR_EXCLUDE_EXTENSIONS:
        if path.endswith(ext):
            return False
    full_lower = url.lower()
    return any(p in full_lower for p in XHR_CANDIDATE_PATTERNS)


def _normalize(raw: dict, query: str) -> dict:
    """Produce a normalized candidate dict in Vulture adapter shape."""
    title = str(raw.get("title") or raw.get("name") or "").strip() or None

    # Mercari stores prices as integers (JPY cents or USD cents); handle both
    raw_price = raw.get("price")
    price: int | None = None
    if raw_price is not None:
        price_str = str(raw_price).replace("$", "").replace(",", "").strip()
        m = re.search(r"\d+", price_str)
        if m:
            try:
                price = int(m.group())
            except ValueError:
                price = None

    # Construct canonical link from item_id if no explicit URL was found
    link = str(raw.get("link") or raw.get("url") or "").strip() or None
    if link and link.startswith("/"):
        link = "https://www.mercari.com" + link
    item_id = raw.get("item_id") or raw.get("id")
    if not link and item_id:
        link = f"https://www.mercari.com/item/{item_id}/"

    image = str(raw.get("image") or "").strip() or None
    location = str(raw.get("location") or "").strip() or None

    return {
        "source": "mercari",
        "query": query,
        "title": title,
        "price": price,
        "link": link,
        "image": image,
        "location": location,
    }


# api.mercari.com does not publicly resolve (confirmed on Raven residential IP).
# The real search XHR endpoint lives on www.mercari.com or a subdomain that
# Playwright XHR interception will discover at runtime.  No direct API probe.


# ---------------------------------------------------------------------------
# Strategy runners
# ---------------------------------------------------------------------------


def _strategy_a(query: str) -> tuple:
    """
    Strategy A: plain requests with a generic Chrome UA.
    Returns (status_code, html_or_err, response_headers, final_url, cookies).
    On network failure: (None, error_msg, None, url, None).
    """
    url = f"{SEARCH_URL}?keyword={quote_plus(query)}"
    try:
        session = requests.Session()
        resp = session.get(url, headers=HEADERS_PLAIN, timeout=REQUEST_TIMEOUT, allow_redirects=True)
        return resp.status_code, resp.text, dict(resp.headers), resp.url, session.cookies
    except requests.exceptions.ConnectionError as exc:
        return None, f"CONNECTION ERROR: {exc}", None, url, None
    except requests.exceptions.Timeout:
        return None, "TIMEOUT", None, url, None
    except requests.exceptions.RequestException as exc:
        return None, str(exc), None, url, None


def _strategy_b(query: str) -> tuple:
    """
    Strategy B: enriched browser-like session with Sec-Fetch-* headers and
    a warm-up request to mercari.com homepage before the search.
    Returns (status_code, html_or_err, response_headers, final_url, cookies).
    On network failure: (None, error_msg, None, url, None).
    """
    url = f"{SEARCH_URL}?keyword={quote_plus(query)}"
    try:
        session = requests.Session()
        # Warm-up visit to the homepage to establish session cookies
        try:
            session.get("https://www.mercari.com/", headers=HEADERS_BROWSER,
                        timeout=REQUEST_TIMEOUT, allow_redirects=True)
            time.sleep(1.0)
        except requests.exceptions.RequestException:
            pass  # If warm-up fails, continue anyway

        # Now fetch the search page with a Referer header
        search_headers = dict(HEADERS_BROWSER)
        search_headers["Referer"] = "https://www.mercari.com/"
        resp = session.get(url, headers=search_headers, timeout=REQUEST_TIMEOUT, allow_redirects=True)
        return resp.status_code, resp.text, dict(resp.headers), resp.url, session.cookies
    except requests.exceptions.ConnectionError as exc:
        return None, f"CONNECTION ERROR: {exc}", None, url, None
    except requests.exceptions.Timeout:
        return None, "TIMEOUT", None, url, None
    except requests.exceptions.RequestException as exc:
        return None, str(exc), None, url, None


def _strategy_d_direct_api(query: str) -> dict:
    """
    Strategy D: requests-only direct GraphQL call to www.mercari.com/v1/api.

    Hypothesis: Socure's bot detection is JavaScript-only. By making plain
    HTTP requests we never execute Socure's JS, so the fingerprint check
    never runs.  We still need Mercari's CSRF token and session cookies,
    which /v1/initialize hands out freely to any browser-like HTTP client.

    Steps:
        1. Warm-up GET to mercari.com homepage (establishes session + CF cookie).
        2. GET /v1/initialize → extract csrf token and accessToken.
        3. GET /v1/api with operationName + variables + extensions as query
           params (matches the intercepted URL shape from Playwright logs).
        4. POST /v1/api with JSON body (alternative GraphQL form).
        5. Walk any 200 JSON response for listing-shaped objects.
    """
    API_BASE = "https://www.mercari.com/v1/api"
    INIT_URL = "https://www.mercari.com/v1/initialize"
    # Persisted query hash observed in Playwright XHR intercept
    PERSISTED_HASH = "bc1eb4c4c2bb85e0e19b07c807570de0f5386c0fe770a43194c6e61b7af8c111"

    result: dict = {
        "csrf": None,
        "access_token": None,
        "is_bot": None,
        "cookie_names": [],
        "initialize_status": None,
        "get_status": None,
        "get_content_type": None,
        "get_size": 0,
        "get_top_keys": None,
        "get_candidates": 0,
        "post_status": None,
        "post_content_type": None,
        "post_size": 0,
        "post_top_keys": None,
        "post_candidates": 0,
        "raw_candidates": [],
        "extraction_source": None,
        "error": None,
    }

    # --- Session setup (warm-up mirrors Strategy B) -------------------------
    session = requests.Session()
    try:
        session.get(
            "https://www.mercari.com/",
            headers=HEADERS_BROWSER,
            timeout=REQUEST_TIMEOUT,
            allow_redirects=True,
        )
        time.sleep(1.0)
    except requests.exceptions.RequestException:
        pass

    result["cookie_names"] = [c.name for c in session.cookies]

    # --- Step 1: /v1/initialize — get CSRF token ----------------------------
    init_headers = dict(HEADERS_BROWSER)
    init_headers["Accept"] = "application/json, text/plain, */*"
    init_headers["Referer"] = "https://www.mercari.com/"
    try:
        init_resp = session.get(INIT_URL, headers=init_headers, timeout=REQUEST_TIMEOUT)
        result["initialize_status"] = init_resp.status_code
        if init_resp.status_code == 200:
            try:
                init_json = init_resp.json()
                result["csrf"] = init_json.get("csrf")
                result["is_bot"] = init_json.get("isBot")
                token = init_json.get("accessToken") or ""
                if len(str(token)) > 10:
                    result["access_token"] = token
            except (json.JSONDecodeError, ValueError):
                pass
    except requests.exceptions.RequestException as exc:
        result["error"] = f"/v1/initialize request failed: {exc}"
        return result

    # Update cookie list after initialize
    result["cookie_names"] = [c.name for c in session.cookies]

    # --- Build GraphQL variables (from observed intercepted request) ---------
    criteria = {
        "offset": 0,
        "soldItemsOffset": 0,
        "promotedItemsOffset": 0,
        "sortBy": 0,
        "length": 100,
        "query": query,
        "categoryIds": None,
        "brandIds": None,
        "itemConditions": [],
        "shippingPayerIds": [],
        "sizeGroupIds": [],
        "sizeIds": [],
        "itemStatuses": [],
        "customFacets": [],
        "facetTypes": [
            "category_ids_hierarchical", "brand_ids", "size_ids_hierarchical",
            "authenticity", "condition_ids", "item_status", "shipping_payer_ids",
            "meetup", "country_sources", "deals", "price",
        ],
        "authenticities": [],
        "deliveryType": "all",
        "state": None,
        "locale": None,
        "shopPageUri": None,
        "nationalShippingFeeMin": None,
        "nationalShippingFeeMax": None,
        "withCouponOnly": None,
        "excludeShippingTypes": None,
        "savedSearchId": None,
        "meetupDistanceLimit": None,
        "countrySources": [],
        "withDealsOnly": False,
        "showDescription": False,
    }

    feed_deals_criteria = dict(criteria)
    feed_deals_criteria.update({
        "sortBy": 9,
        "length": 20,
        "itemStatuses": [1, 2, 3],
        "withDealsOnly": True,
    })

    variables = {
        "withFeedLikes": False,
        "withFeedRecentlyViewed": False,
        "withFeedDeals": True,
        "feedDealsCriteria": feed_deals_criteria,
        "criteria": criteria,
    }

    extensions = {
        "persistedQuery": {
            "version": 1,
            "sha256Hash": PERSISTED_HASH,
        }
    }

    # --- Shared API headers --------------------------------------------------
    api_headers = dict(HEADERS_BROWSER)
    api_headers["Accept"] = "application/json"
    # Exclude Brotli: requests has no built-in Brotli decompressor, so
    # advertising 'br' causes the server to send compressed binary that
    # resp.text / resp.content.decode() cannot handle.
    api_headers["Accept-Encoding"] = "gzip, deflate"
    api_headers["Origin"] = "https://www.mercari.com"
    api_headers["Referer"] = f"https://www.mercari.com/search/?keyword={quote_plus(query)}"
    if result["csrf"]:
        api_headers["x-csrf-token"] = result["csrf"]
    if result["access_token"]:
        api_headers["Authorization"] = f"Bearer {result['access_token']}"

    def _parse_response(resp: requests.Response, label: str) -> list[dict]:
        """Record metadata and return listing candidates from resp."""
        status = resp.status_code
        ct = resp.headers.get("content-type", resp.headers.get("Content-Type", ""))
        # Decode bytes explicitly so we can strip XSSI before parsing
        try:
            raw_text = resp.content.decode("utf-8", errors="replace")
        except Exception:
            raw_text = resp.text
        size = len(raw_text)
        result[f"{label}_status"] = status
        result[f"{label}_content_type"] = ct
        result[f"{label}_size"] = size
        candidates: list[dict] = []
        # Always capture raw prefix for diagnostics
        result[f"{label}_raw_prefix"] = repr(raw_text[:80])

        if "json" in ct.lower() and status == 200:
            try:
                clean = _strip_xssi(raw_text)
                result[f"{label}_clean_prefix"] = repr(clean[:40])
                body = json.loads(clean)
                if isinstance(body, dict):
                    result[f"{label}_top_keys"] = list(body.keys())[:14]
                elif isinstance(body, list):
                    result[f"{label}_top_keys"] = f"[array len={len(body)}]"
                # Capture data.* sub-keys for path diagnostics
                if isinstance(body, dict) and "data" in body and isinstance(body["data"], dict):
                    result[f"{label}_data_sub_keys"] = list(body["data"].keys())[:12]

                # Try the specific Mercari items path first; fall back to generic walk
                candidates = _extract_mercari_search_items(body) if isinstance(body, dict) else []
                if not candidates:
                    raw = _walk_for_listings(body)
                    # Filter walk results: exclude category-nav objects (integer IDs, no price/thumbnails)
                    real = [
                        c for c in raw
                        if (isinstance(c.get("item_id"), str) and c["item_id"].startswith("m"))
                        or (c.get("price") is not None and c.get("price") != 0)
                        or bool(c.get("image"))
                    ]
                    candidates = real if real else raw
                result[f"{label}_candidates"] = len(candidates)
                result[f"{label}_sample_titles"] = [
                    str(c.get("name") or c.get("title") or "")[:60]
                    for c in candidates[:5]
                ]
            except (json.JSONDecodeError, ValueError) as exc:
                result[f"{label}_json_error"] = str(exc)
        elif status not in (200,):
            # Capture error body for non-200 responses
            result[f"{label}_error_body"] = raw_text[:300]
        return candidates

    # --- Step 2: GET /v1/api (simplified variables matching Playwright GET) --
    # The working Playwright GET used withFeedDeals=false, feedDealsCriteria=null.
    # The full feedDealsCriteria causes a 400 in GET form.
    # GET also requires Content-Type: application/json as a CSRF signal
    # (Mercari blocks GET /v1/api without it — confirmed from 400 error body).
    get_variables = {
        "withFeedLikes": False,
        "withFeedRecentlyViewed": False,
        "withFeedDeals": False,
        "feedDealsCriteria": None,
        "criteria": criteria,
    }
    get_headers = dict(api_headers)
    get_headers["Content-Type"] = "application/json"
    try:
        get_params = {
            "operationName": "searchFacetQuery",
            "variables": json.dumps(get_variables, separators=(",", ":")),
            "extensions": json.dumps(extensions, separators=(",", ":")),
        }
        get_resp = session.get(
            API_BASE, params=get_params, headers=get_headers, timeout=REQUEST_TIMEOUT
        )
        cands = _parse_response(get_resp, "get")
        if cands:
            result["raw_candidates"] = cands
            result["extraction_source"] = "get"
    except requests.exceptions.RequestException as exc:
        result["get_error"] = str(exc)

    # --- Step 3: POST /v1/api (JSON body form) — only if GET failed ---------
    if not result["raw_candidates"]:
        try:
            post_headers = dict(api_headers)
            post_headers["Content-Type"] = "application/json"
            post_body = {
                "operationName": "searchFacetQuery",
                "variables": variables,
                "extensions": extensions,
            }
            post_resp = session.post(
                API_BASE, json=post_body, headers=post_headers, timeout=REQUEST_TIMEOUT
            )
            cands = _parse_response(post_resp, "post")
            if cands:
                result["raw_candidates"] = cands
                result["extraction_source"] = "post"
        except requests.exceptions.RequestException as exc:
            result["post_error"] = str(exc)

    return result


def _strategy_c_playwright(query: str) -> dict:
    """
    Strategy C: headless Playwright probe with broadened XHR interception.
    Isolated to this function — not wired into production runtime.

    Intercepts ALL Playwright network responses matching XHR_CANDIDATE_PATTERNS
    (mercari.com, mercdn.net, /search, /v1/, /v2/, graphql, /items, /entities).
    For each intercepted call records: method, HTTP status, URL, content-type,
    response size, JSON top-level keys, shallow JSON sample, and any
    listing-shaped objects found by recursive walk.
    """
    result = {
        "available": False,
        "status": None,
        "title": None,
        "final_url": None,
        "body_word_count": None,
        "found_markers": [],
        "candidate_count": 0,
        "raw_candidates": [],
        "next_data_found": False,
        "intercepted_calls": [],   # full metadata for every captured call
        "error": None,
    }
    try:
        from playwright.sync_api import sync_playwright  # noqa: PLC0415
    except ImportError:
        result["error"] = "playwright not installed"
        return result

    result["available"] = True
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(
                user_agent=HEADERS_BROWSER["User-Agent"],
                locale="en-US",
                viewport={"width": 1280, "height": 800},
                extra_http_headers={
                    "Accept-Language": "en-US,en;q=0.9",
                    "DNT": "1",
                },
            )
            page = context.new_page()

            intercepted_calls: list[dict] = []

            def _on_response(response):
                url = response.url
                if not _xhr_is_candidate(url):
                    return
                ct = response.headers.get("content-type", "")
                entry: dict = {
                    "method": response.request.method,
                    "status": response.status,
                    "url": url,
                    "content_type": ct,
                    "size": 0,
                    "json_top_keys": None,
                    "json_sample": None,
                    "candidates": [],
                    "error": None,
                }
                try:
                    body_text = response.text()
                    entry["size"] = len(body_text)
                    if "json" in ct.lower():
                        try:
                            body = json.loads(body_text)
                            # Top-level structure
                            if isinstance(body, dict):
                                entry["json_top_keys"] = list(body.keys())[:14]
                                sample: dict = {}
                                for k, v in list(body.items())[:4]:
                                    if isinstance(v, list):
                                        sample[k] = f"[list len={len(v)}]"
                                    elif isinstance(v, dict):
                                        sample[k] = {kk: "..." for kk in list(v.keys())[:6]}
                                    else:
                                        sample[k] = v
                                entry["json_sample"] = sample
                            elif isinstance(body, list):
                                entry["json_top_keys"] = f"[array len={len(body)}]"
                                if body and isinstance(body[0], dict):
                                    entry["json_sample"] = {"[0] keys": list(body[0].keys())[:10]}
                            # Recursive listing-shaped object search
                            entry["candidates"] = _walk_for_listings(body)
                        except (json.JSONDecodeError, Exception) as exc:
                            entry["error"] = f"JSON parse: {exc}"
                except Exception as exc:
                    entry["error"] = f"Body read: {exc}"

                intercepted_calls.append(entry)

            page.on("response", _on_response)

            nav_url = f"{SEARCH_URL}?keyword={quote_plus(query)}"
            resp = page.goto(nav_url, wait_until="domcontentloaded", timeout=45_000)
            result["status"] = resp.status if resp else None
            result["final_url"] = page.url

            # Wait for network to settle so XHR listing calls complete
            try:
                page.wait_for_load_state("networkidle", timeout=12_000)
            except Exception:
                pass

            result["title"] = page.title()
            content = page.content()
            soup = BeautifulSoup(content, "lxml")

            body_text_dom = soup.body.get_text(" ", strip=True) if soup.body else ""
            result["body_word_count"] = len(body_text_dom.split())
            result["found_markers"] = _detect_spa_markers(content)
            result["intercepted_calls"] = intercepted_calls

            # Collect listing candidates from intercepted JSON calls.
            # Prefer data.search.items (clean search results) over the generic
            # walker which picks up navigation/category noise from large POSTs.
            all_xhr_candidates: list[dict] = []
            for call in intercepted_calls:
                body = call.get("body")
                if isinstance(body, dict):
                    specific = _extract_mercari_search_items(body)
                    if specific:
                        all_xhr_candidates.extend(specific)
                        continue
                all_xhr_candidates.extend(call.get("candidates", []))

            if all_xhr_candidates:
                seen_xhr: set[str] = set()
                unique_xhr: list[dict] = []
                for rc in all_xhr_candidates:
                    t = str(rc.get("title") or "")
                    if t and t not in seen_xhr:
                        seen_xhr.add(t)
                        unique_xhr.append(rc)
                result["raw_candidates"] = unique_xhr
                result["candidate_count"] = len(unique_xhr)
                result["extraction_source"] = "intercepted_xhr"

            # Fall back to __NEXT_DATA__ from the rendered DOM
            if not result["raw_candidates"]:
                next_data = _extract_next_data(soup)
                if next_data:
                    result["next_data_found"] = True
                    result["next_data_top_keys"] = list(next_data.keys()) if isinstance(next_data, dict) else []
                    raw_nd = _walk_for_listings(next_data)
                    seen_nd: set[str] = set()
                    unique_nd: list[dict] = []
                    for rc in raw_nd:
                        t = str(rc.get("title") or "")
                        if t and t not in seen_nd:
                            seen_nd.add(t)
                            unique_nd.append(rc)
                    result["raw_candidates"] = unique_nd
                    result["candidate_count"] = len(unique_nd)
                    result["extraction_source"] = "next_data"
                    if not unique_nd and isinstance(next_data, dict):
                        nd_sample: dict = {}
                        for k, v in list(next_data.items())[:5]:
                            if isinstance(v, dict):
                                nd_sample[k] = {kk: "..." for kk in list(v.keys())[:6]}
                            elif isinstance(v, list):
                                nd_sample[k] = f"[list len={len(v)}]"
                            else:
                                nd_sample[k] = v
                        result["next_data_sample"] = nd_sample
                else:
                    blobs = _extract_inline_json_blobs(soup)
                    blob_cands: list[dict] = []
                    for blob in blobs[:8]:
                        blob_cands.extend(_walk_for_listings(blob))
                    result["raw_candidates"] = blob_cands
                    result["candidate_count"] = len(blob_cands)
                    result["extraction_source"] = "json_blobs"

            # CSS selector count — diagnostic only when JSON extraction failed
            if result["candidate_count"] == 0:
                for selector in LISTING_SELECTORS:
                    try:
                        count = len(page.query_selector_all(selector))
                        if count > 0:
                            result["candidate_count"] = count
                            result["matched_selector"] = selector
                            result["extraction_source"] = "css_selector"
                            break
                    except Exception:
                        pass

            browser.close()
    except Exception as exc:
        result["error"] = str(exc)

    return result


# ---------------------------------------------------------------------------
# Main probe
# ---------------------------------------------------------------------------


def probe(query: str) -> None:
    separator = "=" * 70
    print(separator)
    print(f"MERCARI PROBE  query={query!r}")
    print(separator)

    # -----------------------------------------------------------------------
    # Strategy A — plain requests
    # -----------------------------------------------------------------------
    print(f"\n{'─' * 60}")
    _log("INFO", "Strategy A: plain requests")
    print(f"{'─' * 60}")

    status_a, html_or_err_a, headers_a, final_url_a, cookies_a = _strategy_a(query)

    if status_a is None:
        _log("ERROR", f"Strategy A failed: {html_or_err_a}")
        html_a = None
    else:
        html_a = html_or_err_a
        _log("INFO", f"HTTP {status_a}")
        _log("INFO", f"Final URL: {final_url_a}")
        ct = headers_a.get("Content-Type", headers_a.get("content-type", "unknown"))
        _log("INFO", f"Content-Type: {ct}")
        _log("INFO", f"Response size: {len(html_a):,} chars")

        if cookies_a:
            cookie_names = [c.name for c in cookies_a]
            _log("INFO", f"Cookies set: {cookie_names}")

        # Anti-bot check
        antibot_signals = _detect_antibot(html_a, headers_a)
        if antibot_signals:
            for sig in antibot_signals:
                _log("WARN", f"Anti-bot signal detected: {sig}")
        else:
            _log("INFO", "No known anti-bot markers detected")

        if status_a in (403, 429):
            _log("WARN", f"HTTP {status_a} — IP/bot block likely")
        elif status_a == 401:
            _log("WARN", "401 Unauthorized — login required at HTTP layer")
        elif status_a not in (200, 206):
            _log("WARN", f"Non-200 status ({status_a})")

    # -----------------------------------------------------------------------
    # Strategy B — browser-like session
    # -----------------------------------------------------------------------
    print(f"\n{'─' * 60}")
    _log("INFO", "Strategy B: browser-like session (warm-up + enriched headers)")
    print(f"{'─' * 60}")

    status_b, html_or_err_b, headers_b, final_url_b, cookies_b = _strategy_b(query)

    if status_b is None:
        _log("ERROR", f"Strategy B failed: {html_or_err_b}")
        html_b = None
    else:
        html_b = html_or_err_b
        _log("INFO", f"HTTP {status_b}")
        _log("INFO", f"Final URL: {final_url_b}")
        ct_b = headers_b.get("Content-Type", headers_b.get("content-type", "unknown"))
        _log("INFO", f"Content-Type: {ct_b}")
        _log("INFO", f"Response size: {len(html_b):,} chars")

        if cookies_b:
            cookie_names_b = [c.name for c in cookies_b]
            _log("INFO", f"Cookies set: {cookie_names_b}")

        antibot_b = _detect_antibot(html_b, headers_b)
        if antibot_b:
            for sig in antibot_b:
                _log("WARN", f"Anti-bot signal detected: {sig}")
        else:
            _log("INFO", "No known anti-bot markers detected")

        if status_b in (403, 429):
            _log("WARN", f"HTTP {status_b} — browser session did not bypass block")

    # Choose the best HTML for further analysis (prefer 200, prefer B)
    if status_b == 200 and html_b:
        html = html_b
        status = status_b
        _log("INFO", "Using Strategy B response for analysis")
    elif status_a == 200 and html_a:
        html = html_a
        status = status_a
        _log("INFO", "Using Strategy A response for analysis")
    else:
        html = html_a or html_b
        status = status_a or status_b

    if not html or status not in (200, 206):
        _log("WARN", "No usable HTML from requests strategies — analysis limited")
        html = html or ""

    soup = BeautifulSoup(html, "lxml") if html else None

    # -----------------------------------------------------------------------
    # Page title + server-render heuristics
    # -----------------------------------------------------------------------
    print(f"\n{'─' * 60}")
    _log("INFO", "Rendering analysis")
    print(f"{'─' * 60}")

    if soup:
        page_title = soup.title.get_text(strip=True) if soup.title else None
        _log("INFO", f"Search page title: {page_title or '(none)'}")

        if page_title:
            lower_title = page_title.lower()
            if any(w in lower_title for w in ("sign in", "log in", "login", "captcha")):
                _log("WARN", "Login-wall or CAPTCHA page detected in title")

        spa_markers = _detect_spa_markers(html)
        if spa_markers:
            for m in spa_markers:
                _log("INFO", f"SPA marker found: {m}")
            _log("WARN", "Listings appear JS-rendered (SPA detected)")
        else:
            _log("INFO", "No SPA markers found — content may be server-rendered")

        body_text = soup.body.get_text(" ", strip=True) if soup.body else ""
        body_word_count = len(body_text.split())
        _log("INFO", f"Body word count (visible text): {body_word_count}")
        if body_word_count < 150:
            _log("WARN", "Very thin visible body — likely a JS-only shell")
        else:
            _log("INFO", "Body has substantial visible text")

        script_count = len(soup.find_all("script"))
        json_blob_count = len(soup.find_all("script", type="application/json"))
        _log("INFO", f"<script> tags: {script_count}  |  application/json blobs: {json_blob_count}")

        # Check for Mercari's React root
        react_root = soup.find(id="__NEXT_DATA__") or soup.find("div", id="root") or soup.find("div", id="app")
        if react_root:
            _log("INFO", f"React/SPA root element found: id={react_root.get('id')!r}")
    else:
        _log("WARN", "No HTML to parse — rendering analysis skipped")
        body_word_count = 0
        json_blob_count = 0

    # -----------------------------------------------------------------------
    # JSON data extraction (Strategy A/B result)
    # -----------------------------------------------------------------------
    print(f"\n{'─' * 60}")
    _log("INFO", "Embedded JSON extraction (__NEXT_DATA__ / application/json blobs)")
    print(f"{'─' * 60}")

    json_candidates: list[dict] = []

    if soup:
        next_data = _extract_next_data(soup)
        if next_data:
            _log("INFO", "__NEXT_DATA__ found — walking JSON tree for listing-shaped objects")
            raw = _walk_for_listings(next_data)
            seen: set[str] = set()
            for rc in raw:
                t = str(rc.get("title") or "")
                if t and t not in seen:
                    seen.add(t)
                    json_candidates.append(rc)
            _log("INFO", f"Distinct listing-shaped objects found: {len(json_candidates)}")
        else:
            _log("INFO", "No __NEXT_DATA__ tag — trying inline application/json blobs")
            for blob in _extract_inline_json_blobs(soup)[:8]:
                raw = _walk_for_listings(blob)
                json_candidates.extend(raw)
            if json_candidates:
                _log("INFO", f"Listing-shaped objects from JSON blobs: {len(json_candidates)}")
            else:
                _log("WARN", "No listing data found in inline JSON blobs")

    # -----------------------------------------------------------------------
    # DOM selector extraction (fallback)
    # -----------------------------------------------------------------------
    print(f"\n{'─' * 60}")
    _log("INFO", "DOM selector extraction")
    print(f"{'─' * 60}")

    dom_candidates: list[dict] = []
    if soup and body_word_count >= 150:
        dom_candidates = _try_dom_selectors(soup)
        if dom_candidates:
            _log("INFO", f"DOM extraction yielded {len(dom_candidates)} raw candidates")
        else:
            _log("WARN", "No CSS selectors matched — DOM extraction failed")
    elif soup:
        _log("INFO", "DOM extraction skipped (body too thin — JS shell)")
    else:
        _log("INFO", "DOM extraction skipped (no HTML)")

    # -----------------------------------------------------------------------
    # Strategy D — direct requests GraphQL call (bypass Socure JS)
    # -----------------------------------------------------------------------
    print(f"\n{'─' * 60}")
    _log("INFO", "Strategy D: direct requests GraphQL — www.mercari.com/v1/api")
    print(f"{'─' * 60}")
    _log("INFO", "Hypothesis: Socure fingerprints only JS execution — plain HTTP bypasses it")

    d = _strategy_d_direct_api(query)

    _log("INFO", f"/v1/initialize HTTP {d.get('initialize_status')}")
    _log("INFO", f"CSRF token obtained: {'YES' if d.get('csrf') else 'NO'}")
    _log("INFO", f"accessToken obtained: {'YES (len=%d)' % len(str(d['access_token'])) if d.get('access_token') else 'NO'}")
    _log("INFO", f"isBot from /v1/initialize: {d.get('is_bot')}")
    _log("INFO", f"Session cookie names: {d.get('cookie_names', [])}")

    if d.get("error"):
        _log("WARN", f"Strategy D setup error: {d['error']}")
    else:
        # GET attempt
        get_st = d.get("get_status")
        get_ct = d.get("get_content_type", "")
        get_sz = d.get("get_size", 0)
        get_nc = d.get("get_candidates", 0)
        get_keys = d.get("get_top_keys")
        if get_st:
            _log("INFO", f"GET /v1/api: HTTP {get_st}  {get_sz:,} bytes  ct={get_ct}")
            if get_keys:
                _log("INFO", f"GET /v1/api JSON top-level keys: {get_keys}")
            data_sub = d.get("get_data_sub_keys")
            if data_sub:
                _log("INFO", f"GET /v1/api data.* sub-keys: {data_sub}")
            if get_nc:
                _log("INFO", f"Listing-shaped objects from GET: {get_nc}")
                titles = d.get("get_sample_titles", [])
                if titles:
                    _log("INFO", f"GET sample titles: {titles}")
            if get_st == 403:
                _log("WARN", "GET /v1/api 403 — CSRF + session not sufficient for GET")
            if get_st == 400:
                _log("WARN", "GET /v1/api 400 — bad request (variables shape mismatch)")
            if d.get("get_json_error"):
                _log("WARN", f"GET JSON parse error: {d['get_json_error']}")
                _log("INFO", f"GET raw prefix: {d.get('get_raw_prefix')}")
                _log("INFO", f"GET clean prefix: {d.get('get_clean_prefix')}")
            if d.get("get_error_body"):
                _log("INFO", f"GET error body: {d['get_error_body'][:200]}")

        # POST attempt
        post_st = d.get("post_status")
        post_ct = d.get("post_content_type", "")
        post_sz = d.get("post_size", 0)
        post_nc = d.get("post_candidates", 0)
        post_keys = d.get("post_top_keys")
        if post_st:
            _log("INFO", f"POST /v1/api: HTTP {post_st}  {post_sz:,} bytes  ct={post_ct}")
            if post_keys:
                _log("INFO", f"POST /v1/api JSON top-level keys: {post_keys}")
            if post_nc:
                _log("INFO", f"Listing-shaped objects from POST: {post_nc}")
                titles = d.get("post_sample_titles", [])
                if titles:
                    _log("INFO", f"POST sample titles: {titles}")
            if post_st == 403:
                _log("WARN", "POST /v1/api 403 — CSRF + session not sufficient for POST")
            if d.get("post_json_error"):
                _log("WARN", f"POST JSON parse error: {d['post_json_error']}")
                _log("INFO", f"POST raw prefix: {d.get('post_raw_prefix')}")
                _log("INFO", f"POST clean prefix: {d.get('post_clean_prefix')}")
            if d.get("post_error_body"):
                _log("INFO", f"POST error body: {d['post_error_body'][:200]}")

        # Verdict
        d_raw = d.get("raw_candidates", [])
        if d_raw:
            _log("INFO", f"Strategy D SUCCESS — extracted {len(d_raw)} listing candidates (source: {d.get('extraction_source')})")
            if not json_candidates:
                json_candidates = d_raw
        else:
            both_403 = (get_st == 403 or not get_st) and (post_st == 403 or not post_st)
            if both_403:
                _log("WARN", "Strategy D BLOCKED — /v1/api returns 403 for both GET and POST")
                _log("WARN", "Socure gates the GraphQL endpoint server-side, not just via JS")
                _log("INFO", f"CSRF present: {'YES' if d.get('csrf') else 'NO'}")
                _log("INFO", f"accessToken present: {'YES' if d.get('access_token') else 'NO'}")
                _log("INFO", f"Cookie names: {d.get('cookie_names', [])}")
            else:
                _log("WARN", "Strategy D: no listing candidates extracted")

    # -----------------------------------------------------------------------
    # Strategy C — Playwright with broadened XHR interception (isolated)
    # -----------------------------------------------------------------------
    print(f"\n{'─' * 60}")
    _log("INFO", "Strategy C: Playwright probe with broadened XHR interception")
    print(f"{'─' * 60}")
    _log("INFO", f"XHR filter patterns: {XHR_CANDIDATE_PATTERNS}")

    # Re-evaluate — Strategy D may have found candidates
    requests_got_listings = bool(json_candidates or dom_candidates)
    if requests_got_listings and body_word_count >= 150:
        _log("INFO", "Requests strategies yielded candidates — skipping Playwright")
        playwright_result = {"available": False, "skipped": True}
    else:
        _log("INFO", "Launching Playwright (headless Chromium + networkidle wait)...")
        playwright_result = _strategy_c_playwright(query)

        if not playwright_result.get("available"):
            err = playwright_result.get("error", "unknown")
            _log("WARN", f"Playwright not available: {err}")
        elif playwright_result.get("error"):
            _log("WARN", f"Playwright error: {playwright_result['error']}")
        else:
            pw_status = playwright_result.get("status")
            pw_title = playwright_result.get("title")
            pw_words = playwright_result.get("body_word_count", 0)
            pw_markers = playwright_result.get("found_markers", [])
            pw_next_data = playwright_result.get("next_data_found", False)
            pw_raw = playwright_result.get("raw_candidates", [])
            pw_selector = playwright_result.get("matched_selector")
            extraction_src = playwright_result.get("extraction_source", "none")

            _log("INFO", f"Playwright HTTP {pw_status}")
            _log("INFO", f"Playwright page title: {pw_title or '(none)'}")
            _log("INFO", f"Playwright body word count: {pw_words}")
            if pw_markers:
                _log("INFO", f"SPA markers after JS execution: {pw_markers}")
            if pw_words > body_word_count:
                _log("INFO", f"JS hydration added ~{pw_words - body_word_count} words — page is JS-rendered")

            # ── Intercepted XHR calls ──────────────────────────────────────
            calls = playwright_result.get("intercepted_calls", [])
            if calls:
                _log("INFO", f"Intercepted {len(calls)} candidate network call(s):")
                print()
                for call in calls:
                    method = call.get("method", "?")
                    st = call.get("status", "?")
                    url = call.get("url", "?")
                    ct = call.get("content_type", "")
                    sz = call.get("size", 0)
                    n_cands = len(call.get("candidates", []))
                    print(f"    [{method}] HTTP {st}  {sz:,} bytes  ct={ct}")
                    print(f"    URL: {url}")
                    top_keys = call.get("json_top_keys")
                    if top_keys:
                        print(f"    JSON top-level keys: {top_keys}")
                    jsampl = call.get("json_sample")
                    if jsampl:
                        print(f"    JSON shallow sample:")
                        for k, v in jsampl.items():
                            print(f"      {k!r}: {v}")
                    if n_cands:
                        print(f"    Listing-shaped objects found: {n_cands}")
                    if call.get("error"):
                        print(f"    Error: {call['error']}")
                    print()
            else:
                _log("WARN", "No candidate XHR calls intercepted")
                _log("INFO", "Possible reasons: auth-gated endpoint, different domain pattern, lazy-load not triggered")

            # ── __NEXT_DATA__ shell info ───────────────────────────────────
            if pw_next_data:
                _log("INFO", "__NEXT_DATA__ present in rendered DOM (thin shell confirmed)")
                nd_keys = playwright_result.get("next_data_top_keys", [])
                if nd_keys:
                    _log("INFO", f"__NEXT_DATA__ top-level keys: {nd_keys}")
                nd_sample = playwright_result.get("next_data_sample")
                if nd_sample:
                    _log("INFO", "__NEXT_DATA__ shallow structure:")
                    for k, v in nd_sample.items():
                        print(f"      {k!r}: {v}")

            # ── Candidate summary ──────────────────────────────────────────
            if pw_raw:
                _log("INFO", f"Playwright extracted {len(pw_raw)} listing-shaped objects (source: {extraction_src})")
                if not json_candidates:
                    json_candidates = pw_raw
            elif pw_selector:
                _log("INFO", f"Playwright CSS selector matched {playwright_result.get('candidate_count',0)} nodes: {pw_selector!r}")
            else:
                _log("WARN", "Playwright: 0 listing-shaped objects found")

    # -----------------------------------------------------------------------
    # Normalize and print candidates
    # -----------------------------------------------------------------------
    print(f"\n{'─' * 60}")
    _log("INFO", "Normalized candidate objects")
    print(f"{'─' * 60}")

    # json_candidates may have been updated by Playwright; re-evaluate
    all_raw = (json_candidates or dom_candidates)[:MAX_DISPLAY]

    if not all_raw:
        _log("WARN", "No candidates extracted by any strategy")
    else:
        source_label = "JSON/API" if json_candidates else "DOM"
        _log("INFO", f"Source: {source_label}. Showing up to {MAX_DISPLAY} results.\n")
        for i, raw in enumerate(all_raw, 1):
            norm = _normalize(raw, query)
            print(f"  [{i}] {json.dumps(norm, ensure_ascii=False, indent=6)}")

    # -----------------------------------------------------------------------
    # Dedupe stability check
    # -----------------------------------------------------------------------
    print(f"\n{'─' * 60}")
    _log("INFO", "Dedupe stability assessment")
    print(f"{'─' * 60}")

    if all_raw:
        candidates_with_links = [r for r in all_raw if _normalize(r, query).get("link")]
        link_ratio = len(candidates_with_links) / len(all_raw)
        _log("INFO", f"Candidates with stable links: {len(candidates_with_links)}/{len(all_raw)} ({link_ratio:.0%})")
        if link_ratio >= 0.8:
            _log("INFO", "Links appear stable — link-based dedupe is viable")
        else:
            _log("WARN", "Many candidates lack links — dedupe reliability uncertain")
    else:
        _log("WARN", "No candidates to assess for dedupe stability")

    # -----------------------------------------------------------------------
    # Login / session signals
    # -----------------------------------------------------------------------
    print(f"\n{'─' * 60}")
    _log("INFO", "Login / session signals")
    print(f"{'─' * 60}")

    if html:
        login_signals = []
        html_lower = html.lower()
        if "sign in" in html_lower or "log in" in html_lower:
            login_signals.append("'sign in' / 'log in' text in HTML")
        if "create account" in html_lower or "register" in html_lower:
            login_signals.append("'create account' / 'register' text in HTML")
        if soup and soup.find("input", {"type": "password"}):
            login_signals.append("password <input> field found in DOM")
        if login_signals:
            for sig in login_signals:
                _log("INFO", f"Login signal: {sig}")
            _log("INFO", "Login prompts present but may be incidental (not a hard gate)")
        else:
            _log("INFO", "No explicit login-wall signals detected")
    else:
        _log("WARN", "No HTML — login signal check skipped")

    # -----------------------------------------------------------------------
    # Viability summary
    # -----------------------------------------------------------------------
    print(f"\n{'─' * 60}")
    _log("INFO", "Viability summary")
    print(f"{'─' * 60}")

    # Infer viability from probe results
    requests_ok = status in (200, 206) if status else False
    got_candidates = bool(all_raw)
    antibot_signals_a = _detect_antibot(html_a or "", headers_a or {}) if status_a else []
    antibot_signals_b = _detect_antibot(html_b or "", headers_b or {}) if status_b else []
    antibot_present = bool(antibot_signals_a or antibot_signals_b)

    d_got = bool(d.get("raw_candidates"))
    d_blocked = (
        (d.get("get_status") == 403 or not d.get("get_status"))
        and (d.get("post_status") == 403 or not d.get("post_status"))
    )
    playwright_needed = (
        not got_candidates
        and not d_got
        and playwright_result.get("candidate_count", 0) == 0
    )

    _log("INFO", f"requests-only fetch works (search page):  {'YES' if requests_ok else 'NO'}")
    _log("INFO", f"Content appears server-rendered:          {'YES' if (requests_ok and body_word_count >= 150) else 'NO — JS shell'}")
    _log("INFO", f"Strategy D (direct GraphQL) succeeded:    {'YES' if d_got else 'NO'}")
    _log("INFO", f"Strategy D /v1/api blocked (403):         {'YES' if d_blocked else 'NO or not tried'}")
    _log("INFO", f"Candidates extracted by any strategy:     {'YES' if got_candidates else 'NO'}")
    _log("INFO", f"Anti-bot / blocking detected:             {'YES' if antibot_present else 'NOT DETECTED'}")
    _log("INFO", f"Playwright appears required:              {'YES' if playwright_needed else 'NO'}")

    if d_got and not d_blocked:
        classification = "EXPERIMENTAL CANDIDATE (requests-only GraphQL)"
        next_step = (
            "Build adapters/mercari.py using Strategy D pattern: "
            "warm-up → /v1/initialize (CSRF) → POST /v1/api GraphQL"
        )
    elif got_candidates and requests_ok and not antibot_present:
        classification = "STABLE CANDIDATE"
        next_step = "Promote to adapters/mercari.py"
    elif got_candidates and requests_ok:
        classification = "EXPERIMENTAL ONLY"
        next_step = "Build experimental adapter; monitor block rate over time"
    elif playwright_result.get("candidate_count", 0) > 0:
        classification = "EXPERIMENTAL ONLY (requires Playwright)"
        next_step = "Implement Playwright-backed adapter under experiments/"
    elif d_blocked:
        classification = "NOT VIABLE WITHOUT ANTI-BOT TOOLING"
        next_step = (
            "Socure gates /v1/api server-side even for plain requests. "
            "Would require Playwright stealth (playwright-stealth / rebrowser) "
            "or a Mercari account session. Consider abandoning for now."
        )
    else:
        classification = "INVESTIGATE FURTHER"
        next_step = "Review raw responses manually; re-run from Raven"

    _log("INFO", f"Recommended classification: {classification}")
    _log("INFO", f"Recommended next step:      {next_step}")

    print()
    print(separator)
    print("PROBE COMPLETE")
    print(separator)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python experiments/adapters/mercari_probe.py <search term>")
        print('Example: python experiments/adapters/mercari_probe.py "rtx 3080"')
        print('Example: python experiments/adapters/mercari_probe.py "nintendo switch"')
        sys.exit(1)

    search_query = " ".join(sys.argv[1:])
    probe(search_query)
