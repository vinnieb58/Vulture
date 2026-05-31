"""
Simply Fresh Kitchen feasibility probe
======================================
Reconnaissance only. Does NOT place orders, submit meal selections,
purchase anything, or permanently change the account.

Usage:
    python3 experiments/simplyfresh_probe/probe_simplyfresh.py --manual-login
    python3 experiments/simplyfresh_probe/probe_simplyfresh.py
    python3 experiments/simplyfresh_probe/probe_simplyfresh.py --headed
    python3 experiments/simplyfresh_probe/probe_simplyfresh.py --trace

Goal: prove whether Playwright/Chromium on Raven can load the site,
authenticate (manual session save or saved storage state), and navigate
to account / order / calendar / meal-selection areas without submitting.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

try:
    from playwright.sync_api import (
        Browser,
        BrowserContext,
        Page,
        TimeoutError as PlaywrightTimeout,
        sync_playwright,
    )
except ImportError:
    print("ERROR: playwright is not installed.")
    print(
        "Install with:\n"
        "  python3 -m venv .venv\n"
        "  .venv/bin/pip install -r experiments/simplyfresh_probe/requirements.txt\n"
        "  .venv/bin/playwright install chromium"
    )
    sys.exit(1)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)-8s %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("simplyfresh_probe")

# ---------------------------------------------------------------------------
# Paths and config
# ---------------------------------------------------------------------------

PROBE_DIR = Path(__file__).resolve().parent
ARTIFACTS_DIR = PROBE_DIR / "artifacts"
AUTH_DIR = PROBE_DIR / ".auth"
STORAGE_STATE_PATH = AUTH_DIR / "simplyfresh_storage_state.json"

START_URL = "https://new.thesimplyfreshkitchen.com/"

VIEWPORT = {"width": 1440, "height": 900}
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
LOCALE = "en-US"
TIMEZONE = "America/Chicago"

NAV_TIMEOUT_MS = 60_000
ACTION_DELAY_S = 1.0
MANUAL_LOGIN_TIMEOUT_S = 600

CHALLENGE_TITLE_FRAGMENTS = [
    "just a moment",
    "attention required",
    "access denied",
    "checking your browser",
    "ddos protection by cloudflare",
    "please wait",
    "security check",
    "verify you are human",
    "robot or human",
]

CHALLENGE_BODY_MARKERS = [
    "cf-browser-verification",
    "cdn-cgi/challenge-platform",
    "Enable JavaScript and cookies to continue",
    "Checking your browser before accessing",
    "cf-challenge-running",
]

VISIBLE_BLOCKER_PATTERNS = [
    (re.compile(r"two[\s-]?factor", re.I), "2fa"),
    (re.compile(r"\b2fa\b", re.I), "2fa"),
    (re.compile(r"multi[\s-]?factor", re.I), "2fa"),
    (re.compile(r"verify your identity", re.I), "2fa"),
    (re.compile(r"recaptcha", re.I), "captcha"),
    (re.compile(r"hcaptcha", re.I), "captcha"),
    (re.compile(r"verify you are human", re.I), "captcha"),
]

ALLOWED_LINK_PATTERNS = [
    re.compile(r"my\s*account", re.I),
    re.compile(r"account", re.I),
    re.compile(r"place\s*order", re.I),
    re.compile(r"order\s*now", re.I),
    re.compile(r"^next$", re.I),
    re.compile(r"^previous$", re.I),
    re.compile(r"^prev$", re.I),
    re.compile(r"calendar", re.I),
    re.compile(r"month", re.I),
    re.compile(r"student", re.I),
    re.compile(r"child", re.I),
    re.compile(r"select\s*student", re.I),
    re.compile(r"view\s*menu", re.I),
    re.compile(r"menu", re.I),
    re.compile(r"dashboard", re.I),
    re.compile(r"login", re.I),
    re.compile(r"log\s*in", re.I),
    re.compile(r"sign\s*in", re.I),
]

FORBIDDEN_LINK_PATTERNS = [
    re.compile(r"\bsubmit\b", re.I),
    re.compile(r"\bcheckout\b", re.I),
    re.compile(r"\bpay\b", re.I),
    re.compile(r"save\s*order", re.I),
    re.compile(r"\bconfirm\b", re.I),
    re.compile(r"complete\s*order", re.I),
    re.compile(r"\bpurchase\b", re.I),
    re.compile(r"place\s*order\s*confirm", re.I),
    re.compile(r"final\s*confirm", re.I),
]

LOGGED_IN_INDICATORS = [
    re.compile(r"log\s*out", re.I),
    re.compile(r"sign\s*out", re.I),
]

LOGGED_OUT_URL_TOKENS = (
    "/users/sign_in",
    "/users/login",
    "/login",
    "/sign_in",
    "/users/password",
)

LOGIN_FORM_MARKERS = [
    'input[type="password"]',
    'input[name*="password" i]',
    'input[id*="password" i]',
    'form[action*="login" i]',
    'button:has-text("Log In")',
    'button:has-text("Sign In")',
    'a:has-text("Log In")',
    'a:has-text("Sign In")',
]

ACCOUNT_LINK_PATTERNS = [
    re.compile(r"my\s*account", re.I),
    re.compile(r"\baccount\b", re.I),
    re.compile(r"dashboard", re.I),
]

ORDER_LINK_PATTERNS = [
    re.compile(r"place\s*order", re.I),
    re.compile(r"order\s*now", re.I),
    re.compile(r"order\s*meals", re.I),
    re.compile(r"meal\s*order", re.I),
]

CALENDAR_MARKERS = [
    re.compile(r"calendar", re.I),
    re.compile(r"\bmonth\b", re.I),
    re.compile(r"select\s*month", re.I),
    re.compile(r"date\s*picker", re.I),
    'input[type="date"]',
    '[class*="calendar" i]',
    '[id*="calendar" i]',
    'table[class*="calendar" i]',
]

MEAL_OPTION_MARKERS = [
    re.compile(r"meal", re.I),
    re.compile(r"entr[eé]e", re.I),
    re.compile(r"sandwich", re.I),
    re.compile(r"salad", re.I),
    re.compile(r"menu\s*item", re.I),
    re.compile(r"select\s*meal", re.I),
    re.compile(r"daily\s*menu", re.I),
    'input[type="radio"]',
    '[class*="meal" i]',
    '[data-meal]',
]

STUDENT_SELECTOR_MARKERS = [
    re.compile(r"student", re.I),
    re.compile(r"child", re.I),
    re.compile(r"select\s*child", re.I),
    'select[name*="student" i]',
    'select[name*="child" i]',
]

SUBMIT_CONTROL_MARKERS = [
    re.compile(r"\bsubmit\b", re.I),
    re.compile(r"\bcheckout\b", re.I),
    re.compile(r"\bpay\b", re.I),
    re.compile(r"save\s*order", re.I),
    re.compile(r"\bconfirm\b", re.I),
    re.compile(r"complete\s*order", re.I),
    re.compile(r"\bpurchase\b", re.I),
    'button[type="submit"]',
    'input[type="submit"]',
]


@dataclass
class FeasibilityReport:
    site_loaded: bool = False
    login_required: bool = False
    login_successful: str = "unknown"
    account_page_accessible: bool = False
    order_page_accessible: bool = False
    calendar_detected: bool = False
    meal_options_detected: bool = False
    submit_controls_detected: bool = False
    blockers_detected: list[str] = field(default_factory=list)
    recommended_next_step: str = ""
    notes: list[str] = field(default_factory=list)


class ProbeArtifacts:
    def __init__(self, run_id: str) -> None:
        self.run_dir = ARTIFACTS_DIR / run_id
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self._step = 0

    def capture(self, page: Page, label: str) -> None:
        self._step += 1
        safe = re.sub(r"[^a-zA-Z0-9_-]+", "_", label).strip("_").lower()
        prefix = f"{self._step:02d}_{safe}"
        png_path = self.run_dir / f"{prefix}.png"
        html_path = self.run_dir / f"{prefix}.html"
        try:
            page.screenshot(path=str(png_path), full_page=True)
            log.info("Saved screenshot: %s", png_path.relative_to(PROBE_DIR))
        except Exception as exc:
            log.warning("Screenshot failed for %s: %s", label, exc)
        try:
            html_path.write_text(page.content(), encoding="utf-8")
            log.info("Saved HTML snapshot: %s", html_path.relative_to(PROBE_DIR))
        except Exception as exc:
            log.warning("HTML snapshot failed for %s: %s", label, exc)


def human_pause(seconds: float = ACTION_DELAY_S) -> None:
    time.sleep(seconds)


def has_display() -> bool:
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def resolve_headed_flag(explicit_headed: Optional[bool], manual_login: bool) -> bool:
    if explicit_headed is not None:
        return explicit_headed
    if manual_login:
        return has_display()
    return False


def detect_blockers(page: Page) -> list[str]:
    blockers: list[str] = []
    title = (page.title() or "").lower()
    body = page.content().lower()
    visible = _visible_text(page)

    if any(fragment in title for fragment in CHALLENGE_TITLE_FRAGMENTS):
        blockers.append("cloudflare")

    if any(marker in body for marker in CHALLENGE_BODY_MARKERS):
        blockers.append("cloudflare")

    for pattern, label in VISIBLE_BLOCKER_PATTERNS:
        if pattern.search(visible) and label not in blockers:
            blockers.append(label)

    if not blockers and ("access denied" in title or "403" in title):
        blockers.append("unknown")

    return blockers


def page_has_login_form(page: Page) -> bool:
    for selector in LOGIN_FORM_MARKERS:
        try:
            if page.locator(selector).count() > 0:
                return True
        except Exception:
            continue
    return False


def page_looks_logged_in(page: Page) -> bool:
    if page_has_login_form(page):
        return False

    url = page.url.lower()
    if any(token in url for token in LOGGED_OUT_URL_TOKENS):
        return False

    text = _visible_text(page)
    if any(pattern.search(text) for pattern in LOGGED_IN_INDICATORS):
        return True

    if any(token in url for token in ("/dashboard", "/portal", "/orders")):
        return True

    return False


def _visible_text(page: Page) -> str:
    try:
        return page.inner_text("body")
    except Exception:
        return page.content()


def classify_action(label: str) -> str:
    normalized = " ".join(label.split())
    for pattern in FORBIDDEN_LINK_PATTERNS:
        if pattern.search(normalized):
            return "forbidden"
    for pattern in ALLOWED_LINK_PATTERNS:
        if pattern.search(normalized):
            return "allowed"
    return "uncertain"


def find_clickable_candidates(page: Page) -> list[dict[str, str]]:
    candidates: list[dict[str, str]] = []
    selectors = "a, button, [role='button'], input[type='button'], input[type='submit']"
    loc = page.locator(selectors)
    count = min(loc.count(), 200)
    for idx in range(count):
        item = loc.nth(idx)
        try:
            if not item.is_visible():
                continue
            text = (item.inner_text(timeout=500) or item.get_attribute("value") or "").strip()
            if not text:
                text = (item.get_attribute("aria-label") or item.get_attribute("title") or "").strip()
            if not text:
                continue
            href = item.get_attribute("href") or ""
            candidates.append({"text": text, "href": href})
        except Exception:
            continue
    return candidates


def click_safe_link(page: Page, patterns: list[re.Pattern[str]], artifacts: ProbeArtifacts, purpose: str) -> bool:
    for candidate in find_clickable_candidates(page):
        text = candidate["text"]
        href = candidate["href"]
        haystack = f"{text} {href}"
        if not any(pattern.search(haystack) for pattern in patterns):
            continue

        action = classify_action(text)
        if action == "forbidden":
            log.info("FORBIDDEN_ACTION_SKIPPED: %s (%s)", text, purpose)
            continue
        if action == "uncertain":
            log.warning("UNCERTAIN_ACTION_SKIPPED: %s (%s)", text, purpose)
            artifacts.capture(page, f"uncertain_{purpose}")
            continue

        log.info("Clicking allowed link: %s (%s)", text, purpose)
        try:
            page.get_by_role("link", name=re.compile(re.escape(text), re.I)).first.click(timeout=5000)
        except Exception:
            try:
                page.get_by_role("button", name=re.compile(re.escape(text), re.I)).first.click(timeout=5000)
            except Exception as exc:
                log.warning("Could not click %s: %s", text, exc)
                continue

        human_pause()
        artifacts.capture(page, purpose.replace(" ", "_"))
        return True
    return False


def detect_patterns_in_page(page: Page, patterns: list[Any]) -> bool:
    text = _visible_text(page)
    for pattern in patterns:
        if isinstance(pattern, re.Pattern):
            if pattern.search(text):
                return True
        else:
            try:
                if page.locator(pattern).count() > 0:
                    return True
            except Exception:
                continue
    return False


def detect_submit_controls(page: Page) -> bool:
    for candidate in find_clickable_candidates(page):
        if classify_action(candidate["text"]) == "forbidden":
            return True
    return detect_patterns_in_page(page, SUBMIT_CONTROL_MARKERS)


def wait_for_manual_login(page: Page, artifacts: ProbeArtifacts, timeout_s: int) -> bool:
    log.info(
        "Manual login mode: complete login in the browser. Waiting up to %s seconds...",
        timeout_s,
    )
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        human_pause(2.0)
        if page_looks_logged_in(page):
            log.info("Logged-in state detected.")
            artifacts.capture(page, "manual_login_success")
            return True
        blockers = detect_blockers(page)
        if blockers:
            log.warning("Blockers detected during manual login: %s", ", ".join(blockers))
    log.error("Manual login timed out without detecting logged-in state.")
    artifacts.capture(page, "manual_login_timeout")
    return False


def save_storage_state(context: BrowserContext) -> None:
    AUTH_DIR.mkdir(parents=True, exist_ok=True)
    context.storage_state(path=str(STORAGE_STATE_PATH))
    log.info("Saved storage state: %s", STORAGE_STATE_PATH.relative_to(PROBE_DIR))


def load_storage_state_if_present() -> Optional[str]:
    if STORAGE_STATE_PATH.exists():
        log.info("Reusing storage state: %s", STORAGE_STATE_PATH.relative_to(PROBE_DIR))
        return str(STORAGE_STATE_PATH)
    log.info("No saved storage state found.")
    return None


def links_present(page: Page, patterns: list[re.Pattern[str]]) -> bool:
    for candidate in find_clickable_candidates(page):
        haystack = f"{candidate['text']} {candidate['href']}"
        if any(pattern.search(haystack) for pattern in patterns):
            return True
    return False


def navigate_probe(page: Page, artifacts: ProbeArtifacts, report: FeasibilityReport) -> None:
    report.account_page_accessible = links_present(page, ACCOUNT_LINK_PATTERNS)
    report.order_page_accessible = links_present(page, ORDER_LINK_PATTERNS)

    log.info("Step: probing account navigation")
    if click_safe_link(page, ACCOUNT_LINK_PATTERNS, artifacts, "account navigation"):
        report.account_page_accessible = True

    log.info("Step: probing order navigation")
    if click_safe_link(page, ORDER_LINK_PATTERNS, artifacts, "order navigation"):
        report.order_page_accessible = True
    report.order_page_accessible = report.order_page_accessible or links_present(
        page, ORDER_LINK_PATTERNS
    )

    log.info("Step: detecting calendar/month selectors")
    report.calendar_detected = detect_patterns_in_page(page, CALENDAR_MARKERS)
    if report.calendar_detected:
        artifacts.capture(page, "calendar_detected")

    log.info("Step: detecting student/child selectors")
    if detect_patterns_in_page(page, STUDENT_SELECTOR_MARKERS):
        report.notes.append("student_selector_detected")

    log.info("Step: detecting meal options")
    report.meal_options_detected = detect_patterns_in_page(page, MEAL_OPTION_MARKERS)
    if report.meal_options_detected:
        artifacts.capture(page, "meal_options_detected")

    log.info("Step: detecting submit/checkout controls (observation only)")
    report.submit_controls_detected = detect_submit_controls(page)
    if report.submit_controls_detected:
        artifacts.capture(page, "submit_controls_detected")


def build_recommendation(report: FeasibilityReport) -> str:
    if report.blockers_detected:
        primary = report.blockers_detected[0]
        if primary == "cloudflare":
            return "Resolve Cloudflare/browser challenge manually on Raven, then retry headed manual login."
        if primary == "captcha":
            return "Captcha present; manual headed login with human verification is required."
        if primary == "2fa":
            return "2FA detected; keep manual session bootstrap and avoid credential automation for now."
        return "Investigate blockers manually before attempting navigation automation."

    if not report.site_loaded:
        return "Fix site load/connectivity on Raven, then rerun probe."

    if report.login_required and report.login_successful != "true":
        return "Run --manual-login on a headed/VNC session to seed storage state, then rerun normally."

    if not report.order_page_accessible:
        return "Inspect saved HTML/screenshots to map Place Order URL/selectors, then extend probe."

    if not report.calendar_detected or not report.meal_options_detected:
        return "Reach order calendar in manual session, capture DOM selectors, and extend probe mappings."

    return "Feasibility looks promising; next step is read-only DOM mapping and selector hardening."


def print_report(report: FeasibilityReport) -> None:
    blockers = report.blockers_detected or ["none"]
    print("\n" + "=" * 60)
    print("SIMPLY FRESH KITCHEN FEASIBILITY REPORT")
    print("=" * 60)
    print(f"site_loaded: {str(report.site_loaded).lower()}")
    print(f"login_required: {str(report.login_required).lower()}")
    print(f"login_successful: {report.login_successful}")
    print(f"account_page_accessible: {str(report.account_page_accessible).lower()}")
    print(f"order_page_accessible: {str(report.order_page_accessible).lower()}")
    print(f"calendar_detected: {str(report.calendar_detected).lower()}")
    print(f"meal_options_detected: {str(report.meal_options_detected).lower()}")
    print(f"submit_controls_detected: {str(report.submit_controls_detected).lower()}")
    print(f"blockers_detected: {','.join(blockers)}")
    print(f"recommended_next_step: {report.recommended_next_step}")
    if report.notes:
        print("notes:")
        for note in report.notes:
            print(f"  - {note}")
    print("=" * 60 + "\n")


def run_probe(
    manual_login: bool = False,
    headed: Optional[bool] = None,
    trace: bool = False,
) -> FeasibilityReport:
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    artifacts = ProbeArtifacts(run_id)
    report = FeasibilityReport()

    use_headed = resolve_headed_flag(headed, manual_login)
    log.info("Run ID: %s", run_id)
    log.info("Headed mode: %s (display available: %s)", use_headed, has_display())

    with sync_playwright() as playwright:
        browser: Browser = playwright.chromium.launch(headless=not use_headed)
        context_kwargs: dict[str, Any] = {
            "viewport": VIEWPORT,
            "user_agent": USER_AGENT,
            "locale": LOCALE,
            "timezone_id": TIMEZONE,
        }
        storage_state = None if manual_login else load_storage_state_if_present()
        if storage_state:
            context_kwargs["storage_state"] = storage_state

        context: BrowserContext = browser.new_context(**context_kwargs)
        if trace:
            trace_path = artifacts.run_dir / "trace.zip"
            context.tracing.start(screenshots=True, snapshots=True, sources=True)
            log.info("Trace recording enabled: %s", trace_path.relative_to(PROBE_DIR))

        page: Page = context.new_page()
        page.set_default_timeout(NAV_TIMEOUT_MS)

        try:
            log.info("Step: loading homepage %s", START_URL)
            response = page.goto(START_URL, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
            human_pause()
            artifacts.capture(page, "homepage")
            report.site_loaded = response is not None and response.ok
            if not report.site_loaded:
                report.notes.append(f"homepage_status={getattr(response, 'status', 'none')}")

            blockers = detect_blockers(page)
            if blockers:
                report.blockers_detected.extend(blockers)
                log.warning("Blockers detected on homepage: %s", ", ".join(blockers))

            report.login_required = (
                page_has_login_form(page)
                or links_present(page, [re.compile(r"log\s*in", re.I), re.compile(r"sign\s*in", re.I)])
                or not page_looks_logged_in(page)
            )
            log.info("Login required: %s", report.login_required)

            if manual_login:
                if wait_for_manual_login(page, artifacts, MANUAL_LOGIN_TIMEOUT_S):
                    save_storage_state(context)
                    report.login_successful = "true"
                    report.login_required = False
                else:
                    report.login_successful = "false"
            elif storage_state:
                if page_looks_logged_in(page):
                    report.login_successful = "true"
                    report.login_required = False
                elif page_has_login_form(page):
                    report.login_successful = "false"
                    report.notes.append("saved_session_expired_or_invalid")
                else:
                    report.login_successful = "unknown"

            navigate_probe(page, artifacts, report)

            if report.login_successful != "true" and report.login_required:
                log.info("Step: listing navigation links without authenticated session")
                candidates = find_clickable_candidates(page)
                for candidate in candidates[:20]:
                    action = classify_action(candidate["text"])
                    log.info(
                        "Nav candidate [%s]: text=%r href=%r",
                        action,
                        candidate["text"],
                        candidate["href"],
                    )
                if any(
                    any(p.search(f"{c['text']} {c['href']}") for p in ACCOUNT_LINK_PATTERNS)
                    for c in candidates
                ):
                    report.account_page_accessible = True
                if any(
                    any(p.search(f"{c['text']} {c['href']}") for p in ORDER_LINK_PATTERNS)
                    for c in candidates
                ):
                    report.order_page_accessible = True

            if detect_blockers(page):
                for blocker in detect_blockers(page):
                    if blocker not in report.blockers_detected:
                        report.blockers_detected.append(blocker)

        except PlaywrightTimeout as exc:
            log.error("Timeout during probe: %s", exc)
            report.notes.append(f"timeout: {exc}")
            artifacts.capture(page, "timeout")
        except Exception as exc:
            log.error("Probe failed: %s", exc)
            report.notes.append(f"error: {exc}")
            try:
                artifacts.capture(page, "error")
            except Exception:
                pass
        finally:
            if trace:
                trace_path = artifacts.run_dir / "trace.zip"
                context.tracing.stop(path=str(trace_path))
                log.info("Saved trace: %s", trace_path.relative_to(PROBE_DIR))
            context.close()
            browser.close()

    report.recommended_next_step = build_recommendation(report)
    print_report(report)
    return report


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Simply Fresh Kitchen Playwright feasibility probe (no orders submitted)",
    )
    parser.add_argument(
        "--manual-login",
        action="store_true",
        help="Open browser for manual login and save storage state on success",
    )
    parser.add_argument(
        "--headed",
        action="store_true",
        help="Force headed browser mode (requires DISPLAY or xvfb-run)",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Force headless browser mode",
    )
    parser.add_argument(
        "--trace",
        action="store_true",
        help="Record Playwright trace zip under artifacts/",
    )
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    headed: Optional[bool] = None
    if args.headed:
        headed = True
    elif args.headless:
        headed = False

    ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
    report = run_probe(
        manual_login=args.manual_login,
        headed=headed,
        trace=args.trace,
    )
    return 0 if report.site_loaded else 1


if __name__ == "__main__":
    sys.exit(main())
