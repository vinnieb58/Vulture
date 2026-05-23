"""
adapters/offerup.py

OfferUp search adapter for Vulture.

Status: EXPERIMENTAL
---------------------
Do not set ``stable=True`` in the registry capability metadata until
Houston (or any specific city) location targeting has been validated.
See "Location limitation" below.

Parsing strategy
----------------
``requests`` + ``__NEXT_DATA__`` currently works.  OfferUp is a Next.js
SPA that server-renders a full ``__NEXT_DATA__`` JSON blob into the initial
HTML.  The blob contains GraphQL feed nodes typed as ``"ModularFeedListing"``
with the following fields:

    listingId    (str, UUID)  — used to build the canonical item URL
    title        (str)
    price        (str)        — e.g. "550"; parsed to int
    locationName (str)        — e.g. "Arlington, VA"

No browser automation is required; ``requests`` + BeautifulSoup + ``json``
is sufficient.

Location limitation (IMPORTANT — probe results May 2026)
---------------------------------------------------------
Location targeting is NOT possible via URL parameters or cookies.
A systematic probe (experiments/adapters/offerup_location_probe.py) tested
all of the following strategies for Houston TX, Dallas TX, and Arlington VA:

    1. Baseline (no location param)
    2. ?lat=<lat>&lng=<lng>
    3. ?lat=<lat>&lng=<lng>&radius=30
    4. ?location=<City, ST>
    5. ?location_slug=<city-st>
    6. ?zip=<zip>
    7. /search/<city-slug>?q=...  (→ HTTP 404, route does not exist)
    8. ou_location cookie injection (URL-encoded JSON)
    9. separate ou_lat / ou_lng cookies

Result: every strategy returned identical results for all three cities.
The server resolved results entirely from the requesting IP's GeoIP.
A cloud/datacenter IP (AWS Northern Virginia) returned DMV-area listings
(Arlington VA, Burke VA, Washington DC, Chantilly VA) for all strategies
regardless of what city was requested.  The ``__NEXT_DATA__`` JSON confirmed
the GeoIP resolution via ``city = 'Ashburn'`` (a Loudoun County, VA
datacenter hub).

Houston and Katy TX results observed during earlier Raven runs were caused by
Raven's residential IP being GeoIP'd to Houston — NOT by the city parameter.

Conclusion: ``supports_location`` must remain ``False`` and
``location_control`` must remain ``"unverified"`` in ``adapters/registry.py``
until a working mechanism is identified.  Candidates for future investigation:
  - OfferUp's internal GraphQL/REST API (requires reverse-engineering the
    mobile/web app's authenticated API calls)
  - Browser automation with a locally running browser that stores a location
    preference in the OfferUp user session after interactive location-setting

The ``city`` parameter is accepted for registry interface compatibility and
is logged on every call so the GeoIP gap remains visible in Vulture logs.

Validated during reconnaissance (experiments/adapters/offerup_probe.py,
May 2026):
    - requests works; HTTP 200 without bot block
    - No login or session required for basic search
    - __NEXT_DATA__ carries full listing payload server-side
    - ModularFeedListing nodes contain title, price, locationName, listingId

Manual smoke tests
------------------
Direct adapter smoke (no .env required)::

    python3 -c "
    from adapters.offerup import search_offerup
    results = search_offerup('rtx 3080', city='houston', limit=5)
    for r in results: print(r)
    "

Expected: up to 5 Listing objects with source='offerup'.  The INFO log line
will show ``requested_city='houston'`` and ``actual_locations_observed=[...]``
— the observed locations reflect the requesting IP's GeoIP region, NOT the
requested city.

Location probe (run from project root)::

    python3 experiments/adapters/offerup_location_probe.py
    python3 experiments/adapters/offerup_location_probe.py --query "75 inch tv"
    python3 experiments/adapters/offerup_location_probe.py --query "toyota sequoia"

From Raven (residential TX IP), Houston/Katy locations should appear in
``actual_locations_observed``.  From any cloud/datacenter IP, results will
reflect that datacenter's GeoIP region regardless of the ``city`` argument.

Houston vs Dallas comparison on Raven::

    python3 -c "
    from adapters.offerup import search_offerup
    h = search_offerup('rtx 3080', city='houston', limit=10)
    d = search_offerup('rtx 3080', city='dallas', limit=10)
    h_locs = sorted({l.location for l in h if l.location})
    d_locs = sorted({l.location for l in d if l.location})
    print('Houston call locations:', h_locs)
    print('Dallas  call locations:', d_locs)
    print('Same results?', h_locs == d_locs)
    "

If ``Same results? True``, location targeting is confirmed to be GeoIP-only.
"""

import json
import logging
import re
from urllib.parse import quote_plus

import requests
from bs4 import BeautifulSoup

from models.listing import Listing

log = logging.getLogger(__name__)

_SEARCH_URL = "https://offerup.com/search"
_ITEM_URL_TEMPLATE = "https://offerup.com/item/detail/{listing_id}/"

# Typename that identifies listing nodes in the __NEXT_DATA__ JSON tree.
# This is the schema stability guard: if OfferUp changes the typename we
# get zero results + a warning rather than silently wrong data.
_EXPECTED_TYPENAME = "ModularFeedListing"

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


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _fetch(query: str) -> "requests.Response | None":
    """Return the HTTP response for a search, or None on any unrecoverable error."""
    url = f"{_SEARCH_URL}?q={quote_plus(query)}"
    log.debug("OfferUp fetch: %s", url)
    try:
        resp = requests.get(url, headers=_HEADERS, timeout=20, allow_redirects=True)
    except requests.exceptions.RequestException as exc:
        log.error("OfferUp request failed for query %r: %s", query, exc)
        return None

    if resp.status_code == 403:
        log.error(
            "OfferUp returned 403 Forbidden for query %r — possible IP/bot block. "
            "Browser automation with a residential proxy may be required.",
            query,
        )
        return None

    if resp.status_code not in (200, 206):
        log.error(
            "OfferUp returned HTTP %d for query %r", resp.status_code, query
        )
        return None

    # Login-gate check: if we were redirected to a login/signin page
    final_url = str(resp.url).lower()
    if "login" in final_url or "signin" in final_url:
        log.error(
            "OfferUp redirected to a login page for query %r: %s", query, resp.url
        )
        return None

    return resp


def _extract_next_data(html: str) -> "dict | None":
    """
    Parse the ``__NEXT_DATA__`` script tag from the HTML and return its JSON
    content as a dict, or None if the tag is absent or unparseable.
    """
    soup = BeautifulSoup(html, "lxml")
    tag = soup.find("script", id="__NEXT_DATA__")
    if not tag or not tag.string:
        log.warning(
            "OfferUp: __NEXT_DATA__ script tag not found — "
            "page structure may have changed or the request was blocked"
        )
        return None
    try:
        return json.loads(tag.string)
    except json.JSONDecodeError as exc:
        log.error("OfferUp: failed to parse __NEXT_DATA__ JSON: %s", exc)
        return None


def _collect_feed_listings(obj: object, depth: int = 0, max_depth: int = 10) -> list[dict]:
    """
    Recursively walk the ``__NEXT_DATA__`` tree and return every dict whose
    ``__typename`` equals ``_EXPECTED_TYPENAME`` (``"ModularFeedListing"``).

    Using the exact typename instead of guessing by field presence is the
    schema stability guard: if OfferUp renames or restructures its feed nodes
    this function returns an empty list and the caller logs a clear warning
    rather than silently producing garbage data.
    """
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


def _parse_price(raw: object) -> "int | None":
    """Convert a raw price value (str or number) to an integer dollar amount."""
    if raw is None:
        return None
    m = re.search(r"[\d,]+", str(raw).replace("$", ""))
    if not m:
        return None
    try:
        return int(m.group().replace(",", ""))
    except ValueError:
        return None


def _node_to_listing(node: dict) -> "Listing | None":
    """
    Convert a single ``ModularFeedListing`` dict to a ``Listing`` instance.
    Returns None if the node lacks a usable title.
    """
    title = str(node.get("title") or "").strip()
    if not title:
        log.debug(
            "OfferUp: skipping %s node with empty title (listingId=%s)",
            _EXPECTED_TYPENAME, node.get("listingId"),
        )
        return None

    price = _parse_price(node.get("price"))
    location = str(node.get("locationName") or "").strip() or None
    listing_id = node.get("listingId") or ""
    link = _ITEM_URL_TEMPLATE.format(listing_id=listing_id) if listing_id else ""

    return Listing(
        source="offerup",
        title=title,
        price=price,
        location=location,
        link=link,
    )


# ---------------------------------------------------------------------------
# Public adapter function
# ---------------------------------------------------------------------------


def search_offerup(query: str, city: str = "houston", limit: int = 10) -> list[Listing]:
    """
    Search OfferUp for *query* and return up to *limit* ``Listing`` objects.

    *city* is accepted for registry interface compatibility (all registered
    adapters share the ``(query, city, limit)`` signature) but does NOT
    control the geographic scope of results.  A systematic probe of URL
    params (?lat, ?lng, ?zip, ?location, ?location_slug), path slugs, and
    cookie injection confirmed that OfferUp resolves listings entirely from
    the requesting IP's GeoIP.  No tested mechanism changed the result set.

    The requested city and the actual listing locations are both logged at
    INFO level so the GeoIP gap is visible during Raven runs.  From a Raven
    residential TX IP, results will be Houston/Katy; from a cloud datacenter
    IP, results will be from that datacenter's GeoIP region.

    Does not write to SQLite, does not send Discord alerts.
    """
    log.info(
        "OfferUp search: query=%r requested_city=%r limit=%d "
        "(location_control=unverified — results are GeoIP-driven by requesting IP, "
        "not by the city argument)",
        query, city, limit,
    )

    resp = _fetch(query)
    if resp is None:
        return []

    next_data = _extract_next_data(resp.text)
    if next_data is None:
        return []

    raw_nodes = _collect_feed_listings(next_data)
    if not raw_nodes:
        log.warning(
            "OfferUp: zero %r nodes found in __NEXT_DATA__ for query %r — "
            "expected __typename='%s'.  Schema may have changed.",
            _EXPECTED_TYPENAME, query, _EXPECTED_TYPENAME,
        )
        return []

    log.debug(
        "OfferUp: found %d raw %s nodes before dedup/limit",
        len(raw_nodes), _EXPECTED_TYPENAME,
    )

    listings: list[Listing] = []
    seen_ids: set[str] = set()

    for node in raw_nodes:
        if len(listings) >= limit:
            break
        listing_id = node.get("listingId") or ""
        if listing_id in seen_ids:
            continue
        seen_ids.add(listing_id)
        listing = _node_to_listing(node)
        if listing is not None:
            listings.append(listing)

    if not listings:
        log.warning(
            "OfferUp: query %r yielded 0 usable listings after parsing",
            query,
        )
        return []

    returned_locations = sorted({lst.location for lst in listings if lst.location})
    log.info(
        "OfferUp: query=%r returned %d listing(s). "
        "requested_city=%r, actual_locations_observed=%s",
        query, len(listings), city, returned_locations,
    )

    return listings
