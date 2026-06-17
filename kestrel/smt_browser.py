"""Probe-quality browser CSV download for Smart Meter Texas (fallback path)."""

from __future__ import annotations

import logging
import re
from datetime import date
from pathlib import Path
from typing import Any

from kestrel.config import KestrelConfig
from kestrel.models import EnergyInterval
from kestrel.redact import redact_text
from kestrel.smart_meter_texas import (
    SMT_BASE_URL,
    SmartMeterTexasError,
    parse_csv_content,
)

log = logging.getLogger(__name__)

_LOGIN_URL = f"{SMT_BASE_URL}/login"
_MFA_PATTERNS = re.compile(
    r"(?i)(multi[- ]?factor|two[- ]?factor|verification code|authenticator|"
    r"one[- ]?time|otp|security code|enter the code)"
)
_CAPTCHA_PATTERNS = re.compile(r"(?i)(captcha|recaptcha|hcaptcha|i am not a robot)")


def _playwright_available() -> bool:
    try:
        import playwright.sync_api  # noqa: F401

        return True
    except ImportError:
        return False


def _detect_interactive_blocker(page_text: str) -> str | None:
    if _CAPTCHA_PATTERNS.search(page_text):
        return "CAPTCHA challenge detected"
    if _MFA_PATTERNS.search(page_text):
        return "multi-factor authentication required"
    return None


def _safe_browser_error_label(exc: Exception) -> str:
    text = redact_text(str(exc))
    if "ERR_HTTP2_PROTOCOL_ERROR" in text or "net::" in text:
        return "navigation error"
    name = type(exc).__name__
    if "Timeout" in name or "timeout" in text.lower():
        return "navigation timeout"
    return name


def fetch_intervals_via_browser(
    config: KestrelConfig,
    *,
    start: date,
    end: date,
    account_id: str | None = None,
    debug_dir: Path | None = None,
    debug_safe: bool = False,
) -> list[EnergyInterval]:
    """
    Log into the SMT portal with Playwright and download the official CSV export.

    Fails clearly when MFA/CAPTCHA or interactive steps are required.
    """
    if not config.has_smt_credentials:
        raise SmartMeterTexasError("Smart Meter Texas username/password are required for browser fetch.")
    if not _playwright_available():
        raise SmartMeterTexasError(
            "Playwright is not installed. Browser live refresh is unavailable. "
            "Use --import-csv or install playwright."
        )

    try:
        return _fetch_intervals_via_browser_impl(
            config,
            start=start,
            end=end,
            account_id=account_id,
            debug_dir=debug_dir,
            debug_safe=debug_safe,
        )
    except SmartMeterTexasError:
        raise
    except Exception as exc:  # noqa: BLE001
        label = _safe_browser_error_label(exc)
        if debug_safe:
            log.exception("Browser fallback error")
        else:
            log.warning("Browser fallback failed: %s", label)
        raise SmartMeterTexasError(f"Browser fallback failed: {label}") from None


def _fetch_intervals_via_browser_impl(
    config: KestrelConfig,
    *,
    start: date,
    end: date,
    account_id: str | None = None,
    debug_dir: Path | None = None,
    debug_safe: bool = False,
) -> list[EnergyInterval]:
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
    from playwright.sync_api import sync_playwright

    assert config.smt_username and config.smt_password
    if debug_dir is not None:
        debug_dir.mkdir(parents=True, exist_ok=True)

    start_str = start.strftime("%m/%d/%Y")
    end_str = end.strftime("%m/%d/%Y")
    page = None

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=config.headless)
            try:
                context = browser.new_context(accept_downloads=True)
                page = context.new_page()
                page.set_default_timeout(45_000)

                log.info("Opening Smart Meter Texas login page")
                page.goto(_LOGIN_URL, wait_until="domcontentloaded")

                body_text = page.inner_text("body")
                blocker = _detect_interactive_blocker(body_text)
                if blocker:
                    raise SmartMeterTexasError(
                        f"Smart Meter Texas browser login blocked: {blocker}. "
                        "Use manual CSV import instead."
                    )

                username_field = page.locator(
                    'input[name="username"], input[id*="user" i], input[type="text"]'
                ).first
                password_field = page.locator('input[type="password"]').first
                username_field.fill(config.smt_username)
                password_field.fill(config.smt_password)

                submit = page.locator(
                    'button[type="submit"], input[type="submit"], button:has-text("Log In"), '
                    'button:has-text("Sign In")'
                ).first
                submit.click()
                page.wait_for_load_state("networkidle", timeout=45_000)

                post_login_text = page.inner_text("body")
                blocker = _detect_interactive_blocker(post_login_text)
                if blocker:
                    if debug_dir is not None:
                        _save_debug_artifact(page, debug_dir, "post_login_blocker")
                    raise SmartMeterTexasError(
                        f"Smart Meter Texas browser login blocked: {blocker}. "
                        "Use manual CSV import instead."
                    )

                if "login" in page.url.lower() and "password" in post_login_text.lower():
                    raise SmartMeterTexasError(
                        "Smart Meter Texas browser login failed. Check credentials in .env."
                    )

                csv_text = _download_interval_csv(
                    page,
                    start_str=start_str,
                    end_str=end_str,
                    debug_dir=debug_dir,
                )
            finally:
                browser.close()
    except SmartMeterTexasError:
        raise
    except PlaywrightTimeoutError as exc:
        if debug_dir is not None and page is not None:
            _save_debug_artifact(page, debug_dir, "timeout")
        if debug_safe:
            log.exception("Browser fallback timeout")
        raise SmartMeterTexasError(
            "Browser fallback failed: navigation timeout"
        ) from None
    except PlaywrightError as exc:
        if debug_dir is not None and page is not None:
            _save_debug_artifact(page, debug_dir, "playwright_error")
        label = _safe_browser_error_label(exc)
        if debug_safe:
            log.exception("Browser fallback navigation error")
        raise SmartMeterTexasError(f"Browser fallback failed: {label}") from None

    intervals = parse_csv_content(
        csv_text,
        tz_name=config.timezone,
        account_id=account_id or config.smt_account_id,
        raw_source="smt_portal_browser_csv",
    )
    if not intervals:
        raise SmartMeterTexasError(
            "Smart Meter Texas browser export returned no intervals for the requested range."
        )
    log.info("Browser CSV export parsed %s intervals", len(intervals))
    return intervals


def _download_interval_csv(
    page: Any,
    *,
    start_str: str,
    end_str: str,
    debug_dir: Path | None,
) -> str:
    """Navigate to the 15-minute export UI and return CSV text."""
    energy_links = [
        'a:has-text("Energy Data")',
        'a:has-text("15 Min")',
        'a:has-text("Interval")',
        'text=Energy Data 15 Min Interval',
    ]
    opened = False
    for selector in energy_links:
        locator = page.locator(selector)
        if locator.count() > 0:
            locator.first.click()
            page.wait_for_load_state("networkidle", timeout=30_000)
            opened = True
            break

    if not opened:
        if debug_dir is not None:
            _save_debug_artifact(page, debug_dir, "energy_nav_missing")
        raise SmartMeterTexasError(
            "Could not locate Smart Meter Texas 15-minute energy export page in browser."
        )

    start_input = page.locator(
        'input[name*="start" i], input[id*="start" i], input[placeholder*="Start" i]'
    ).first
    end_input = page.locator(
        'input[name*="end" i], input[id*="end" i], input[placeholder*="End" i]'
    ).first
    start_input.fill(start_str)
    end_input.fill(end_str)

    export_button = page.locator(
        'button:has-text("Export"), a:has-text("Export"), input[value*="Export" i]'
    ).first
    with page.expect_download(timeout=60_000) as download_info:
        export_button.click()
    download = download_info.value
    download_path = download.path()
    csv_bytes = Path(download_path).read_bytes()
    csv_text = csv_bytes.decode("utf-8-sig", errors="replace")
    if not csv_text.strip():
        if debug_dir is not None:
            _save_debug_artifact(page, debug_dir, "empty_csv")
        raise SmartMeterTexasError("Smart Meter Texas browser export file was empty.")
    return csv_text


def _save_debug_artifact(page: Any, debug_dir: Path, label: str) -> None:
    safe_label = re.sub(r"[^a-z0-9_-]+", "_", label.lower()).strip("_") or "artifact"
    html_path = debug_dir / f"{safe_label}.html"
    try:
        html_path.write_text(redact_text(page.content()), encoding="utf-8")
        log.info("Saved redacted browser debug artifact: %s", safe_label)
    except OSError as exc:
        log.warning("Could not save browser debug artifact %s: %s", safe_label, exc)
