"""
Facebook Marketplace candidate-source probe
==========================================
Reconnaissance only. Does NOT write to SQLite, send Discord alerts,
or touch the production adapter registry.

Goal: determine whether public Facebook Marketplace search pages yield
parseable listing data without a logged-in session.

Usage:
    python experiments/adapters/facebook_marketplace_probe.py --query "steam deck" --location "Houston, TX" --limit 5
    python experiments/adapters/facebook_marketplace_probe.py --query "macbook screen" --location "Houston, TX" --limit 5

This probe:
  - visits public Marketplace search URLs only
  - prefers Playwright (Facebook is JS-heavy)
  - never stores Facebook credentials
  - never bypasses login, CAPTCHA, anti-bot, or access controls
  - exits cleanly with a viability report (never crashes hunt runtime)
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import asdict, dataclass, field
from typing import Any
from urllib.parse import quote_plus, urlencode

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

SOURCE = "facebook_marketplace"
FB_ORIGIN = "https://www.facebook.com"

VIEWPORT = {"width": 1280, "height": 900}
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

NAV_TIMEOUT_MS = 45_000
NETWORK_IDLE_TIMEOUT_MS = 12_000
SETTLE_MS = 2_000

# Canonical city slugs verified against Facebook Marketplace URL routing.
# Free-form city names and ZIP codes redirect to IP-geolocated /category/search/.
KNOWN_LOCATION_SLUGS: dict[str, str] = {
    "houston, tx": "houston",
    "houston": "houston",
    "austin, tx": "austin",
    "austin": "austin",
    "dallas, tx": "dallas",
    "dallas": "dallas",
    "san antonio, tx": "sanantonio",
    "san antonio": "sanantonio",
    "arlington, va": "arlington",
    "arlington": "arlington",
    "new york, ny": "nyc",
    "new york": "nyc",
    "nyc": "nyc",
    "los angeles, ca": "la",
    "los angeles": "la",
    "la": "la",
    "san francisco, ca": "sanfrancisco",
    "san francisco": "sanfrancisco",
    "sf": "sanfrancisco",
    "chicago, il": "chicago",
    "chicago": "chicago",
    "boston, ma": "boston",
    "boston": "boston",
    "seattle, wa": "seattle",
    "seattle": "seattle",
    "atlanta, ga": "atlanta",
    "atlanta": "atlanta",
    "miami, fl": "miami",
    "miami": "miami",
    "portland, or": "portland",
    "portland": "portland",
    "denver, co": "denver",
    "denver": "denver",
    "phoenix, az": "phoenix",
    "phoenix": "phoenix",
}

LOGIN_MARKERS = (
    "log in to facebook",
    "log in or sign up",
    "login_form",
    "you must log in",
    "create new account",
)

CAPTCHA_MARKERS = (
    "captcha",
    "security check",
    "confirm your identity",
    "unusual activity",
    "verify it's you",
    "recaptcha",
)

CAPTCHA_URL_MARKERS = (
    "/checkpoint/",
    "challenge",
)

LOCATION_WALL_MARKERS = (
    "set your location",
    "choose a location",
    "enter your location",
    "turn on location",
    "allow location",
    "location services",
    "update your location",
    "where are you located",
)

REGION_UNAVAILABLE_MARKERS = (
    "marketplace isn't available",
    "marketplace is not available",
    "not available in your country",
    "not available in your region",
)

BLOCKER_LOGIN_WALL = "login_wall"
BLOCKER_CAPTCHA = "captcha_checkpoint"
BLOCKER_LOCATION = "location_permission_wall"
BLOCKER_LOCATION_RESOLUTION = "location_resolution_failed"
BLOCKER_EMPTY = "empty_public_results"
BLOCKER_UNSUPPORTED = "unsupported_page_shape"
BLOCKER_REGION = "region_unavailable"
BLOCKER_UNAVAILABLE = "marketplace_unavailable"


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass
class NormalizedListing:
    source: str
    title: str | None
    price: int | None
    location: str | None
    link: str | None
    image: str | None = None
    query: str | None = None


@dataclass
class ProbeReport:
    query: str
    location: str
    location_slug: str | None
    search_url: str | None
    http_status: int | None = None
    final_url: str | None = None
    page_title: str | None = None
    blockers: list[str] = field(default_factory=list)
    listings: list[NormalizedListing] = field(default_factory=list)
    extraction_method: str | None = None
    error: str | None = None
    recommendation: str = "keep_probe_only"

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["listings"] = [asdict(item) for item in self.listings]
        return data


# ---------------------------------------------------------------------------
# URL + location helpers
# ---------------------------------------------------------------------------


def _normalize_location_key(location: str) -> str:
    return re.sub(r"\s+", " ", location.strip().lower())


def resolve_location_slug(location: str) -> tuple[str | None, str | None]:
    """
    Map a human location string to a Facebook Marketplace city slug.

    Returns (slug, error_message). Unknown locations return (None, reason).
    """
    key = _normalize_location_key(location)
    if not key:
        return None, "location is empty"

    if key in KNOWN_LOCATION_SLUGS:
        return KNOWN_LOCATION_SLUGS[key], None

    # "City, ST" where city is a single token we can slugify.
    if "," in key:
        city_part = key.split(",", 1)[0].strip()
        slug = re.sub(r"[^a-z0-9]+", "", city_part)
        if slug:
            return slug, (
                f"location {location!r} is not in the known slug table; "
                f"using best-effort slug {slug!r} (may redirect to IP-geo default)"
            )

    slug = re.sub(r"[^a-z0-9]+", "", key)
    if slug:
        return slug, (
            f"location {location!r} is not in the known slug table; "
            f"using best-effort slug {slug!r} (may redirect to IP-geo default)"
        )
    return None, f"could not derive a Marketplace city slug from {location!r}"


def build_search_url(query: str, location_slug: str) -> str:
    params = urlencode({"query": query})
    return f"{FB_ORIGIN}/marketplace/{location_slug}/search/?{params}"


def location_slug_lost(requested_slug: str, final_url: str, html: str) -> bool:
    """True when Facebook dropped the requested city slug (redirect to category search)."""
    final_lower = final_url.lower()
    if "/marketplace/category/search" in final_lower:
        return True
    if f"/marketplace/{requested_slug.lower()}/search" not in final_lower:
        # Allow query-only drift but flag category fallback.
        if "/marketplace/" in final_lower and "/search" in final_lower:
            path_match = re.search(r"/marketplace/([^/]+)/search", final_lower)
            if path_match and path_match.group(1) not in (requested_slug.lower(), "category"):
                return True
    params_match = re.search(r'"location_id":"([^"]+)"', html)
    if params_match and params_match.group(1) == "category":
        return True
    return False


# ---------------------------------------------------------------------------
# Blocker detection
# ---------------------------------------------------------------------------


def detect_blockers(
    *,
    html: str,
    final_url: str,
    page_title: str | None,
    requested_slug: str | None = None,
    listing_count: int = 0,
) -> list[str]:
    blockers: list[str] = []
    haystack = " ".join(
        part for part in (html, final_url, page_title or "") if part
    ).lower()

    if any(marker in haystack for marker in REGION_UNAVAILABLE_MARKERS):
        blockers.append(BLOCKER_REGION)

    if any(marker in haystack for marker in LOGIN_MARKERS):
        blockers.append(BLOCKER_LOGIN_WALL)

    final_lower = final_url.lower()
    if any(marker in final_lower for marker in CAPTCHA_URL_MARKERS) or any(
        marker in haystack for marker in CAPTCHA_MARKERS
    ):
        blockers.append(BLOCKER_CAPTCHA)

    if any(marker in haystack for marker in LOCATION_WALL_MARKERS):
        blockers.append(BLOCKER_LOCATION)

    if requested_slug and location_slug_lost(requested_slug, final_url, html):
        blockers.append(BLOCKER_LOCATION_RESOLUTION)

    if listing_count == 0 and BLOCKER_LOGIN_WALL not in blockers:
        looks_like_marketplace = (
            "marketplace_search" in html
            or "/marketplace/item/" in html
            or "/marketplace/" in final_url.lower()
        )
        if looks_like_marketplace:
            blockers.append(BLOCKER_EMPTY)
        elif any(phrase in haystack for phrase in ("marketplace", "facebook")):
            blockers.append(BLOCKER_UNSUPPORTED)
        else:
            blockers.append(BLOCKER_UNAVAILABLE)

    # De-duplicate while preserving order.
    seen: set[str] = set()
    ordered: list[str] = []
    for item in blockers:
        if item not in seen:
            seen.add(item)
            ordered.append(item)
    return ordered


# ---------------------------------------------------------------------------
# Extraction + normalization
# ---------------------------------------------------------------------------


def _parse_price_value(raw: Any) -> int | None:
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        value = float(raw)
        # Facebook may expose minor units in amount_with_offset_in_currency.
        if value >= 1000 and value == int(value) and value % 100 == 0:
            return int(value // 100)
        return int(value)
    text = str(raw).strip()
    match = re.search(r"[\d,]+(?:\.\d+)?", text.replace("$", ""))
    if not match:
        return None
    number = match.group().replace(",", "")
    try:
        return int(float(number))
    except ValueError:
        return None


def _location_from_listing_obj(listing: dict[str, Any]) -> str | None:
    location = listing.get("location")
    if isinstance(location, dict):
        reverse = location.get("reverse_geocode")
        if isinstance(reverse, dict):
            city = str(reverse.get("city") or "").strip()
            state = str(reverse.get("state") or "").strip()
            display = str(reverse.get("city_page", {}).get("display_name") or "").strip()
            if display:
                return display
            if city and state:
                return f"{city}, {state}"
            if city:
                return city
        location_text = str(location.get("location_text") or "").strip()
        if location_text:
            return location_text
    return None


def _image_from_listing_obj(listing: dict[str, Any]) -> str | None:
    photo = listing.get("primary_listing_photo")
    if isinstance(photo, dict):
        image = photo.get("image")
        if isinstance(image, dict):
            uri = str(image.get("uri") or "").strip()
            if uri:
                return uri
    photos = listing.get("listing_photos")
    if isinstance(photos, list) and photos:
        first = photos[0]
        if isinstance(first, dict):
            image = first.get("image")
            if isinstance(image, dict):
                uri = str(image.get("uri") or "").strip()
                if uri:
                    return uri
    return None


def _listing_id_and_link(listing: dict[str, Any]) -> tuple[str | None, str | None]:
    listing_id = listing.get("id") or listing.get("listing_id")
    if listing_id is not None:
        listing_id = str(listing_id).strip() or None
    link = None
    if listing_id:
        link = f"{FB_ORIGIN}/marketplace/item/{listing_id}/"
    return listing_id, link


def normalize_listing(raw: dict[str, Any], query: str) -> NormalizedListing:
    title = str(
        raw.get("title")
        or raw.get("marketplace_listing_title")
        or raw.get("name")
        or ""
    ).strip() or None

    price = _parse_price_value(raw.get("price"))
    if price is None and isinstance(raw.get("listing_price"), dict):
        price_obj = raw["listing_price"]
        price = _parse_price_value(price_obj.get("amount"))
        if price is None:
            price = _parse_price_value(price_obj.get("formatted_amount"))
        if price is None:
            price = _parse_price_value(price_obj.get("amount_with_offset_in_currency"))

    location = None
    raw_location = raw.get("location")
    if isinstance(raw_location, str):
        location = raw_location.strip() or None
    if not location:
        location = _location_from_listing_obj(raw)

    link = str(raw.get("link") or raw.get("url") or "").strip() or None
    if link and link.startswith("/"):
        link = FB_ORIGIN + link
    if not link:
        _, link = _listing_id_and_link(raw)

    image = str(raw.get("image") or "").strip() or None
    if not image:
        image = _image_from_listing_obj(raw)

    return NormalizedListing(
        source=SOURCE,
        query=query,
        title=title,
        price=price,
        location=location,
        link=link,
        image=image,
    )


def _walk_marketplace_nodes(obj: Any, depth: int = 0, max_depth: int = 14) -> list[dict[str, Any]]:
    if depth > max_depth:
        return []
    found: list[dict[str, Any]] = []
    if isinstance(obj, dict):
        listing = obj.get("listing")
        if isinstance(listing, dict) and (
            listing.get("marketplace_listing_title")
            or listing.get("id")
            or listing.get("listing_price")
        ):
            found.append(listing)
        for value in obj.values():
            found.extend(_walk_marketplace_nodes(value, depth + 1, max_depth))
    elif isinstance(obj, list):
        for item in obj:
            found.extend(_walk_marketplace_nodes(item, depth + 1, max_depth))
    return found


def _extract_json_script_blobs(html: str) -> list[Any]:
    blobs: list[Any] = []
    for match in re.finditer(
        r'<script[^>]*type="application/json"[^>]*>(.*?)</script>',
        html,
        flags=re.DOTALL | re.IGNORECASE,
    ):
        payload = match.group(1).strip()
        if not payload:
            continue
        try:
            blobs.append(json.loads(payload))
        except json.JSONDecodeError:
            continue
    return blobs


def _extract_feed_units_regex(html: str) -> list[dict[str, Any]]:
    """Best-effort extraction when JSON is embedded inline but not in script tags."""
    pattern = re.compile(
        r'"marketplace_search":\{"feed_units":\{"edges":\[(.*?)\],"page_info":',
        re.DOTALL,
    )
    match = pattern.search(html)
    if not match:
        return []
    edges_blob = "[" + match.group(1) + "]"
    try:
        edges = json.loads(edges_blob)
    except json.JSONDecodeError:
        return []
    listings: list[dict[str, Any]] = []
    if isinstance(edges, list):
        for edge in edges:
            if not isinstance(edge, dict):
                continue
            node = edge.get("node")
            if isinstance(node, dict) and isinstance(node.get("listing"), dict):
                listings.append(node["listing"])
    return listings


def _extract_dom_listings(html: str, limit: int) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    seen_links: set[str] = set()

    # Anchor hrefs are the most stable public signal on rendered search pages.
    for match in re.finditer(
        r'href="(/marketplace/item/\d+/?[^"]*)"',
        html,
        flags=re.IGNORECASE,
    ):
        href = match.group(1)
        if href in seen_links:
            continue
        seen_links.add(href)
        listing_id_match = re.search(r"/marketplace/item/(\d+)", href)
        listing_id = listing_id_match.group(1) if listing_id_match else None
        results.append(
            {
                "id": listing_id,
                "link": href,
            }
        )
        if len(results) >= limit:
            break

    # Try to enrich DOM rows with nearby title/price text when present.
    for item in results:
        if not item.get("id"):
            continue
        item_id = item["id"]
        context_pattern = re.compile(
            rf"/marketplace/item/{re.escape(item_id)}.*?<span[^>]*>(?P<title>[^<]{{3,120}})</span>",
            re.DOTALL | re.IGNORECASE,
        )
        context_match = context_pattern.search(html)
        if context_match:
            item["title"] = context_match.group("title").strip()
        price_match = re.search(
            rf"/marketplace/item/{re.escape(item_id)}[\s\S]{{0,400}}?\$(\d[\d,]*)",
            html,
            flags=re.IGNORECASE,
        )
        if price_match:
            item["price"] = price_match.group(1)
    return results


def extract_raw_listings(html: str, *, limit: int = 5) -> tuple[list[dict[str, Any]], str | None]:
    """
    Extract listing-shaped dicts from HTML.

    Returns (raw_listings, extraction_method).
    """
    candidates: list[dict[str, Any]] = []
    method: str | None = None

    for blob in _extract_json_script_blobs(html):
        for listing in _walk_marketplace_nodes(blob):
            candidates.append(listing)
        if candidates:
            return _dedupe_raw(candidates, limit), "json_script_blob"

    regex_listings = _extract_feed_units_regex(html)
    if regex_listings:
        candidates.extend(regex_listings)
        if len(candidates) >= limit:
            return _dedupe_raw(candidates, limit), "ssr_feed_units_regex"

    dom_listings = _extract_dom_listings(html, limit)
    if dom_listings:
        candidates.extend(dom_listings)
        return _dedupe_raw(candidates, limit), "dom_item_links"

    return _dedupe_raw(candidates, limit), method


def _dedupe_raw(candidates: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    seen: set[str] = set()
    unique: list[dict[str, Any]] = []
    for raw in candidates:
        listing_id, link = _listing_id_and_link(raw)
        key = listing_id or str(raw.get("title") or "") or link or ""
        if not key or key in seen:
            continue
        seen.add(key)
        unique.append(raw)
        if len(unique) >= limit:
            break
    return unique


# ---------------------------------------------------------------------------
# Playwright probe
# ---------------------------------------------------------------------------


def _recommendation(blockers: list[str], listing_count: int) -> str:
    hard_blockers = {
        BLOCKER_LOGIN_WALL,
        BLOCKER_CAPTCHA,
        BLOCKER_REGION,
        BLOCKER_UNAVAILABLE,
        BLOCKER_LOCATION,
    }
    if any(b in hard_blockers for b in blockers):
        return "keep_probe_only_requires_account_or_session"
    if BLOCKER_LOCATION_RESOLUTION in blockers:
        return "keep_probe_only_until_location_slug_mapping_is_reliable"
    if listing_count > 0 and BLOCKER_EMPTY not in blockers:
        return "probe_only_public_access_limited_but_parseable"
    return "keep_probe_only"


def run_playwright_probe(
    query: str,
    location: str,
    *,
    limit: int = 5,
    headed: bool = False,
) -> ProbeReport:
    report = ProbeReport(
        query=query,
        location=location,
        location_slug=None,
        search_url=None,
    )

    slug, slug_warning = resolve_location_slug(location)
    if not slug:
        report.error = slug_warning
        report.blockers.append(BLOCKER_LOCATION_RESOLUTION)
        report.recommendation = _recommendation(report.blockers, 0)
        return report

    report.location_slug = slug
    report.search_url = build_search_url(query, slug)

    try:
        from playwright.sync_api import (  # noqa: PLC0415
            TimeoutError as PlaywrightTimeout,
            sync_playwright,
        )
    except (ImportError, ModuleNotFoundError):
        report.error = (
            "playwright is not installed; run: pip install playwright "
            "&& python -m playwright install chromium"
        )
        report.blockers.append(BLOCKER_UNSUPPORTED)
        report.recommendation = _recommendation(report.blockers, 0)
        return report

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=not headed)
            context = browser.new_context(
                user_agent=USER_AGENT,
                locale="en-US",
                viewport=VIEWPORT,
                extra_http_headers={"Accept-Language": "en-US,en;q=0.9"},
            )
            page = context.new_page()

            try:
                response = page.goto(
                    report.search_url,
                    wait_until="domcontentloaded",
                    timeout=NAV_TIMEOUT_MS,
                )
                report.http_status = response.status if response else None
                report.final_url = page.url
                try:
                    page.wait_for_load_state("networkidle", timeout=NETWORK_IDLE_TIMEOUT_MS)
                except PlaywrightTimeout:
                    pass
                page.wait_for_timeout(SETTLE_MS)
                report.page_title = page.title()
                html = page.content()
            except PlaywrightTimeout:
                report.error = f"navigation timed out after {NAV_TIMEOUT_MS} ms"
                report.final_url = page.url
                report.page_title = page.title()
                html = page.content()
            except Exception as exc:  # noqa: BLE001 — probe must not crash callers
                report.error = f"playwright navigation failed: {exc}"
                browser.close()
                report.blockers.append(BLOCKER_UNSUPPORTED)
                report.recommendation = _recommendation(report.blockers, 0)
                return report
            finally:
                browser.close()
    except Exception as exc:  # noqa: BLE001
        report.error = f"playwright launch failed: {exc}"
        report.blockers.append(BLOCKER_UNSUPPORTED)
        report.recommendation = _recommendation(report.blockers, 0)
        return report

    raw_listings, extraction_method = extract_raw_listings(html, limit=limit)
    report.extraction_method = extraction_method
    report.listings = [normalize_listing(raw, query) for raw in raw_listings]

    report.blockers = detect_blockers(
        html=html,
        final_url=report.final_url or "",
        page_title=report.page_title,
        requested_slug=slug,
        listing_count=len(report.listings),
    )

    if slug_warning and BLOCKER_LOCATION_RESOLUTION not in report.blockers:
        report.error = slug_warning

    report.recommendation = _recommendation(report.blockers, len(report.listings))
    return report


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def _print_report(report: ProbeReport) -> None:
    sep = "=" * 72
    print(sep)
    print("FACEBOOK MARKETPLACE PROBE")
    print(sep)
    print(f"query          : {report.query!r}")
    print(f"location       : {report.location!r}")
    print(f"location_slug  : {report.location_slug!r}")
    print(f"search_url     : {report.search_url}")
    print(f"http_status    : {report.http_status}")
    print(f"final_url      : {report.final_url}")
    print(f"page_title     : {report.page_title!r}")
    if report.error:
        print(f"note           : {report.error}")

    print("\n--- blockers ---")
    if report.blockers:
        for blocker in report.blockers:
            print(f"  - {blocker}")
    else:
        print("  (none detected)")

    print("\n--- extraction ---")
    print(f"method         : {report.extraction_method or '(none)'}")
    print(f"listing_count  : {len(report.listings)}")

    if report.listings:
        print("\n--- normalized listings ---")
        for index, listing in enumerate(report.listings, 1):
            print(f"  [{index}] {json.dumps(asdict(listing), ensure_ascii=False)}")

    print("\n--- viability ---")
    viable = bool(report.listings) and not any(
        b in report.blockers
        for b in (
            BLOCKER_LOGIN_WALL,
            BLOCKER_CAPTCHA,
            BLOCKER_REGION,
            BLOCKER_UNAVAILABLE,
            BLOCKER_LOCATION,
        )
    )
    print(f"public_search_viable : {'YES (limited)' if viable else 'NO'}")
    print(f"recommendation       : {report.recommendation}")
    if any(
        b in report.blockers
        for b in (BLOCKER_LOGIN_WALL, BLOCKER_CAPTCHA, BLOCKER_LOCATION)
    ):
        print("account/session      : likely required for reliable access")
    elif viable:
        print("account/session      : not required for first-page public results")
    else:
        print("account/session      : unclear — re-run from residential IP if blocked")

    print(sep)
    print("PROBE COMPLETE")
    print(sep)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Probe public Facebook Marketplace search viability for Vulture."
    )
    parser.add_argument("--query", "-q", required=True, help="Search query")
    parser.add_argument(
        "--location",
        "-l",
        default="Houston, TX",
        help='Location label (default: "Houston, TX")',
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=5,
        help="Maximum listings to normalize and print (default: 5)",
    )
    parser.add_argument(
        "--headed",
        action="store_true",
        help="Run Chromium in headed mode (requires a display).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit the probe report as JSON only.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    limit = max(1, min(args.limit, 20))

    try:
        report = run_playwright_probe(
            args.query,
            args.location,
            limit=limit,
            headed=args.headed,
        )
    except Exception as exc:  # noqa: BLE001 — probe must never crash hunt runtime
        fallback = ProbeReport(
            query=args.query,
            location=args.location,
            location_slug=None,
            search_url=None,
            error=f"unexpected probe failure: {exc}",
            blockers=[BLOCKER_UNSUPPORTED],
            recommendation="keep_probe_only",
        )
        if args.json:
            print(json.dumps(fallback.to_dict(), ensure_ascii=False, indent=2))
        else:
            _print_report(fallback)
        return 1

    if args.json:
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    else:
        _print_report(report)

    blocked = any(
        b in report.blockers
        for b in (
            BLOCKER_LOGIN_WALL,
            BLOCKER_CAPTCHA,
            BLOCKER_REGION,
            BLOCKER_UNAVAILABLE,
            BLOCKER_LOCATION,
        )
    )
    if blocked or not report.listings:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
