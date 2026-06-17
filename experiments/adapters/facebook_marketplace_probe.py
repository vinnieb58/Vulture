"""
Facebook Marketplace Playwright reconnaissance probe
======================================================
Reconnaissance only. Does NOT write to SQLite, send Discord alerts,
register in adapters/registry.py, touch .env, or add Facebook as a runtime source.

No login automation, stored cookies, or production adapter integration.

Usage (anonymous / no saved session):
    python experiments/adapters/facebook_marketplace_probe.py
    python experiments/adapters/facebook_marketplace_probe.py --query "rtx 3080"
    python experiments/adapters/facebook_marketplace_probe.py --query "rtx 3080" --limit 10 --screenshot-on-fail

Manual profile setup (one-time, headed; you log in yourself — no credential automation):
    python experiments/adapters/facebook_marketplace_probe.py --setup-profile \\
        --profile-dir artifacts/facebook_marketplace_probe/profiles/manual_fb_profile --headed

Probe with saved profile:
    python experiments/adapters/facebook_marketplace_probe.py --query "rtx 3080" --limit 10 \\
        --profile-dir artifacts/facebook_marketplace_probe/profiles/manual_fb_profile --screenshot-on-fail

Prerequisites:
    pip install playwright beautifulsoup4 lxml
    python -m playwright install chromium
"""

from __future__ import annotations

import argparse
import re
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Optional
from urllib.parse import quote_plus, urljoin

from bs4 import BeautifulSoup

try:
    from playwright.sync_api import (
        BrowserContext,
        Page,
        Playwright,
        TimeoutError as PlaywrightTimeout,
        sync_playwright,
    )
except ImportError:
    print("ERROR: playwright is not installed.")
    print("Install: pip install playwright beautifulsoup4 lxml")
    print("Then:    python -m playwright install chromium")
    sys.exit(1)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

FACEBOOK_ORIGIN = "https://www.facebook.com"
MARKETPLACE_SEARCH_BASE = f"{FACEBOOK_ORIGIN}/marketplace/search"

DEFAULT_QUERIES = ["rtx 3080", "toyota sequoia", "75 inch tv"]

ARTIFACTS_DIR = Path("artifacts/facebook_marketplace_probe")
DEFAULT_PROFILE_DIR = ARTIFACTS_DIR / "profiles" / "manual_fb_profile"
MARKETPLACE_HOME_URL = f"{FACEBOOK_ORIGIN}/marketplace"

CHROMIUM_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--disable-dev-shm-usage",
    "--no-sandbox",
    "--disable-infobars",
]

VIEWPORT = {"width": 1280, "height": 900}
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
LOCALE = "en-US"
TIMEZONE = "America/Chicago"

INITIAL_LOAD_MS = 3_500
SCROLL_PAUSE_MS = 1_200
SCROLL_COUNT = 3
DEFAULT_LIMIT = 10
DEFAULT_TIMEOUT_MS = 90_000

LOGIN_URL_FRAGMENTS = ["login.facebook.com", "/login.php", "/login/"]
LOGIN_TEXT_MARKERS = [
    "log in to facebook",
    "log into facebook",
    "log in to continue",
    "you must log in",
    "create new account",
    "forgot password",
]
LOGIN_DOM_MARKERS = [
    'id="loginform"',
    'name="login"',
    'data-testid="royal_login_form"',
]

CHECKPOINT_URL_FRAGMENTS = ["checkpoint", "/challenge/"]
CHECKPOINT_TEXT_MARKERS = [
    "confirm it's you",
    "confirm its you",
    "security check",
    "help us confirm",
    "enter security code",
    "two-factor authentication",
    "approve this login",
    "suspicious activity",
]

MARKETPLACE_CONTENT_MARKERS = [
    "/marketplace/item/",
    "marketplace/search",
    "marketplace/browse",
    "marketplace/you",
]

LISTING_CARD_SELECTORS = [
    'a[href*="/marketplace/item/"]',
    '[data-pagelet*="Marketplace"]',
    '[role="article"]',
    'div[class*="marketplace"]',
    'div[aria-label*="Marketplace"]',
]

FIELD_SELECTORS = {
    "title": [
        "span[dir='auto']",
        "span",
        "a[href*='/marketplace/item/']",
    ],
    "price": [
        "span:contains('$')",
    ],
    "location": [
        "span[dir='auto']",
    ],
}


@dataclass
class CandidateListing:
    title: Optional[str]
    price: Optional[str]
    location: Optional[str]
    link: Optional[str]


@dataclass
class QueryDiagnostics:
    query: str
    requested_url: str
    final_url: str
    page_title: str
    http_status: Optional[int]
    html_length: int
    load_ms: int
    login_required: Literal["yes", "no", "unknown"]
    challenge_detected: Literal["yes", "no", "unknown"]
    marketplace_content: bool
    candidate_elements: int
    listings: list[CandidateListing]
    nav_error: Optional[str]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")


def build_search_url(query: str) -> str:
    return f"{MARKETPLACE_SEARCH_BASE}/?query={quote_plus(query)}"


def _safe_text(el) -> Optional[str]:
    if el is None:
        return None
    try:
        text = el.get_text(" ", strip=True)
        return text if text else None
    except Exception:
        return None


def normalize_marketplace_link(href: Optional[str]) -> Optional[str]:
    if not href:
        return None
    try:
        href = str(href).strip()
        if not href or href in ("#", "javascript:void(0)"):
            return None
        if href.startswith("/"):
            href = urljoin(FACEBOOK_ORIGIN, href)
        if "/marketplace/item/" not in href:
            return None
        # Strip tracking query params but keep path
        match = re.match(r"(https?://[^?#]+/marketplace/item/\d+)", href)
        if match:
            return match.group(1)
        if href.startswith("http"):
            return href.split("?")[0]
        return href
    except Exception:
        return None


def _detect_login(final_url: str, title: str, html: str) -> Literal["yes", "no", "unknown"]:
    url_lower = (final_url or "").lower()
    title_lower = (title or "").lower()
    html_lower = (html or "").lower()

    if any(frag in url_lower for frag in LOGIN_URL_FRAGMENTS):
        return "yes"
    if "checkpoint" in url_lower and "login" in url_lower:
        return "yes"
    if any(m in title_lower for m in LOGIN_TEXT_MARKERS):
        return "yes"
    if any(m in html_lower for m in LOGIN_TEXT_MARKERS[:4]):
        return "yes"
    if any(m in html_lower for m in LOGIN_DOM_MARKERS):
        return "yes"

    if "/marketplace/" in url_lower and "log in" not in title_lower:
        return "no"
    if not html:
        return "unknown"
    return "unknown"


def _detect_challenge(final_url: str, title: str, html: str) -> Literal["yes", "no", "unknown"]:
    url_lower = (final_url or "").lower()
    title_lower = (title or "").lower()
    html_lower = (html or "").lower()

    # Login wall is not a checkpoint — see login_required.
    if any(frag in url_lower for frag in LOGIN_URL_FRAGMENTS):
        return "no"

    if any(frag in url_lower for frag in CHECKPOINT_URL_FRAGMENTS):
        return "yes"
    if any(m in title_lower or m in html_lower for m in CHECKPOINT_TEXT_MARKERS):
        return "yes"
    if "captcha" in html_lower and "marketplace/item" not in html_lower:
        return "yes"

    if "/marketplace/" in url_lower:
        return "no"
    if not html:
        return "unknown"
    return "unknown"


def _marketplace_content_present(html: str, final_url: str) -> bool:
    combined = f"{html} {final_url}".lower()
    return any(m in combined for m in MARKETPLACE_CONTENT_MARKERS)


def _count_candidate_elements(soup: BeautifulSoup) -> int:
    best = 0
    for sel in LISTING_CARD_SELECTORS:
        try:
            if ":contains" in sel:
                continue
            n = len(soup.select(sel))
            if n > best:
                best = n
        except Exception:
            continue
    try:
        item_links = soup.select('a[href*="/marketplace/item/"]')
        if len(item_links) > best:
            best = len(item_links)
    except Exception:
        pass
    return best


def _price_like(text: Optional[str]) -> bool:
    if not text:
        return False
    return bool(re.search(r"\$\s*[\d,]+", text))


def _extract_price_from_text(text: str) -> Optional[str]:
    match = re.search(r"\$\s*[\d,]+(?:\.\d{2})?", text)
    return match.group(0).strip() if match else None


def _find_card_for_link(link_el, soup: BeautifulSoup):
    """Walk up from item link to a reasonable listing container."""
    parent = link_el.parent
    for _ in range(8):
        if parent is None or parent.name in ("body", "html", "[document]"):
            break
        if parent.name in ("div", "li", "article", "span") and parent.get("role") == "article":
            return parent
        spans = parent.find_all("span", limit=20) if hasattr(parent, "find_all") else []
        if len(spans) >= 2:
            return parent
        parent = getattr(parent, "parent", None)
    return link_el.parent


def _extract_listings_from_html(html: str, limit: int) -> tuple[list[CandidateListing], int]:
    soup = BeautifulSoup(html, "lxml")
    candidate_count = _count_candidate_elements(soup)

    seen_links: set[str] = set()
    results: list[CandidateListing] = []

    try:
        item_links = soup.select('a[href*="/marketplace/item/"]')
    except Exception:
        item_links = []

    for link_el in item_links:
        if len(results) >= limit:
            break
        try:
            href = link_el.get("href")
            link = normalize_marketplace_link(href)
            if not link or link in seen_links:
                continue
            seen_links.add(link)

            card = _find_card_for_link(link_el, soup)
            card_text = ""
            if card is not None:
                try:
                    card_text = card.get_text(" ", strip=True)
                except Exception:
                    card_text = ""

            title = _safe_text(link_el)
            if title and len(title) < 3:
                title = None
            if not title and card is not None:
                for span in card.find_all("span", limit=15):
                    t = _safe_text(span)
                    if t and len(t) > 5 and not _price_like(t) and "mi away" not in t.lower():
                        title = t
                        break

            price = _extract_price_from_text(card_text) if card_text else None
            if not price and card is not None:
                for span in card.find_all("span", limit=20):
                    t = _safe_text(span)
                    if _price_like(t):
                        price = _extract_price_from_text(t or "")
                        break

            location = None
            if card_text:
                loc_match = re.search(
                    r"([A-Za-z][A-Za-z\s.'-]+,\s*[A-Z]{2})\b",
                    card_text,
                )
                if loc_match:
                    location = loc_match.group(1).strip()
                else:
                    away_match = re.search(r"(\d+(?:\.\d+)?\s*(?:mi|km)\s+away)", card_text, re.I)
                    if away_match:
                        location = away_match.group(1).strip()

            if not location and card is not None:
                for span in card.find_all("span", limit=20):
                    t = _safe_text(span)
                    if not t:
                        continue
                    if re.search(r"\b(?:mi|km)\s+away\b", t, re.I) or re.search(
                        r",\s*[A-Z]{2}\b", t
                    ):
                        location = t
                        break

            results.append(
                CandidateListing(
                    title=title,
                    price=price,
                    location=location,
                    link=link,
                )
            )
        except Exception:
            continue

    return results, candidate_count


def _scroll_page(page: Page, scroll_count: int, pause_ms: int) -> None:
    for i in range(scroll_count):
        try:
            page.evaluate(
                """() => {
                    const h = document.body ? document.body.scrollHeight : 0;
                    window.scrollTo(0, Math.min(h, (window.scrollY || 0) + window.innerHeight * 0.85));
                }"""
            )
            page.wait_for_timeout(pause_ms)
        except Exception:
            break


def _save_screenshot(page: Page, query: str, reason: str) -> Optional[Path]:
    try:
        ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
        safe_query = re.sub(r"[^\w\-]+", "_", query)[:40]
        path = ARTIFACTS_DIR / f"fb_marketplace_{safe_query}_{_timestamp()}_{reason}.png"
        page.screenshot(path=str(path), full_page=True)
        print(f"  Screenshot saved: {path}")
        return path
    except Exception as exc:
        print(f"  Screenshot failed: {exc}")
        return None


def _extraction_quality(listings: list[CandidateListing]) -> Literal["none", "partial", "good"]:
    if not listings:
        return "none"
    with_title = sum(1 for x in listings if x.title)
    with_price = sum(1 for x in listings if x.price)
    with_link = sum(1 for x in listings if x.link)
    if with_title >= 3 and with_price >= 2 and with_link >= 3:
        return "good"
    if with_link >= 1 or with_title >= 1:
        return "partial"
    return "none"


def _recommended_next_step(
    reachable: bool,
    login_required: str,
    challenge_detected: str,
    extraction_quality: str,
    headed: bool,
    using_profile: bool,
    profile_login_worked: Optional[bool],
) -> str:
    if not reachable:
        return "retry headed"
    if challenge_detected == "yes":
        return "retry headed"
    if using_profile and profile_login_worked is False:
        return "re-run --setup-profile (session expired or invalid)"
    if login_required == "yes" and not using_profile:
        return "try browser profile"
    if login_required == "yes":
        return "re-run --setup-profile"
    if extraction_quality == "good":
        return "build experimental adapter"
    if extraction_quality == "partial":
        return "build experimental adapter"
    if using_profile and profile_login_worked:
        return "improve parser selectors"
    if not headed:
        return "retry headed"
    return "abandon"


def _profile_dir_has_data(profile_dir: Path) -> bool:
    if not profile_dir.exists():
        return False
    try:
        return any(profile_dir.iterdir())
    except OSError:
        return False


def _stealth_init_script() -> str:
    return "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"


def _open_persistent_context(
    pw: Playwright,
    profile_dir: Path,
    *,
    headed: bool,
    slowmo: int,
) -> BrowserContext:
    profile_dir.mkdir(parents=True, exist_ok=True)
    context = pw.chromium.launch_persistent_context(
        str(profile_dir.resolve()),
        headless=not headed,
        slow_mo=slowmo,
        viewport=VIEWPORT,
        user_agent=USER_AGENT,
        locale=LOCALE,
        timezone_id=TIMEZONE,
        args=CHROMIUM_ARGS,
        extra_http_headers={
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate, br",
        },
    )
    context.add_init_script(_stealth_init_script())
    return context


def _open_ephemeral_context(
    pw: Playwright,
    *,
    headed: bool,
    slowmo: int,
) -> tuple[Any, BrowserContext]:
    launch_opts: dict[str, Any] = {
        "headless": not headed,
        "slow_mo": slowmo,
        "args": CHROMIUM_ARGS,
    }
    browser = pw.chromium.launch(**launch_opts)
    context = browser.new_context(
        viewport=VIEWPORT,
        user_agent=USER_AGENT,
        locale=LOCALE,
        timezone_id=TIMEZONE,
        extra_http_headers={
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate, br",
        },
    )
    context.add_init_script(_stealth_init_script())
    return browser, context


def _get_or_new_page(context: BrowserContext) -> Page:
    if context.pages:
        return context.pages[0]
    return context.new_page()


def _location_controllability_hint(results: list[QueryDiagnostics]) -> str:
    """Heuristic: varied listing locations suggest geo is active; URL params suggest URL control."""
    locations: list[str] = []
    for r in results:
        for listing in r.listings:
            if listing.location:
                locations.append(listing.location.strip().lower())
    if not locations:
        return "unknown (no location strings extracted)"
    unique = sorted(set(locations))
    if len(unique) >= 2:
        return f"possibly yes (saw {len(unique)} distinct location strings; check Marketplace radius UI)"
    return f"unclear (only {unique[0]!r} observed; try changing Marketplace location in profile UI)"


def setup_profile(profile_dir: Path, headed: bool, slowmo: int) -> None:
    """Headed persistent context: user logs in manually; session saved in profile_dir."""
    if not headed:
        print(
            "WARNING: --setup-profile works best with --headed so you can log in in the browser."
        )
    profile_dir = profile_dir.resolve()
    print()
    print("=" * 72)
    print("  FACEBOOK MARKETPLACE — MANUAL PROFILE SETUP")
    print("=" * 72)
    print(f"  Profile directory: {profile_dir}")
    print("  This probe does NOT automate login or store credentials in the repo.")
    print("  Session data stays on disk only under the profile path (gitignored).")
    print()

    with sync_playwright() as pw:
        try:
            context = _open_persistent_context(pw, profile_dir, headed=True, slowmo=slowmo)
        except Exception as exc:
            print(f"ERROR: Could not launch persistent browser: {exc}")
            print("Install browsers: python -m playwright install chromium")
            sys.exit(1)

        page = _get_or_new_page(context)
        try:
            page.goto(
                MARKETPLACE_HOME_URL,
                wait_until="domcontentloaded",
                timeout=DEFAULT_TIMEOUT_MS,
            )
        except Exception as exc:
            print(f"WARNING: Initial navigation issue (continue in browser): {exc}")

        print("  Instructions:")
        print("    1. In the browser window, log in to Facebook if prompted.")
        print("    2. Confirm Marketplace loads (browse/search works).")
        print("    3. Optionally set your Marketplace location/radius in the UI.")
        print("    4. Return here and press Enter to save the profile and exit.")
        print()
        print("  Do NOT paste passwords or tokens into this terminal.")
        print()

        try:
            input("  Press Enter when finished logging in and Marketplace is reachable... ")
        except (EOFError, KeyboardInterrupt):
            print("\n  Setup cancelled — closing browser without further prompts.")
        finally:
            try:
                context.close()
            except Exception:
                pass

    print()
    if _profile_dir_has_data(profile_dir):
        print(f"  Profile saved under: {profile_dir}")
        print("  Next: run probe with --profile-dir pointing at this path.")
    else:
        print(f"  WARNING: Profile directory looks empty: {profile_dir}")
    print("=" * 72)
    print()


def clear_profile_warning_only(profile_dir: Path) -> None:
    profile_dir = profile_dir.resolve()
    print()
    print("=" * 72)
    print("  CLEAR PROFILE (warning only — no files deleted)")
    print("=" * 72)
    if not profile_dir.exists():
        print(f"  Profile path does not exist: {profile_dir}")
    elif not _profile_dir_has_data(profile_dir):
        print(f"  Profile path exists but appears empty: {profile_dir}")
    else:
        print(f"  To remove saved session data, delete this directory yourself:")
        print(f"    rm -rf {profile_dir}")
        print("  Or on Windows, remove the folder via File Explorer.")
        print("  Never commit profile contents to git.")
    print("=" * 72)
    print()


def _print_query_diagnostics(d: QueryDiagnostics) -> None:
    sep = "=" * 72
    print()
    print(sep)
    print(f"  FACEBOOK MARKETPLACE PROBE   query={d.query!r}")
    print(sep)
    print(f"  Query                  : {d.query}")
    print(f"  Requested URL          : {d.requested_url}")
    print(f"  Final URL              : {d.final_url}")
    print(f"  Page title             : {d.page_title!r}")
    print(f"  HTTP status            : {d.http_status}")
    print(f"  HTML length            : {d.html_length:,} bytes")
    print(f"  Load time              : {d.load_ms} ms")
    print(f"  Login required         : {d.login_required}")
    print(f"  Challenge detected     : {d.challenge_detected}")
    print(f"  Marketplace content    : {'yes' if d.marketplace_content else 'no'}")
    print(f"  Candidate elements     : ~{d.candidate_elements}")
    if d.nav_error:
        print(f"  Navigation error       : {d.nav_error}")

    print()
    print(f"--- Extracted listings (up to {len(d.listings)}) ---")
    if not d.listings:
        print("  (none)")
    else:
        for i, listing in enumerate(d.listings, 1):
            print(f"\n  [{i}]")
            for k, v in asdict(listing).items():
                print(f"    {k}: {v!r}")


# ---------------------------------------------------------------------------
# Probe run
# ---------------------------------------------------------------------------


def probe_query(
    page: Page,
    query: str,
    limit: int,
    timeout_ms: int,
    screenshot_on_fail: bool,
) -> QueryDiagnostics:
    requested_url = build_search_url(query)
    t0 = time.monotonic()
    http_status: Optional[int] = None
    nav_error: Optional[str] = None
    html = ""
    final_url = requested_url
    page_title = ""

    try:
        response = page.goto(
            requested_url,
            wait_until="domcontentloaded",
            timeout=timeout_ms,
        )
        http_status = response.status if response else None
        final_url = page.url
        page_title = page.title() or ""
    except PlaywrightTimeout as exc:
        nav_error = f"navigation_timeout: {exc}"
    except Exception as exc:
        nav_error = f"navigation_error: {exc}"

    if not nav_error:
        try:
            page.wait_for_timeout(INITIAL_LOAD_MS)
            _scroll_page(page, SCROLL_COUNT, SCROLL_PAUSE_MS)
            page.wait_for_timeout(SCROLL_PAUSE_MS)
            html = page.content()
            final_url = page.url
            page_title = page.title() or ""
        except Exception as exc:
            nav_error = nav_error or f"content_error: {exc}"
            try:
                html = page.content()
            except Exception:
                html = ""

    load_ms = int((time.monotonic() - t0) * 1000)
    html_length = len(html)

    login_required = _detect_login(final_url, page_title, html)
    challenge_detected = _detect_challenge(final_url, page_title, html)
    marketplace_content = _marketplace_content_present(html, final_url)

    listings: list[CandidateListing] = []
    candidate_elements = 0
    if html:
        try:
            listings, candidate_elements = _extract_listings_from_html(html, limit)
        except Exception:
            listings = []
            candidate_elements = 0

    diag = QueryDiagnostics(
        query=query,
        requested_url=requested_url,
        final_url=final_url,
        page_title=page_title,
        http_status=http_status,
        html_length=html_length,
        load_ms=load_ms,
        login_required=login_required,
        challenge_detected=challenge_detected,
        marketplace_content=marketplace_content,
        candidate_elements=candidate_elements,
        listings=listings,
        nav_error=nav_error,
    )

    _print_query_diagnostics(diag)

    should_screenshot = screenshot_on_fail and (
        nav_error
        or login_required == "yes"
        or challenge_detected == "yes"
        or not marketplace_content
        or len(listings) == 0
    )
    if should_screenshot:
        reason = "fail" if nav_error else "diagnostic"
        _save_screenshot(page, query, reason)

    return diag


def run_probe(
    queries: list[str],
    limit: int,
    headed: bool,
    slowmo: int,
    timeout_ms: int,
    screenshot_on_fail: bool,
    profile_dir: Optional[Path] = None,
) -> list[QueryDiagnostics]:
    using_profile = profile_dir is not None
    sep = "=" * 72
    print(sep)
    print("  FACEBOOK MARKETPLACE PLAYWRIGHT PROBE (experimental, isolated)")
    print(f"  queries={queries}")
    print(
        f"  mode={'headed' if headed else 'headless'}  limit={limit}  "
        f"slowmo={slowmo}  timeout_ms={timeout_ms}  screenshot_on_fail={screenshot_on_fail}"
    )
    if using_profile:
        print(f"  profile_dir={profile_dir.resolve()}")
        if not _profile_dir_has_data(profile_dir):
            print("  WARNING: profile directory missing or empty — run --setup-profile first")
    else:
        print("  profile_dir=(none — anonymous session)")
    print(sep)

    if headed:
        print("\n  NOTE: --headed requires a display. On headless servers try:")
        print("        xvfb-run python experiments/adapters/facebook_marketplace_probe.py ... --headed\n")

    results: list[QueryDiagnostics] = []

    try:
        with sync_playwright() as pw:
            browser: Any = None
            try:
                if using_profile:
                    context = _open_persistent_context(
                        pw, profile_dir, headed=headed, slowmo=slowmo
                    )
                else:
                    browser, context = _open_ephemeral_context(
                        pw, headed=headed, slowmo=slowmo
                    )
            except Exception as exc:
                print(f"\nERROR: Browser launch failed: {exc}")
                print("Install browsers: python -m playwright install chromium")
                sys.exit(1)

            page = _get_or_new_page(context)

            for query in queries:
                results.append(
                    probe_query(
                        page=page,
                        query=query,
                        limit=limit,
                        timeout_ms=timeout_ms,
                        screenshot_on_fail=screenshot_on_fail,
                    )
                )
                if query != queries[-1]:
                    try:
                        page.wait_for_timeout(1_500)
                    except Exception:
                        pass

            context.close()
            if browser is not None:
                browser.close()
    except Exception as exc:
        print(f"\nFATAL: Probe failed: {exc}")
        sys.exit(1)

    return results


def _profile_login_worked(
    results: list[QueryDiagnostics], using_profile: bool
) -> Optional[bool]:
    if not using_profile:
        return None
    if not results:
        return False
    logged_in_signals = 0
    for r in results:
        on_marketplace = "/marketplace" in (r.final_url or "").lower()
        not_login_url = r.login_required != "yes"
        has_content = r.marketplace_content or len(r.listings) > 0
        if on_marketplace and not_login_url and has_content:
            logged_in_signals += 1
    if logged_in_signals == len(results):
        return True
    if all(r.login_required == "yes" for r in results):
        return False
    if any(r.login_required == "yes" for r in results):
        return False
    return logged_in_signals > 0


def _print_summary(
    results: list[QueryDiagnostics],
    headed: bool,
    profile_dir: Optional[Path] = None,
) -> None:
    using_profile = profile_dir is not None
    profile_login = _profile_login_worked(results, using_profile)
    total_listings = sum(len(r.listings) for r in results)
    reachable = any(
        r.http_status == 200 or (r.html_length > 5_000 and not r.nav_error) for r in results
    )
    login_vals = {r.login_required for r in results}
    challenge_vals = {r.challenge_detected for r in results}

    if login_vals == {"yes"}:
        login_required: Literal["yes", "no", "unknown"] = "yes"
    elif login_vals <= {"no"}:
        login_required = "no"
    else:
        login_required = "unknown"

    if challenge_vals == {"yes"}:
        challenge_detected: Literal["yes", "no", "unknown"] = "yes"
    elif challenge_vals <= {"no"}:
        challenge_detected = "no"
    else:
        challenge_detected = "unknown"

    qualities = [_extraction_quality(r.listings) for r in results]
    if "good" in qualities:
        extraction_quality: Literal["none", "partial", "good"] = "good"
    elif "partial" in qualities:
        extraction_quality = "partial"
    else:
        extraction_quality = "none"

    recommended = _recommended_next_step(
        reachable=reachable,
        login_required=login_required,
        challenge_detected=challenge_detected,
        extraction_quality=extraction_quality,
        headed=headed,
        using_profile=using_profile,
        profile_login_worked=profile_login,
    )
    location_hint = _location_controllability_hint(results)

    print()
    print("=" * 72)
    print("FACEBOOK MARKETPLACE PROBE SUMMARY")
    print(f"- reachable: {'yes' if reachable else 'no'}")
    if using_profile:
        plw = "yes" if profile_login is True else "no" if profile_login is False else "unknown"
        print(f"- profile_login_worked: {plw}")
    print(f"- login_required: {login_required}")
    print(f"- challenge_detected: {challenge_detected}")
    print(f"- listings_found: {total_listings}")
    print(f"- extraction_quality: {extraction_quality}")
    print(f"- location_controllability: {location_hint}")
    print(f"- recommended_next_step: {recommended}")
    print("=" * 72)
    print()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Facebook Marketplace Playwright recon probe (experiments only)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python experiments/adapters/facebook_marketplace_probe.py
  python experiments/adapters/facebook_marketplace_probe.py --query "rtx 3080" --limit 10
  python experiments/adapters/facebook_marketplace_probe.py --setup-profile \\
      --profile-dir artifacts/facebook_marketplace_probe/profiles/manual_fb_profile --headed
  python experiments/adapters/facebook_marketplace_probe.py --query "rtx 3080" --limit 10 \\
      --profile-dir artifacts/facebook_marketplace_probe/profiles/manual_fb_profile
""",
    )
    p.add_argument(
        "--profile-dir",
        type=Path,
        dest="profile_dir",
        metavar="PATH",
        help=(
            "Playwright persistent profile directory (gitignored). "
            f"Suggested default: {DEFAULT_PROFILE_DIR}"
        ),
    )
    p.add_argument(
        "--setup-profile",
        action="store_true",
        dest="setup_profile",
        help=(
            "Open headed browser at Marketplace for manual login; "
            "press Enter in terminal when done (requires --profile-dir)"
        ),
    )
    p.add_argument(
        "--clear-profile-warning-only",
        action="store_true",
        dest="clear_profile_warning",
        help="Print how to delete the profile directory; does not delete files",
    )
    p.add_argument(
        "--query",
        action="append",
        dest="queries",
        metavar="TEXT",
        help="Search term (repeatable). Default: rtx 3080, toyota sequoia, 75 inch tv",
    )
    p.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_LIMIT,
        help=f"Max listings to extract per query (default: {DEFAULT_LIMIT})",
    )
    p.add_argument(
        "--headed",
        action="store_true",
        help="Run visible browser (use xvfb-run on headless servers)",
    )
    p.add_argument(
        "--slowmo",
        type=int,
        default=0,
        metavar="MS",
        help="Playwright slow_mo delay in milliseconds (default: 0)",
    )
    p.add_argument(
        "--timeout-ms",
        type=int,
        default=DEFAULT_TIMEOUT_MS,
        dest="timeout_ms",
        help=f"Navigation timeout in ms (default: {DEFAULT_TIMEOUT_MS})",
    )
    p.add_argument(
        "--screenshot-on-fail",
        action="store_true",
        dest="screenshot_on_fail",
        help=f"Save PNG under {ARTIFACTS_DIR}/ on failure or empty extraction",
    )
    return p


def main() -> None:
    args = _build_parser().parse_args()
    profile_dir: Optional[Path] = args.profile_dir

    if args.clear_profile_warning:
        clear_profile_warning_only(profile_dir or DEFAULT_PROFILE_DIR)
        sys.exit(0)

    if args.setup_profile:
        if not profile_dir:
            print("ERROR: --setup-profile requires --profile-dir")
            print(f"  Example: --profile-dir {DEFAULT_PROFILE_DIR}")
            sys.exit(1)
        setup_profile(profile_dir, headed=args.headed or True, slowmo=args.slowmo)
        sys.exit(0)

    queries = args.queries if args.queries else DEFAULT_QUERIES
    results = run_probe(
        queries=queries,
        limit=args.limit,
        headed=args.headed,
        slowmo=args.slowmo,
        timeout_ms=args.timeout_ms,
        screenshot_on_fail=args.screenshot_on_fail,
        profile_dir=profile_dir,
    )
    _print_summary(results, headed=args.headed, profile_dir=profile_dir)
    sys.exit(0)


if __name__ == "__main__":
    main()
