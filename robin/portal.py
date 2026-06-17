"""Daycare portal browser automation for Robin photo discovery (read-only)."""

from __future__ import annotations

import logging
import os
import re
import time
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

from robin.config import RobinConfig
from robin.redact import redact_text

log = logging.getLogger(__name__)

STORAGE_STATE_FILENAME = "daycare_storage_state.json"
MANUAL_LOGIN_TIMEOUT_S = 600

_PHOTO_LINK_PATTERNS = [
    re.compile(r"photo", re.I),
    re.compile(r"gallery", re.I),
    re.compile(r"image", re.I),
    re.compile(r"media", re.I),
    re.compile(r"album", re.I),
    re.compile(r"picture", re.I),
]

_LOGIN_FORM_MARKERS = [
    'input[type="password"]',
    'input[name*="password" i]',
    'form[action*="login" i]',
    'button:has-text("Log In")',
    'button:has-text("Sign In")',
]

_LOGGED_IN_INDICATORS = [
    re.compile(r"log\s*out", re.I),
    re.compile(r"sign\s*out", re.I),
]

_LOGGED_OUT_URL_TOKENS = (
    "/login",
    "/sign_in",
    "/signin",
    "/users/sign_in",
    "/users/login",
)

_CHALLENGE_TITLE_FRAGMENTS = [
    "just a moment",
    "attention required",
    "access denied",
    "checking your browser",
    "please wait",
    "security check",
]

_DATE_PATTERNS = [
    re.compile(r"\b(\d{4}-\d{2}-\d{2})\b"),
    re.compile(r"\b(\d{1,2}/\d{1,2}/\d{4})\b"),
    re.compile(r"\b(\d{1,2}-\d{1,2}-\d{4})\b"),
]

_IMAGE_URL_RE = re.compile(
    r"https?://[^\s\"'<>]+?\.(?:jpg|jpeg|png|gif|webp|heic|heif)(?:\?[^\s\"'<>]*)?",
    re.IGNORECASE,
)

_ICON_HINTS = ("icon", "logo", "avatar", "sprite", "favicon", "badge", "emoji")


@dataclass(frozen=True)
class PhotoCandidate:
    url: str
    detected_date: date | None = None
    label: str | None = None
    source_page: str | None = None


class RobinPortalError(Exception):
    """Raised when portal navigation or discovery fails."""


def _playwright_available() -> bool:
    try:
        import playwright.sync_api  # noqa: F401

        return True
    except ImportError:
        return False


def storage_state_path(session_dir: Path) -> Path:
    return session_dir / STORAGE_STATE_FILENAME


def has_display() -> bool:
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def resolve_headful(*, headful_flag: bool, config: RobinConfig) -> bool:
    if headful_flag:
        return True
    return not config.headless


def _parse_detected_date(text: str | None) -> date | None:
    if not text:
        return None
    for pattern in _DATE_PATTERNS:
        match = pattern.search(text)
        if not match:
            continue
        raw = match.group(1)
        for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m-%d-%Y"):
            try:
                return datetime.strptime(raw, fmt).date()
            except ValueError:
                continue
    return None


def _detect_photo_date(url: str, context: str | None = None) -> date | None:
    """Prefer explicit dates in the URL path over nearby page text."""
    return _parse_detected_date(url) or _parse_detected_date(context)


def _looks_like_icon(url: str, label: str | None = None) -> bool:
    haystack = f"{url} {label or ''}".lower()
    return any(hint in haystack for hint in _ICON_HINTS)


def _normalize_image_url(base_url: str, raw_url: str) -> str | None:
    if not raw_url or raw_url.startswith("data:"):
        return None
    absolute = urljoin(base_url, raw_url.strip())
    parsed = urlparse(absolute)
    if parsed.scheme not in {"http", "https"}:
        return None
    return absolute


def _extract_srcset_url(srcset: str) -> str | None:
    for part in srcset.split(","):
        token = part.strip().split()
        if token:
            return token[0]
    return None


def discover_photo_candidates_from_html(
    html: str,
    *,
    base_url: str,
    page_label: str | None = None,
    since_date: date | None = None,
) -> list[PhotoCandidate]:
    """
    Discover image candidates from HTML without a browser.

    Useful for unit tests and offline fixture parsing.
    """
    candidates: list[PhotoCandidate] = []
    seen: set[str] = set()

    for match in _IMAGE_URL_RE.finditer(html):
        url = _normalize_image_url(base_url, match.group(0))
        if not url or url in seen or _looks_like_icon(url):
            continue
        seen.add(url)
        context_start = max(0, match.start() - 120)
        context_end = min(len(html), match.end() + 120)
        context = html[context_start:context_end]
        detected = _detect_photo_date(url, context)
        if since_date and detected and detected < since_date:
            continue
        candidates.append(
            PhotoCandidate(
                url=url,
                detected_date=detected,
                source_page=page_label or base_url,
            )
        )

    return candidates


def discover_photo_candidates_on_page(
    page: Any,
    *,
    since_date: date | None = None,
) -> list[PhotoCandidate]:
    """Collect photo candidates from the current Playwright page."""
    base_url = page.url
    candidates: list[PhotoCandidate] = []
    seen: set[str] = set()

    img_locator = page.locator("img")
    count = min(img_locator.count(), 500)
    for idx in range(count):
        img = img_locator.nth(idx)
        try:
            if not img.is_visible():
                continue
        except Exception:
            continue

        src = img.get_attribute("src")
        data_src = img.get_attribute("data-src")
        srcset = img.get_attribute("srcset")
        alt = (img.get_attribute("alt") or "").strip() or None

        raw_urls = [value for value in (src, data_src) if value]
        if srcset:
            srcset_url = _extract_srcset_url(srcset)
            if srcset_url:
                raw_urls.append(srcset_url)

        nearby_text = ""
        try:
            nearby_text = str(
                img.evaluate(
                    "el => (el.closest('article,li,div,section') || el.parentElement)?.innerText || ''"
                )
            )
        except Exception:
            nearby_text = alt or ""

        for raw_url in raw_urls:
            url = _normalize_image_url(base_url, raw_url)
            if not url or url in seen or _looks_like_icon(url, alt):
                continue
            detected = _detect_photo_date(url, nearby_text)
            if since_date and detected and detected < since_date:
                continue
            seen.add(url)
            candidates.append(
                PhotoCandidate(
                    url=url,
                    detected_date=detected,
                    label=alt,
                    source_page=base_url,
                )
            )

    for match in _IMAGE_URL_RE.finditer(page.content()):
        url = _normalize_image_url(base_url, match.group(0))
        if not url or url in seen or _looks_like_icon(url):
            continue
        context_start = max(0, match.start() - 120)
        context_end = min(len(page.content()), match.end() + 120)
        context = page.content()[context_start:context_end]
        detected = _detect_photo_date(url, context)
        if since_date and detected and detected < since_date:
            continue
        seen.add(url)
        candidates.append(
            PhotoCandidate(
                url=url,
                detected_date=detected,
                source_page=base_url,
            )
        )

    return candidates


def page_has_login_form(page: Any) -> bool:
    for selector in _LOGIN_FORM_MARKERS:
        try:
            if page.locator(selector).count() > 0:
                return True
        except Exception:
            continue
    return False


def page_looks_logged_in(page: Any) -> bool:
    if page_has_login_form(page):
        return False
    url = page.url.lower()
    if any(token in url for token in _LOGGED_OUT_URL_TOKENS):
        return False
    try:
        text = page.inner_text("body")
    except Exception:
        text = page.content()
    if any(pattern.search(text) for pattern in _LOGGED_IN_INDICATORS):
        return True
    return False


def detect_blockers(page: Any) -> list[str]:
    blockers: list[str] = []
    title = (page.title() or "").lower()
    if any(fragment in title for fragment in _CHALLENGE_TITLE_FRAGMENTS):
        blockers.append("challenge_page")
    return blockers


def wait_for_manual_login(page: Any, *, timeout_s: int = MANUAL_LOGIN_TIMEOUT_S) -> bool:
    log.info(
        "Headful manual login: complete authentication in the browser (up to %s seconds).",
        timeout_s,
    )
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if page_looks_logged_in(page):
            log.info("Manual login detected.")
            return True
        blockers = detect_blockers(page)
        if blockers:
            log.warning("Blockers during manual login: %s", ", ".join(blockers))
        time.sleep(2)
    log.error("Manual login timed out.")
    return False


def save_storage_state(context: Any, session_dir: Path) -> None:
    session_dir.mkdir(parents=True, exist_ok=True)
    path = storage_state_path(session_dir)
    context.storage_state(path=str(path))
    log.info("Saved browser session state to %s", path)


def load_storage_state_if_present(session_dir: Path) -> str | None:
    path = storage_state_path(session_dir)
    if path.exists():
        log.info("Reusing saved browser session state.")
        return str(path)
    return None


def attempt_credential_login(page: Any, config: RobinConfig) -> bool:
    if not config.has_credentials:
        return False

    assert config.username and config.password

    try:
        username_field = page.locator(
            'input[name="username"], input[id*="user" i], input[type="email"], input[type="text"]'
        ).first
        password_field = page.locator('input[type="password"]').first
        username_field.fill(config.username)
        password_field.fill(config.password)

        submit = page.locator(
            'button[type="submit"], input[type="submit"], button:has-text("Log In"), '
            'button:has-text("Sign In")'
        ).first
        submit.click()
        page.wait_for_load_state("domcontentloaded", timeout=30_000)
    except Exception as exc:  # noqa: BLE001
        log.warning("Credential login attempt failed: %s", type(exc).__name__)
        return False

    return page_looks_logged_in(page)


def navigate_to_photos_area(page: Any) -> bool:
    """Try to click a photos/gallery link on the current page."""
    selectors = "a, button, [role='button']"
    loc = page.locator(selectors)
    count = min(loc.count(), 200)
    for idx in range(count):
        item = loc.nth(idx)
        try:
            if not item.is_visible():
                continue
            text = (item.inner_text(timeout=500) or item.get_attribute("aria-label") or "").strip()
            href = item.get_attribute("href") or ""
            haystack = f"{text} {href}"
            if not any(pattern.search(haystack) for pattern in _PHOTO_LINK_PATTERNS):
                continue
            log.info("Navigating via link: %s", redact_text(text[:80]))
            item.click(timeout=10_000)
            page.wait_for_load_state("domcontentloaded", timeout=30_000)
            return True
        except Exception:
            continue
    return False


def discover_portal_photos(
    config: RobinConfig,
    *,
    headful: bool = False,
    since_date: date | None = None,
    limit: int | None = None,
) -> list[PhotoCandidate]:
    """
    Log into the daycare portal (if needed) and discover downloadable photo candidates.

    Read-only: does not post, share, or perform destructive portal actions.
    """
    if not config.has_portal_url:
        raise RobinPortalError("ROBIN_DAYCARE_PORTAL_URL is required for portal discovery.")
    if not _playwright_available():
        raise RobinPortalError(
            "Playwright is not installed. Install playwright and run "
            "'python -m playwright install chromium'."
        )

    assert config.portal_url is not None

    if "PLAYWRIGHT_HOST_PLATFORM_OVERRIDE" not in os.environ:
        os.environ["PLAYWRIGHT_HOST_PLATFORM_OVERRIDE"] = "ubuntu24.04-x64"

    from playwright.sync_api import sync_playwright

    use_headful = resolve_headful(headful_flag=headful, config=config)
    if headful and not use_headful and not has_display():
        log.warning("Headful mode requested but no DISPLAY found; continuing headless.")

    candidates: list[PhotoCandidate] = []

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=not use_headful)
        try:
            context_kwargs: dict[str, Any] = {}
            saved_state = None if headful else load_storage_state_if_present(config.session_dir)
            if saved_state:
                context_kwargs["storage_state"] = saved_state

            context = browser.new_context(**context_kwargs)
            page = context.new_page()
            page.set_default_timeout(45_000)

            log.info("Opening daycare portal.")
            page.goto(config.portal_url, wait_until="domcontentloaded")

            blockers = detect_blockers(page)
            if blockers:
                raise RobinPortalError(
                    f"Portal challenge detected ({', '.join(blockers)}). "
                    "Retry with --headful for manual login."
                )

            logged_in = page_looks_logged_in(page)
            if not logged_in:
                if config.has_credentials:
                    logged_in = attempt_credential_login(page, config)
                if not logged_in and headful:
                    if wait_for_manual_login(page):
                        save_storage_state(context, config.session_dir)
                        logged_in = True
                elif not logged_in and saved_state:
                    log.warning("Saved session appears expired; retry with --headful.")

            if not logged_in and page_has_login_form(page):
                raise RobinPortalError(
                    "Portal login required. Set ROBIN_DAYCARE_USERNAME/PASSWORD or "
                    "run with --headful to capture a session."
                )

            if not navigate_to_photos_area(page):
                log.info("No dedicated photos navigation link found; scanning current page.")

            page_candidates = discover_photo_candidates_on_page(page, since_date=since_date)
            candidates.extend(page_candidates)

            if headful and logged_in and not saved_state:
                save_storage_state(context, config.session_dir)

        finally:
            browser.close()

    if limit is not None and limit >= 0:
        candidates = candidates[:limit]

    log.info("Discovered %d photo candidate(s).", len(candidates))
    return candidates
