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

Location limitation (IMPORTANT)
---------------------------------
Location targeting is NOT solved.  Returned locations are GeoIP- and
session-dependent: OfferUp resolves results by the server's GeoIP of the
requesting IP and/or any location stored in its session cookies — **not** by
the ``city`` argument.  The same query from different IPs or sessions returns
results from completely different cities.

The ``city`` parameter is accepted for registry interface compatibility and
is logged on every call, but it does NOT currently control which city's
listings are returned.

Do NOT mark this adapter ``stable=True`` until a reliable mechanism for
targeting a specific city (e.g. ``?location_slug=``, session-based location
cookies, or a location API call) has been validated for Houston and at least
one other target city.  Until then ``location_control`` must remain
``"unverified"`` in ``adapters/registry.py``.

Validated during reconnaissance (experiments/adapters/offerup_probe.py,
May 2026):
    - requests works; HTTP 200 without bot block
    - No login or session required for basic search
    - __NEXT_DATA__ carries full listing payload server-side
    - ModularFeedListing nodes contain title, price, locationName, listingId

Quick manual smoke test
-----------------------
Run from the project root (no .env required)::

    python3 -c "from adapters.offerup import search_offerup; print(search_offerup('rtx 3080', limit=5))"

Expected output: a list of up to 5 Listing objects with source='offerup',
non-empty title, integer price, city+state location, and a canonical
offerup.com item URL.  Log output (INFO level) shows requested_city vs
actual_locations_observed so the GeoIP gap is visible.
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
    currently control the geographic scope of results.  OfferUp resolves
    listings by the server's GeoIP of the requesting IP.  The requested city
    and the actual listing locations are both logged so this gap is visible
    during Raven runs.

    Does not write to SQLite, does not send Discord alerts.
    """
    log.info(
        "OfferUp search: query=%r, requested_city=%r (location_control=unverified), limit=%d",
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
