"""
OfferUp candidate-source probe
==============================
Reconnaissance only. Does NOT write to SQLite, send Discord alerts,
or touch the production adapter registry.

Usage:
    python experiments/adapters/offerup_probe.py "rtx 3080"
    python experiments/adapters/offerup_probe.py "75 inch tv"
    python experiments/adapters/offerup_probe.py "toyota sequoia"

Goal: determine whether OfferUp is a viable future Vulture adapter for
general local marketplace deals.
"""

import json
import re
import sys
from urllib.parse import quote_plus

import requests
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

BASE_URL = "https://offerup.com/search/"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "DNT": "1",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}

# SPA fingerprints to scan for
SPA_MARKERS = [
    "__NEXT_DATA__",
    "__NUXT__",
    "data-reactroot",
    "__REDUX_STORE__",
    "window.__INITIAL_STATE__",
    "window.__PRELOADED_STATE__",
    "window.__APP_STATE__",
]

# CSS selectors to probe (best-guess; may not survive DOM changes)
LISTING_SELECTORS = [
    "[data-testid='listing-card']",
    "[data-testid='item-card']",
    ".listing-card",
    ".item-tile",
    "li[class*='item']",
    "div[class*='listing']",
    "div[class*='ItemCard']",
    "div[class*='item-card']",
]

MAX_LISTINGS = 5


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _detect_spa_markers(text: str) -> list[str]:
    found = []
    for marker in SPA_MARKERS:
        if marker in text:
            found.append(marker)
    return found


def _extract_next_data(soup: BeautifulSoup) -> dict | None:
    """Pull Apollo/Next.js embedded JSON from __NEXT_DATA__ script tag."""
    tag = soup.find("script", id="__NEXT_DATA__")
    if tag and tag.string:
        try:
            return json.loads(tag.string)
        except json.JSONDecodeError:
            return None
    return None


def _walk_for_listings(obj, depth=0, max_depth=8) -> list[dict]:
    """
    Recursively walk a parsed JSON blob looking for objects that look like
    marketplace listings (have title/price/location/link-ish keys).

    Handles the OfferUp __NEXT_DATA__ shape where listings look like:
      {__typename: "ModularFeedListing", listingId: "...", title: "...",
       price: "550", locationName: "Arlington, VA", ...}
    as well as generic shapes with name/url/location keys.
    """
    if depth > max_depth:
        return []
    results = []
    if isinstance(obj, dict):
        keys = {k.lower() for k in obj}
        # OfferUp-specific: ModularFeedListing shape
        is_offerup_listing = "listingid" in keys and "title" in keys
        # Generic fallback: has a title/name + price or url
        is_generic_listing = ("title" in keys or "name" in keys) and (
            "price" in keys or "url" in keys or "id" in keys
        )

        if is_offerup_listing or is_generic_listing:
            candidate = {}
            for k, v in obj.items():
                lk = k.lower()
                if lk == "title":
                    candidate["title"] = v
                elif lk == "name" and "title" not in candidate:
                    candidate["title"] = v
                elif lk == "price":
                    candidate["price"] = v
                elif lk == "locationname":
                    candidate["location"] = v
                elif lk in ("location", "city", "area") and "location" not in candidate:
                    candidate["location"] = v
                elif lk == "listingid":
                    candidate["listing_id"] = v
                elif lk in ("url", "link", "href", "listing_url"):
                    candidate["link"] = v
                elif lk == "id" and "listing_id" not in candidate:
                    candidate.setdefault("listing_id", v)
            if candidate.get("title"):
                results.append(candidate)
        for v in obj.values():
            results.extend(_walk_for_listings(v, depth + 1, max_depth))
    elif isinstance(obj, list):
        for item in obj:
            results.extend(_walk_for_listings(item, depth + 1, max_depth))
    return results


def _normalize(raw: dict, query: str) -> dict:
    """Produce a clean candidate dict in Vulture adapter shape."""
    title = str(raw.get("title") or raw.get("name") or "").strip()

    raw_price = raw.get("price")
    price = None
    if raw_price is not None:
        price_str = str(raw_price)
        m = re.search(r"[\d,]+", price_str.replace("$", ""))
        if m:
            try:
                price = int(m.group().replace(",", ""))
            except ValueError:
                price = None

    location = str(raw.get("location") or raw.get("city") or raw.get("area") or "").strip() or None

    link = str(raw.get("link") or raw.get("url") or raw.get("href") or "").strip() or None
    if link and link.startswith("/"):
        link = "https://offerup.com" + link

    # OfferUp listing URLs follow /item/detail/<listingId>/
    listing_id = raw.get("listing_id") or raw.get("id")
    if not link and listing_id:
        link = f"https://offerup.com/item/detail/{listing_id}/"

    return {
        "source": "offerup",
        "query": query,
        "title": title or None,
        "price": price,
        "location": location,
        "link": link,
    }


def _try_dom_selectors(soup: BeautifulSoup) -> list[dict]:
    """Attempt CSS-selector extraction from server-rendered HTML."""
    for selector in LISTING_SELECTORS:
        items = soup.select(selector)
        if items:
            print(f"  DOM selector matched: {selector!r} ({len(items)} nodes)")
            results = []
            for item in items[:MAX_LISTINGS]:
                title_el = item.select_one("h2, h3, [class*='title'], [class*='Title']")
                price_el = item.select_one("[class*='price'], [class*='Price']")
                link_el = item.select_one("a[href]")
                loc_el = item.select_one("[class*='location'], [class*='Location'], [class*='city']")

                results.append({
                    "title": title_el.get_text(strip=True) if title_el else None,
                    "price": price_el.get_text(strip=True) if price_el else None,
                    "location": loc_el.get_text(strip=True) if loc_el else None,
                    "link": link_el.get("href") if link_el else None,
                })
            return results
    return []


# ---------------------------------------------------------------------------
# Main probe
# ---------------------------------------------------------------------------


def probe(query: str) -> None:
    separator = "=" * 70
    print(separator)
    print(f"OFFERUP PROBE  query={query!r}")
    print(separator)

    search_url = f"{BASE_URL}?q={quote_plus(query)}"
    print(f"\nTarget URL : {search_url}")

    # ------------------------------------------------------------------
    # Step 1 — requests fetch
    # ------------------------------------------------------------------
    print("\n--- Step 1: requests fetch ---")
    try:
        session = requests.Session()
        resp = session.get(search_url, headers=HEADERS, timeout=20, allow_redirects=True)
    except requests.exceptions.ConnectionError as exc:
        print(f"  BLOCKED / CONNECTION ERROR: {exc}")
        print("  => Cannot reach OfferUp. Possible block or network restriction.")
        return
    except requests.exceptions.Timeout:
        print("  TIMEOUT after 20 s.")
        return

    print(f"  HTTP status  : {resp.status_code}")
    print(f"  Final URL    : {resp.url}")
    print(f"  Content-Type : {resp.headers.get('Content-Type', 'unknown')}")
    print(f"  Response size: {len(resp.text):,} chars")

    if resp.status_code == 403:
        print("\n  RESULT: 403 Forbidden — bot/IP block in place.")
        print("  => Browser automation (with residential proxy) likely required.")
        return
    if resp.status_code == 401:
        print("\n  RESULT: 401 Unauthorized — login required at the HTTP layer.")
        return
    if resp.status_code == 302 or str(resp.url) != search_url:
        print(f"\n  Redirected to: {resp.url}")
        if "login" in str(resp.url).lower() or "signin" in str(resp.url).lower():
            print("  RESULT: Login gate detected via redirect.")
            return

    if resp.status_code not in (200, 206):
        print(f"\n  Non-200 status ({resp.status_code}). Stopping.")
        return

    html = resp.text
    soup = BeautifulSoup(html, "lxml")

    # ------------------------------------------------------------------
    # Step 2 — page title
    # ------------------------------------------------------------------
    print("\n--- Step 2: page title ---")
    page_title = soup.title.get_text(strip=True) if soup.title else None
    print(f"  Title: {page_title or '(none)'}")

    # Sanity-check for login wall in title
    if page_title and any(w in page_title.lower() for w in ("sign in", "log in", "login")):
        print("  RESULT: Login-wall page detected in title.")
        return

    # ------------------------------------------------------------------
    # Step 3 — SPA marker detection
    # ------------------------------------------------------------------
    print("\n--- Step 3: SPA / rendering detection ---")
    found_markers = _detect_spa_markers(html)
    if found_markers:
        print(f"  SPA markers found: {found_markers}")
        print("  => Page is JavaScript-rendered (SPA). requests gets a shell.")
    else:
        print("  No known SPA markers found.")

    # Heuristic: if <body> has very little visible text, it's JS-rendered
    body_text = soup.body.get_text(" ", strip=True) if soup.body else ""
    body_word_count = len(body_text.split())
    print(f"  Body word count (visible text): {body_word_count}")
    if body_word_count < 100:
        print("  => Very thin body text — likely a JS-only shell.")

    # Count <script> tags as another heuristic
    script_count = len(soup.find_all("script"))
    print(f"  <script> tag count: {script_count}")

    # Check for inline JSON blobs
    inline_json_blobs = soup.find_all("script", type="application/json")
    print(f"  application/json script blobs: {len(inline_json_blobs)}")

    # ------------------------------------------------------------------
    # Step 4 — try __NEXT_DATA__ extraction
    # ------------------------------------------------------------------
    print("\n--- Step 4: __NEXT_DATA__ / embedded JSON extraction ---")
    next_data = _extract_next_data(soup)
    json_candidates: list[dict] = []

    if next_data:
        print("  __NEXT_DATA__ found. Walking JSON tree for listing-shaped objects...")
        raw_candidates = _walk_for_listings(next_data)
        # Deduplicate by title
        seen_titles: set[str] = set()
        for rc in raw_candidates:
            t = str(rc.get("title") or "")
            if t and t not in seen_titles:
                seen_titles.add(t)
                json_candidates.append(rc)
        print(f"  Distinct listing-shaped objects found: {len(json_candidates)}")
    else:
        print("  No __NEXT_DATA__ tag (or unparseable). Trying inline JSON blobs...")
        for blob in inline_json_blobs[:5]:
            try:
                blob_data = json.loads(blob.string or "")
                raw_candidates = _walk_for_listings(blob_data)
                json_candidates.extend(raw_candidates)
            except (json.JSONDecodeError, TypeError):
                pass
        if json_candidates:
            print(f"  Listing-shaped objects from inline blobs: {len(json_candidates)}")
        else:
            print("  No listing data found in inline JSON blobs.")

    # ------------------------------------------------------------------
    # Step 5 — DOM selector fallback
    # ------------------------------------------------------------------
    print("\n--- Step 5: DOM selector extraction ---")
    dom_candidates: list[dict] = []
    if body_word_count >= 100:
        dom_candidates = _try_dom_selectors(soup)
        if dom_candidates:
            print(f"  DOM extraction yielded {len(dom_candidates)} raw candidates.")
        else:
            print("  No known CSS selectors matched.")
    else:
        print("  Skipped (body too thin — JS shell).")

    # ------------------------------------------------------------------
    # Step 6 — normalize and print candidates
    # ------------------------------------------------------------------
    print("\n--- Step 6: normalized candidate dictionaries ---")
    all_raw = json_candidates[:MAX_LISTINGS] or dom_candidates[:MAX_LISTINGS]

    if not all_raw:
        print("  No candidates extracted.")
    else:
        source_label = "JSON" if json_candidates else "DOM"
        print(f"  Source: {source_label}. Showing up to {MAX_LISTINGS} results.\n")
        for i, raw in enumerate(all_raw, 1):
            norm = _normalize(raw, query)
            print(f"  [{i}] {json.dumps(norm, ensure_ascii=False, indent=4)}")

    # ------------------------------------------------------------------
    # Step 7 — login/session signal check
    # ------------------------------------------------------------------
    print("\n--- Step 7: login / session signals ---")
    login_signals = []
    if "log in" in html.lower() or "sign in" in html.lower():
        login_signals.append("'log in' / 'sign in' text present in HTML")
    if soup.find("input", {"type": "password"}):
        login_signals.append("password input field found")
    cookie_names = [c.name for c in session.cookies]
    if cookie_names:
        print(f"  Cookies set by server: {cookie_names}")
    if login_signals:
        print(f"  Login signals: {login_signals}")
    else:
        print("  No explicit login-wall signals detected.")

    print()
    print(separator)
    print("PROBE COMPLETE")
    print(separator)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python experiments/adapters/offerup_probe.py <search term>")
        print('Example: python experiments/adapters/offerup_probe.py "rtx 3080"')
        sys.exit(1)

    search_query = " ".join(sys.argv[1:])
    probe(search_query)
