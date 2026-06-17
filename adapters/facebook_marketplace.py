"""
adapters/facebook_marketplace.py

Facebook Marketplace search adapter for Vulture.

Status: EXPERIMENTAL — included in selected default vertical profiles for
this single-user Raven deployment (marketplace/general verticals, not retail).
----------------------------------------------------------------------
Raven residential smoke tests (May 2026) returned SSR listings for common
queries but every run also reported ``login_wall`` and ``captcha_checkpoint``
blockers. Public access is fragile and may fail without warning.

Public SSR access is fragile and often reports login_wall/captcha_checkpoint.
No login, cookies, sessions, or CAPTCHA bypass are implemented. If Facebook
becomes noisy or blocked, remove it from default profiles in
``engine/source_selection.py`` but leave this adapter registered.

Parsing strategy
----------------
Playwright Chromium → SSR JSON blobs / feed_units regex / DOM item links.
Extraction and normalization reuse ``experiments/adapters/facebook_marketplace_probe.py``.

Safety boundaries
-----------------
- Public Marketplace search URLs only
- No credential storage, cookies, sessions, or browser profiles persisted
- No CAPTCHA/login/checkpoint bypass
- No raw HTML or screenshots stored by default
- Never raises; returns ``[]`` on hard failures

The ``city`` argument maps to a Marketplace city slug (e.g. ``"Houston, TX"``
→ ``houston``). Unknown cities use best-effort slugification and may redirect
to IP-geolocated results.

Does not write to SQLite. Does not send Discord alerts.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from experiments.adapters.facebook_marketplace_probe import (
    BLOCKER_CAPTCHA,
    BLOCKER_LOGIN_WALL,
    NAV_TIMEOUT_MS,
    NETWORK_IDLE_TIMEOUT_MS,
    SETTLE_MS,
    USER_AGENT,
    VIEWPORT,
    build_search_url,
    detect_blockers,
    extract_raw_listings,
    normalize_listing,
    resolve_location_slug,
)
from models.listing import Listing

log = logging.getLogger(__name__)

SOURCE = "facebook_marketplace"

# Ubuntu 26.04 Playwright compatibility (same pattern as bestbuy/carsdotcom).
if not os.environ.get("PLAYWRIGHT_HOST_PLATFORM_OVERRIDE"):
    os.environ["PLAYWRIGHT_HOST_PLATFORM_OVERRIDE"] = "ubuntu24.04-x64"

try:
    from playwright.sync_api import (
        TimeoutError as PlaywrightTimeout,
        sync_playwright,
    )
    _PLAYWRIGHT_AVAILABLE = True
except ImportError:
    _PLAYWRIGHT_AVAILABLE = False
    log.warning(
        "playwright package not found. Facebook Marketplace adapter will return "
        "empty results. Install with: pip install playwright "
        "&& python -m playwright install chromium"
    )

_WARN_BLOCKERS = frozenset({BLOCKER_LOGIN_WALL, BLOCKER_CAPTCHA})


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _normalized_to_listing(normalized) -> Listing | None:
    title = (normalized.title or "").strip()
    link = (normalized.link or "").strip()
    if not title or not link:
        return None
    return Listing(
        source=SOURCE,
        title=title,
        price=normalized.price,
        location=normalized.location,
        link=link,
    )


def _log_blocker_warning(blockers: list[str], query: str, listing_count: int) -> None:
    notable = [b for b in blockers if b in _WARN_BLOCKERS] or blockers
    log.warning(
        "facebook_marketplace: blocker(s) detected for query=%r: %s. "
        "Public access is fragile; no login/CAPTCHA bypass is implemented. "
        "Returning %d SSR listing(s) extracted before/at blocker.",
        query,
        ", ".join(notable),
        listing_count,
    )


def parse_search_html(
    html: str,
    query: str,
    *,
    final_url: str = "",
    page_title: str | None = None,
    requested_slug: str | None = None,
    limit: int = 25,
) -> tuple[list[Listing], list[str]]:
    """
    Parse Marketplace search HTML into ``Listing`` objects and blocker tags.

  Returns (listings, blockers). Location and image may be absent per listing.
    """
    raw_listings, _method = extract_raw_listings(html, limit=limit)
    listings: list[Listing] = []
    for raw in raw_listings:
        normalized = normalize_listing(raw, query)
        listing = _normalized_to_listing(normalized)
        if listing is not None:
            listings.append(listing)

    blockers = detect_blockers(
        html=html,
        final_url=final_url,
        page_title=page_title,
        requested_slug=requested_slug,
        listing_count=len(listings),
    )
    return listings, blockers


def _fetch_search_html(query: str, city: str) -> tuple[str | None, str | None, str | None, str | None]:
    """
    Fetch search HTML via Playwright.

    Returns (html, final_url, page_title, location_slug) or (None, ...) on failure.
    """
    slug, slug_warning = resolve_location_slug(city)
    if not slug:
        log.warning(
            "facebook_marketplace: could not resolve location slug for city=%r: %s",
            city,
            slug_warning,
        )
        return None, None, None, None

    if slug_warning:
        log.info(
            "facebook_marketplace: location slug warning for city=%r: %s",
            city,
            slug_warning,
        )

    search_url = build_search_url(query, slug)

    if not _PLAYWRIGHT_AVAILABLE:
        return None, None, None, slug

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            context = browser.new_context(
                user_agent=USER_AGENT,
                locale="en-US",
                viewport=VIEWPORT,
                extra_http_headers={"Accept-Language": "en-US,en;q=0.9"},
            )
            page = context.new_page()
            try:
                page.goto(
                    search_url,
                    wait_until="domcontentloaded",
                    timeout=NAV_TIMEOUT_MS,
                )
                try:
                    page.wait_for_load_state(
                        "networkidle", timeout=NETWORK_IDLE_TIMEOUT_MS
                    )
                except PlaywrightTimeout:
                    pass
                page.wait_for_timeout(SETTLE_MS)
                html = page.content()
                return html, page.url, page.title(), slug
            except PlaywrightTimeout:
                log.error(
                    "facebook_marketplace: navigation timed out after %d ms for query=%r",
                    NAV_TIMEOUT_MS,
                    query,
                )
                return page.content(), page.url, page.title(), slug
            except Exception as exc:
                log.error(
                    "facebook_marketplace: navigation failed for query=%r: %s",
                    query,
                    str(exc)[:200],
                )
                return None, None, None, slug
            finally:
                browser.close()
    except Exception as exc:
        log.error(
            "facebook_marketplace: Playwright launch failed for query=%r: %s",
            query,
            exc,
        )
        return None, None, None, slug


# ---------------------------------------------------------------------------
# Public adapter
# ---------------------------------------------------------------------------


def search_facebook_marketplace(
    query: str,
    city: str | None = None,
    limit: int = 25,
    **kwargs: Any,
) -> list[Listing]:
    """
    Search Facebook Marketplace and return up to *limit* ``Listing`` objects.

    Experimental — included in selected default vertical profiles for this
    single-user Raven deployment. Public SSR access is fragile; login/CAPTCHA
    blockers may appear. When blockers are detected, logs a warning. Returns any SSR
    listings extracted from the page when present; otherwise returns ``[]``.
    Never raises.

    Extra keyword arguments (e.g. ``max_price``, ``min_price``, ``radius``,
    ``condition``) are accepted for hunt fan-out compatibility and ignored.
    """
    if kwargs:
        log.debug(
            "facebook_marketplace: ignoring unsupported adapter kwargs: %s",
            sorted(kwargs.keys()),
        )
    location = city or "Houston, TX"
    cap = max(1, min(limit, 50))

    log.info(
        "facebook_marketplace search: query=%r city=%r limit=%d "
        "(experimental; explicit opt-in only)",
        query,
        location,
        cap,
    )

    try:
        html, final_url, page_title, slug = _fetch_search_html(query, location)
        if html is None:
            log.warning(
                "facebook_marketplace: no HTML for query=%r city=%r",
                query,
                location,
            )
            return []

        listings, blockers = parse_search_html(
            html,
            query,
            final_url=final_url or "",
            page_title=page_title,
            requested_slug=slug,
            limit=cap,
        )

        if blockers:
            _log_blocker_warning(blockers, query, len(listings))
            if not listings:
                return []

        if not listings:
            log.warning(
                "facebook_marketplace: query=%r returned 0 usable listings",
                query,
            )
            return []

        log.info(
            "facebook_marketplace: query=%r -> %d listing(s)",
            query,
            len(listings),
        )
        return listings

    except Exception as exc:
        log.error(
            "facebook_marketplace: unexpected error for query=%r: %s",
            query,
            exc,
        )
        return []
