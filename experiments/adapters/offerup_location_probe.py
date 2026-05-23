"""
experiments/adapters/offerup_location_probe.py

OfferUp Location-Targeting Probe
=================================
Reconnaissance only. Does NOT write to SQLite, send Discord alerts,
or touch the production adapter registry.

Goal
----
Determine whether OfferUp's search results can be geographically controlled
through any combination of:
  - URL query parameters  (?lat=, ?lng=, ?location=, ?zip=, ?radius=,
                           ?location_slug=, ?city=)
  - Path parameters       (/search/<city-slug>?q=...)
  - Session cookies       (set a location cookie before search)
  - Custom request headers

For each strategy we:
  1. Fetch the page
  2. Check HTTP status and final URL
  3. Detect __NEXT_DATA__ presence
  4. Extract any location-related values from the JSON tree
  5. Collect up to 5 listing locations from ModularFeedListing nodes
  6. Print a verdict

Usage
-----
    python experiments/adapters/offerup_location_probe.py
    python experiments/adapters/offerup_location_probe.py --query "75 inch tv"
    python experiments/adapters/offerup_location_probe.py --query "toyota sequoia"

All output goes to stdout only. No file I/O.
"""

import argparse
import json
import re
import sys
from urllib.parse import quote_plus, urlencode

import requests
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_SEARCH_BASE = "https://offerup.com/search"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "DNT": "1",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}

_TIMEOUT = 25

# Cities under test with their known coordinates and zip codes
_CITIES = {
    "houston": {
        "label": "Houston, TX",
        "lat": 29.7604,
        "lng": -95.3698,
        "zip": "77001",
        "slug": "houston-tx",
        "location_str": "Houston, TX",
    },
    "dallas": {
        "label": "Dallas, TX",
        "lat": 32.7767,
        "lng": -96.7970,
        "zip": "75201",
        "slug": "dallas-tx",
        "location_str": "Dallas, TX",
    },
    "arlington_va": {
        "label": "Arlington, VA",
        "lat": 38.8816,
        "lng": -77.0910,
        "zip": "22201",
        "slug": "arlington-va",
        "location_str": "Arlington, VA",
    },
}

_EXPECTED_TYPENAME = "ModularFeedListing"
_MAX_LISTINGS = 5


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sep(char: str = "=", width: int = 72) -> str:
    return char * width


def _fetch(url: str, session: requests.Session,
           extra_headers: dict | None = None,
           cookies: dict | None = None) -> tuple[requests.Response | None, str]:
    """
    Return (response, error_message). error_message is empty on success.
    """
    headers = {**_HEADERS, **(extra_headers or {})}
    try:
        resp = session.get(
            url, headers=headers, timeout=_TIMEOUT,
            allow_redirects=True, cookies=cookies or {},
        )
    except requests.exceptions.ConnectionError as exc:
        return None, f"ConnectionError: {exc}"
    except requests.exceptions.Timeout:
        return None, f"Timeout after {_TIMEOUT}s"
    except requests.exceptions.RequestException as exc:
        return None, f"RequestException: {exc}"

    if resp.status_code == 403:
        return None, "HTTP 403 – possible IP/bot block"
    if resp.status_code not in (200, 206):
        return None, f"HTTP {resp.status_code}"
    if "login" in str(resp.url).lower() or "signin" in str(resp.url).lower():
        return None, f"Redirected to login page: {resp.url}"
    return resp, ""


def _extract_next_data(html: str) -> dict | None:
    soup = BeautifulSoup(html, "lxml")
    tag = soup.find("script", id="__NEXT_DATA__")
    if not tag or not tag.string:
        return None
    try:
        return json.loads(tag.string)
    except json.JSONDecodeError:
        return None


def _collect_feed_listings(obj: object, depth: int = 0, max_depth: int = 12) -> list[dict]:
    """Recursively walk __NEXT_DATA__ and return all ModularFeedListing nodes."""
    if depth > max_depth:
        return []
    found: list[dict] = []
    if isinstance(obj, dict):
        if obj.get("__typename") == _EXPECTED_TYPENAME:
            found.append(obj)
        else:
            for v in obj.values():
                found.extend(_collect_feed_listings(v, depth + 1, max_depth))
    elif isinstance(obj, list):
        for item in obj:
            found.extend(_collect_feed_listings(item, depth + 1, max_depth))
    return found


def _find_location_values(obj: object, depth: int = 0, max_depth: int = 12,
                          _found: list | None = None) -> list[tuple[str, object]]:
    """
    Walk the JSON tree and return (key_path, value) pairs for any key that
    contains 'location', 'city', 'state', 'lat', 'lng', 'lon', 'zip',
    'postal', or 'radius' (case-insensitive), skipping deep duplication.
    """
    if _found is None:
        _found = []
    if depth > max_depth:
        return _found
    _LOC_KEYS = frozenset({
        "location", "city", "state", "lat", "lng", "lon", "latitude", "longitude",
        "zip", "postal", "radius", "locationname", "locationslug",
        "location_slug", "geo", "area", "region",
    })
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k.lower() in _LOC_KEYS and not isinstance(v, (dict, list)):
                _found.append((k, v))
            else:
                _find_location_values(v, depth + 1, max_depth, _found)
    elif isinstance(obj, list):
        for item in obj:
            _find_location_values(item, depth + 1, max_depth, _found)
    return _found


def _listing_locations(nodes: list[dict]) -> list[str]:
    locs: list[str] = []
    for n in nodes[:_MAX_LISTINGS]:
        loc = str(n.get("locationName") or "").strip()
        if loc:
            locs.append(loc)
    return locs


def _verdict(observed_locs: list[str], expected_label: str) -> str:
    if not observed_locs:
        return "UNKNOWN (no location data in results)"
    # Check if *most* observed locations match the expected city/state
    expected_parts = [p.strip().lower() for p in expected_label.split(",")]
    match_count = sum(
        1 for loc in observed_locs
        if any(part in loc.lower() for part in expected_parts)
    )
    ratio = match_count / len(observed_locs)
    if ratio >= 0.6:
        return f"MATCHES TARGET ({match_count}/{len(observed_locs)} listing locations match {expected_label!r})"
    else:
        return f"DOES NOT MATCH ({match_count}/{len(observed_locs)} match; got {sorted(set(observed_locs))})"


# ---------------------------------------------------------------------------
# Probe strategies
# ---------------------------------------------------------------------------


def _run_strategy(label: str, url: str, session: requests.Session,
                  city_info: dict, extra_headers: dict | None = None,
                  cookies: dict | None = None) -> dict:
    """
    Execute one location strategy. Returns a result dict with:
      label, url, status, next_data_found, location_values, listing_count,
      listing_locations, verdict
    """
    print(f"\n  Strategy: {label}")
    print(f"  URL: {url}")

    resp, err = _fetch(url, session, extra_headers=extra_headers, cookies=cookies)
    if resp is None:
        print(f"  ERROR: {err}")
        return {"label": label, "url": url, "error": err}

    print(f"  HTTP {resp.status_code} | final URL: {resp.url}")

    next_data = _extract_next_data(resp.text)
    has_next_data = next_data is not None
    print(f"  __NEXT_DATA__ found: {has_next_data}")

    if not has_next_data:
        print("  WARNING: No __NEXT_DATA__ — cannot extract listings or location values")
        return {
            "label": label, "url": url, "status": resp.status_code,
            "next_data_found": False, "listing_count": 0,
            "listing_locations": [], "verdict": "UNKNOWN (no __NEXT_DATA__)",
        }

    # Collect location-related fields from the JSON tree (top-level props)
    loc_values = _find_location_values(next_data)
    # Deduplicate while preserving order
    seen: set = set()
    unique_loc_values: list[tuple[str, object]] = []
    for k, v in loc_values:
        key = (k.lower(), str(v))
        if key not in seen:
            seen.add(key)
            unique_loc_values.append((k, v))

    if unique_loc_values:
        print(f"  Location-related values in JSON ({len(unique_loc_values)} unique):")
        for k, v in unique_loc_values[:20]:
            print(f"    {k} = {v!r}")
        if len(unique_loc_values) > 20:
            print(f"    ... and {len(unique_loc_values) - 20} more")
    else:
        print("  No location-related values found in JSON")

    # Collect listing nodes
    nodes = _collect_feed_listings(next_data)
    observed_locs = _listing_locations(nodes)
    print(f"  Listing nodes found: {len(nodes)} | showing {len(observed_locs)} locations")
    if observed_locs:
        for i, loc in enumerate(observed_locs, 1):
            print(f"    [{i}] {loc}")

    verdict = _verdict(observed_locs, city_info["label"])
    print(f"  Verdict: {verdict}")

    return {
        "label": label,
        "url": url,
        "status": resp.status_code,
        "next_data_found": True,
        "location_values": unique_loc_values,
        "listing_count": len(nodes),
        "listing_locations": observed_locs,
        "verdict": verdict,
    }


def probe_city(city_key: str, city_info: dict, query: str) -> list[dict]:
    """Run all location strategies for one city against the given query."""
    q = quote_plus(query)
    results: list[dict] = []

    session = requests.Session()

    print(f"\n{_sep()}")
    print(f"CITY: {city_info['label']}  |  query={query!r}")
    print(_sep())

    # ------------------------------------------------------------------
    # 1. Baseline – no location param (what GeoIP gives us)
    # ------------------------------------------------------------------
    url = f"{_SEARCH_BASE}?q={q}"
    results.append(_run_strategy(
        "1. Baseline (no location param)", url, session, city_info
    ))

    # ------------------------------------------------------------------
    # 2. lat/lng query params
    # ------------------------------------------------------------------
    params = urlencode({"q": query, "lat": city_info["lat"], "lng": city_info["lng"]})
    url = f"{_SEARCH_BASE}?{params}"
    results.append(_run_strategy(
        "2. lat/lng params", url, session, city_info
    ))

    # ------------------------------------------------------------------
    # 3. lat/lng + radius (30 miles)
    # ------------------------------------------------------------------
    params = urlencode({
        "q": query, "lat": city_info["lat"], "lng": city_info["lng"], "radius": 30
    })
    url = f"{_SEARCH_BASE}?{params}"
    results.append(_run_strategy(
        "3. lat/lng + radius=30", url, session, city_info
    ))

    # ------------------------------------------------------------------
    # 4. location string param
    # ------------------------------------------------------------------
    params = urlencode({"q": query, "location": city_info["location_str"]})
    url = f"{_SEARCH_BASE}?{params}"
    results.append(_run_strategy(
        "4. location= string param", url, session, city_info
    ))

    # ------------------------------------------------------------------
    # 5. location_slug param
    # ------------------------------------------------------------------
    params = urlencode({"q": query, "location_slug": city_info["slug"]})
    url = f"{_SEARCH_BASE}?{params}"
    results.append(_run_strategy(
        "5. location_slug= param", url, session, city_info
    ))

    # ------------------------------------------------------------------
    # 6. zip code param
    # ------------------------------------------------------------------
    params = urlencode({"q": query, "zip": city_info["zip"]})
    url = f"{_SEARCH_BASE}?{params}"
    results.append(_run_strategy(
        "6. zip= param", url, session, city_info
    ))

    # ------------------------------------------------------------------
    # 7. City slug in URL path  /search/<slug>?q=...
    # ------------------------------------------------------------------
    url = f"https://offerup.com/search/{city_info['slug']}?q={q}"
    results.append(_run_strategy(
        "7. City slug in path /search/<slug>?q=...", url, session, city_info
    ))

    # ------------------------------------------------------------------
    # 8. lat/lng via session cookie  (set ou_location cookie before search)
    # ------------------------------------------------------------------
    # OfferUp sometimes stores user location in a cookie.  Try injecting it.
    lat = city_info["lat"]
    lng = city_info["lng"]
    cookie_value = f"%7B%22latitude%22%3A{lat}%2C%22longitude%22%3A{lng}%7D"  # URL-encoded JSON
    url = f"{_SEARCH_BASE}?q={q}"
    results.append(_run_strategy(
        "8. ou_location cookie injection",
        url, session, city_info,
        cookies={"ou_location": cookie_value},
    ))

    # ------------------------------------------------------------------
    # 9. lat/lng as separate cookies
    # ------------------------------------------------------------------
    url = f"{_SEARCH_BASE}?q={q}"
    results.append(_run_strategy(
        "9. lat/lng as separate cookies",
        url, session, city_info,
        cookies={"ou_lat": str(lat), "ou_lng": str(lng)},
    ))

    return results


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------


def _summarize(all_results: dict[str, list[dict]]) -> None:
    print(f"\n{_sep('#')}")
    print("LOCATION PROBE SUMMARY")
    print(_sep('#'))

    any_strategy_works = False

    for city_key, city_results in all_results.items():
        city_label = _CITIES[city_key]["label"]
        print(f"\n{city_label}")
        print("-" * len(city_label))
        for r in city_results:
            label = r.get("label", "?")
            verdict = r.get("verdict", "ERROR: " + r.get("error", "unknown"))
            listing_count = r.get("listing_count", 0)
            locs = r.get("listing_locations", [])
            loc_summary = ", ".join(sorted(set(locs)))[:60] if locs else "(none)"
            print(f"  {label}")
            print(f"    listing_count={listing_count}  observed_locations=[{loc_summary}]")
            print(f"    Verdict: {verdict}")
            if "MATCHES TARGET" in verdict:
                any_strategy_works = True

    print(f"\n{_sep()}")
    if any_strategy_works:
        print("CONCLUSION: At least one strategy appears to control location.")
        print("Review the per-city output above to identify the reliable mechanism.")
    else:
        print("CONCLUSION: No strategy reliably matched the requested city.")
        print("OfferUp location appears to be GeoIP/session-driven from the server.")
        print("Location targeting cannot be controlled via simple URL params or cookies.")
    print(_sep())


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="OfferUp location-targeting probe for Vulture."
    )
    parser.add_argument(
        "--query", "-q", default="rtx 3080",
        help="Search query to probe (default: 'rtx 3080')"
    )
    parser.add_argument(
        "--cities", "-c", nargs="+",
        choices=list(_CITIES.keys()), default=list(_CITIES.keys()),
        help="Which cities to probe (default: all)"
    )
    args = parser.parse_args()

    print(_sep())
    print(f"OfferUp Location-Targeting Probe")
    print(f"Query: {args.query!r}")
    print(f"Cities: {args.cities}")
    print(_sep())
    print()
    print("NOTE: This probe makes real HTTP requests to offerup.com.")
    print("      Results depend on the requesting IP's GeoIP resolution.")
    print("      A cloud/datacenter IP may see different results than a")
    print("      residential IP (Raven server).")
    print()

    all_results: dict[str, list[dict]] = {}
    for city_key in args.cities:
        city_info = _CITIES[city_key]
        all_results[city_key] = probe_city(city_key, city_info, args.query)

    _summarize(all_results)


if __name__ == "__main__":
    main()
