import json
import re
import time
from urllib.parse import quote_plus

import requests

from models.listing import Listing

_SEARCH_URL = "https://www.mercari.com/search/"
_INITIALIZE_URL = "https://www.mercari.com/v1/initialize"
_API_URL = "https://www.mercari.com/v1/api"
_PERSISTED_QUERY_HASH = "bc1eb4c4c2bb85e0e19b07c807570de0f5386c0fe770a43194c6e61b7af8c111"

_HEADERS_BROWSER = {
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

_GENERIC_QUERY_TOKENS = {
    "for",
    "with",
    "and",
    "the",
    "item",
    "items",
    "sale",
    "buy",
    "used",
    "new",
}


def _normalize_price(value) -> int | None:
    """
    Normalize Mercari numeric price to integer dollars for Listing.

    Empirical probe data on Raven shows values like 38000 for ~$380.00.
    Treat large integers as minor units (cents) and convert to whole dollars.
    """
    if not isinstance(value, int):
        return None
    if value <= 0:
        return None
    # Heuristic: Mercari API commonly returns cents-like values.
    if value >= 1000:
        return value // 100
    return value


def _is_relevant_to_query(title: str, query: str) -> bool:
    """
    Conservative, deterministic relevance guard.

    - Tokenize title/query to alphanumeric lowercase tokens.
    - Drop tiny/generic query tokens.
    - If query has no meaningful tokens, do not filter.
    - For model-like queries (e.g. "rtx 3080"), require one strong token match.
    """
    if not title or not query:
        return True

    title_tokens = set(re.findall(r"[a-z0-9]+", title.lower()))
    query_tokens = [
        t
        for t in re.findall(r"[a-z0-9]+", query.lower())
        if len(t) > 1 and t not in _GENERIC_QUERY_TOKENS
    ]
    if not query_tokens:
        return True

    strong_tokens = [t for t in query_tokens if any(ch.isdigit() for ch in t) or len(t) >= 3]
    if not strong_tokens:
        return True

    return any(token in title_tokens for token in strong_tokens)


def _walk_for_listing_objects(obj, depth: int = 0, max_depth: int = 10) -> list[dict]:
    """Fallback recursive scan for listing-shaped objects."""
    if depth > max_depth:
        return []

    found: list[dict] = []
    if isinstance(obj, dict):
        keys = {k.lower() for k in obj}
        has_name = "name" in keys or "title" in keys
        has_id = "id" in keys
        if has_name and has_id:
            found.append(obj)
        for value in obj.values():
            found.extend(_walk_for_listing_objects(value, depth + 1, max_depth))
    elif isinstance(obj, list):
        for item in obj:
            found.extend(_walk_for_listing_objects(item, depth + 1, max_depth))
    return found


def _extract_items(payload: dict) -> list[dict]:
    """
    Parse data.search.items[] when available.
    Fall back to recursive object scan if shape differs.
    """
    data = payload.get("data")
    if isinstance(data, dict):
        search = data.get("search")
        if isinstance(search, dict):
            items = search.get("items")
            if isinstance(items, list):
                return items
            items_alt = search.get("itemsList")
            if isinstance(items_alt, list):
                return items_alt

        search_facet = data.get("searchFacet")
        if isinstance(search_facet, dict):
            items = search_facet.get("items")
            if isinstance(items, list):
                return items

    return _walk_for_listing_objects(payload)


def _canonical_item_url(item_id: str, raw_url: str | None = None) -> str:
    """
    Build a Mercari US item URL that opens in the browser.

    Mercari US listing pages live at /us/item/{id}/ — the legacy /item/{id}/
    path returns 404. API payloads may return either form; normalize always.
    """
    if isinstance(raw_url, str) and raw_url.strip():
        link = raw_url.strip()
        if link.startswith("/"):
            link = "https://www.mercari.com" + link
        elif link.startswith("item/"):
            link = "https://www.mercari.com/" + link
        # Legacy path without /us/
        link = re.sub(
            r"^https?://(?:www\.)?mercari\.com/item/",
            "https://www.mercari.com/us/item/",
            link,
            flags=re.IGNORECASE,
        )
        if re.search(r"mercari\.com/us/item/", link, re.IGNORECASE):
            return link if link.endswith("/") else link + "/"
    return f"https://www.mercari.com/us/item/{item_id}/"


def _normalize_listing(item: dict) -> Listing | None:
    """
    Convert a Mercari JSON object into Listing.
    Returns None for malformed/noisy records.
    """
    item_id = item.get("id")
    if not isinstance(item_id, str) or not item_id.startswith("m"):
        # Ignore category/nav objects (often numeric IDs).
        return None

    raw_title = item.get("name") or item.get("title")
    if not isinstance(raw_title, str):
        return None
    title = " ".join(raw_title.split()).strip()
    if not title:
        return None

    price = _normalize_price(item.get("price"))

    raw_url = item.get("url") or item.get("itemUrl") or item.get("permalink")
    link = _canonical_item_url(item_id, raw_url if isinstance(raw_url, str) else None)

    image = None
    thumbs = item.get("thumbnails")
    if isinstance(thumbs, list) and thumbs:
        first = thumbs[0]
        if isinstance(first, str) and first.strip():
            image = first
    if image is None:
        maybe_image = item.get("thumbnail") or item.get("image")
        if isinstance(maybe_image, str) and maybe_image.strip():
            image = maybe_image

    # Listing model currently has no image field; keep normalized extraction local.
    return Listing(
        source="mercari",
        title=title,
        price=price,
        location=None,
        link=link,
    )


def search_mercari(query: str, city: str = "houston", limit: int = 10) -> list[Listing]:
    """
    Mercari adapter (requests-only Strategy D).

    Notes:
    - `city` is accepted for adapter-signature compatibility but currently unused.
    - No Playwright, no login handling, no proxy support.
    """
    del city  # Mercari location control is not implemented.
    if not query or not query.strip():
        return []

    request_limit = max(1, min(int(limit or 10), 100))
    session = requests.Session()

    try:
        # 1) Warm-up request to establish session / CF cookie.
        session.get("https://www.mercari.com/", headers=_HEADERS_BROWSER, timeout=20, allow_redirects=True)
        time.sleep(0.5)

        # 2) Search page hit for realistic referer/session flow.
        session.get(
            f"{_SEARCH_URL}?keyword={quote_plus(query)}",
            headers={**_HEADERS_BROWSER, "Referer": "https://www.mercari.com/"},
            timeout=20,
            allow_redirects=True,
        )

        # 3) Initialize to get CSRF/access token.
        init_headers = {
            **_HEADERS_BROWSER,
            "Accept": "application/json, text/plain, */*",
            "Referer": "https://www.mercari.com/",
        }
        init_resp = session.get(_INITIALIZE_URL, headers=init_headers, timeout=20)
        init_resp.raise_for_status()
        init_payload = init_resp.json()
        csrf = init_payload.get("csrf")
        access_token = init_payload.get("accessToken")
        if not isinstance(csrf, str) or not csrf:
            return []

        # 4) Query v1/api using persisted GraphQL query.
        criteria = {
            "offset": 0,
            "soldItemsOffset": 0,
            "promotedItemsOffset": 0,
            "sortBy": 0,
            "length": request_limit,
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
                "category_ids_hierarchical",
                "brand_ids",
                "size_ids_hierarchical",
                "authenticity",
                "condition_ids",
                "item_status",
                "shipping_payer_ids",
                "meetup",
                "country_sources",
                "deals",
                "price",
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
        variables = {
            "withFeedLikes": False,
            "withFeedRecentlyViewed": False,
            "withFeedDeals": False,
            "feedDealsCriteria": None,
            "criteria": criteria,
        }
        extensions = {
            "persistedQuery": {
                "version": 1,
                "sha256Hash": _PERSISTED_QUERY_HASH,
            }
        }

        api_headers = {
            **_HEADERS_BROWSER,
            "Accept": "application/json",
            "Content-Type": "application/json",
            # Do NOT advertise Brotli, requests may not decode it.
            "Accept-Encoding": "gzip, deflate",
            "Origin": "https://www.mercari.com",
            "Referer": f"https://www.mercari.com/search/?keyword={quote_plus(query)}",
            "x-csrf-token": csrf,
        }
        if isinstance(access_token, str) and access_token:
            api_headers["Authorization"] = f"Bearer {access_token}"

        api_resp = session.get(
            _API_URL,
            params={
                "operationName": "searchFacetQuery",
                "variables": json.dumps(variables, separators=(",", ":")),
                "extensions": json.dumps(extensions, separators=(",", ":")),
            },
            headers=api_headers,
            timeout=25,
        )
        api_resp.raise_for_status()
        payload = api_resp.json()

    except (requests.RequestException, ValueError, json.JSONDecodeError):
        return []

    listings: list[Listing] = []
    raw_items = _extract_items(payload)
    for raw in raw_items:
        if not isinstance(raw, dict):
            continue
        listing = _normalize_listing(raw)
        if listing is None:
            continue
        if not _is_relevant_to_query(listing.title, query):
            continue
        listings.append(listing)
        if len(listings) >= request_limit:
            break

    return listings
