"""Shared helpers for Simply Fresh Kitchen probe scripts."""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

try:
    from playwright.sync_api import Page, TimeoutError as PlaywrightTimeout
except ImportError:
    Page = Any  # type: ignore[misc, assignment]
    PlaywrightTimeout = Exception  # type: ignore[misc, assignment]

PROBE_DIR = Path(__file__).resolve().parent
ARTIFACTS_DIR = PROBE_DIR / "artifacts"
AUTH_DIR = PROBE_DIR / ".auth"
STORAGE_STATE_PATH = AUTH_DIR / "simplyfresh_storage_state.json"

START_URL = "https://new.thesimplyfreshkitchen.com/"
PROFILE_URL = f"{START_URL.rstrip('/')}/profile"

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
MEAL_CALENDAR_WAIT_MS = 15_000
CHOOSER_OVERLAY_ACTIVE_SELECTOR = ".Chooser__options--active"
MODAL_ROOT_SELECTOR = ".Modal"
MODAL_CONTENT_SELECTOR = ".Modal__content"

SAFE_MODAL_CLOSE_PATTERNS = [
    re.compile(r"^x$", re.I),
    re.compile(r"^×$"),
    re.compile(r"\bclose\b", re.I),
    re.compile(r"^back$", re.I),
    re.compile(r"\bcancel\b", re.I),
    re.compile(r"\bdismiss\b", re.I),
]

MODAL_ADD_BUTTON_PATTERN = re.compile(r"^add$", re.I)

REGULAR_SIZE_PATTERNS = [
    re.compile(r"^regular$", re.I),
]

ORDER_NOW_PATTERNS = [
    re.compile(r"^order\s*now$", re.I),
    re.compile(r"\border\s*now\b", re.I),
]

PLACE_ORDER_FALLBACK_PATTERNS = [
    re.compile(r"place\s*order", re.I),
]

PROFILE_CHOOSER_HEADING = re.compile(r"who\s+are\s+you\s+ordering\s+for", re.I)
MEAL_CALENDAR_HEADING = re.compile(r"choose\s+your\s+meals", re.I)
SELECT_PROFILE_PATTERN = re.compile(r"select\s*profile", re.I)
CREATE_PROFILE_PATTERN = re.compile(r"create\s*a\s*new\s*profile", re.I)
NEXT_BUTTON_PATTERN = re.compile(r"^next$", re.I)

DATE_ROW_PATTERN = re.compile(
    r"\b(January|February|March|April|May|June|July|August|"
    r"September|October|November|December)\s+\d{1,2},\s+\d{4}\b",
    re.I,
)

FORBIDDEN_CONTROL_PATTERNS = [
    re.compile(r"\bsubmit\b", re.I),
    re.compile(r"save\s*order", re.I),
    re.compile(r"\bcheckout\b", re.I),
    re.compile(r"\bpay\b", re.I),
    re.compile(r"\bconfirm\b", re.I),
    re.compile(r"\bcomplete\b", re.I),
    re.compile(r"\bpurchase\b", re.I),
    re.compile(r"\bfinalize\b", re.I),
    re.compile(r"place\s*order\s*confirm", re.I),
    re.compile(r"cart\s*checkout", re.I),
    re.compile(r"continue\s*to\s*payment", re.I),
    re.compile(r"final\s*confirm", re.I),
]

AUTOSAVE_MARKERS = [
    re.compile(r"\bsaved\b", re.I),
    re.compile(r"order\s*saved", re.I),
    re.compile(r"selection\s*saved", re.I),
    re.compile(r"successfully\s*updated", re.I),
]

MONTH_NAME_PATTERN = re.compile(
    r"\b(January|February|March|April|May|June|July|August|"
    r"September|October|November|December)\s+(\d{4})\b",
    re.I,
)


@dataclass(frozen=True)
class ProfileConfig:
    profile_name: Optional[str] = None
    school: Optional[str] = None


@dataclass
class NavigationResult:
    ok: bool = False
    profile_chooser_seen: bool = False
    profile_selected: bool = False
    meal_calendar_reached: bool = False
    login_required: bool = False
    profile_overlay_close_strategy: str | None = None


@dataclass
class OverlayCloseResult:
    was_open: bool = False
    closed: bool = False
    strategy: str | None = None
    active_count_before: int = 0
    active_count_after: int = 0


@dataclass
class ModalAddResult:
    add_clicked: bool = False
    add_failed_reason: str | None = None
    regular_size_selected: bool = False
    modal_closed: bool = False


def setup_logging(name: str) -> logging.Logger:
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)-8s %(message)s",
        stream=sys.stdout,
        force=True,
    )
    return logging.getLogger(name)


def human_pause(seconds: float = ACTION_DELAY_S) -> None:
    time.sleep(seconds)


def has_display() -> bool:
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def load_storage_state_path(log: logging.Logger | None = None) -> Optional[str]:
    """Return absolute path to saved storage state, or None if missing."""
    path = resolve_auth_storage_path()
    if path.is_file():
        msg = f"Reusing storage state: {path}"
        if log is not None:
            log.info(msg)
        else:
            logging.getLogger("simplyfresh_probe").info(msg)
        return str(path)
    return None


def resolve_auth_storage_path() -> Path:
    """
    Absolute path to experiments/simplyfresh_probe/.auth/simplyfresh_storage_state.json.

    Resolved from this module's location (not process cwd), so it works when invoked
    from repo root or the probe directory. Override with SIMPLYFRESH_STORAGE_STATE_PATH.
    """
    override = os.getenv("SIMPLYFRESH_STORAGE_STATE_PATH", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    return STORAGE_STATE_PATH.resolve()


def new_run_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def safe_filename(label: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]+", "_", label).strip("_").lower()


def get_visible_text(page: Page) -> str:
    try:
        return page.inner_text("body")
    except Exception:
        return page.content()


def capture_named(page: Page, run_dir: Path, base_name: str, probe_dir: Path, log: logging.Logger) -> None:
    png_path = run_dir / f"{base_name}.png"
    html_path = run_dir / f"{base_name}.html"
    try:
        page.screenshot(path=str(png_path), full_page=True)
        log.info("Saved screenshot: %s", png_path.relative_to(probe_dir))
    except Exception as exc:
        log.warning("Screenshot failed (%s): %s", base_name, exc)
    try:
        html_path.write_text(page.content(), encoding="utf-8")
        log.info("Saved HTML snapshot: %s", html_path.relative_to(probe_dir))
    except Exception as exc:
        log.warning("HTML snapshot failed (%s): %s", base_name, exc)


def collect_buttons_links_summary(page: Page) -> list[dict[str, Any]]:
    """Collect visible buttons/links with disabled state for debug artifacts."""
    raw: list[dict[str, Any]] = page.evaluate(
        """() => {
        const out = [];
        const seen = new Set();
        const nodes = document.querySelectorAll(
          'a, button, [role="button"], input[type="button"], input[type="submit"]'
        );
        for (const el of nodes) {
          if (!el.offsetParent && el.tagName !== 'BODY') continue;
          const style = window.getComputedStyle(el);
          if (style.visibility === 'hidden' || style.display === 'none') continue;
          let text = (el.innerText || el.textContent || el.value || '').trim().replace(/\\s+/g, ' ');
          if (!text) text = (el.getAttribute('aria-label') || el.getAttribute('title') || '').trim();
          if (!text) continue;
          const key = text + '|' + (el.getAttribute('href') || '');
          if (seen.has(key)) continue;
          seen.add(key);
          const disabled = el.disabled === true ||
            el.getAttribute('aria-disabled') === 'true' ||
            el.hasAttribute('disabled');
          out.push({
            text: text.slice(0, 200),
            tag: el.tagName.toLowerCase(),
            href: el.getAttribute('href') || null,
            disabled,
            role: el.getAttribute('role'),
          });
          if (out.length >= 200) break;
        }
        return out;
    }"""
    )
    return raw


def save_step_debug(
    page: Page,
    run_dir: Path,
    step_prefix: str,
    probe_dir: Path,
    log: logging.Logger,
) -> None:
    """PNG/HTML plus visible_text, buttons summary, and current URL."""
    capture_named(page, run_dir, step_prefix, probe_dir, log)
    visible = get_visible_text(page)
    (run_dir / "visible_text.txt").write_text(visible[:100_000], encoding="utf-8")
    (run_dir / f"{step_prefix}_visible_text.txt").write_text(visible[:100_000], encoding="utf-8")
    (run_dir / "current_url.txt").write_text(page.url, encoding="utf-8")
    (run_dir / f"{step_prefix}_current_url.txt").write_text(page.url, encoding="utf-8")
    summary = collect_buttons_links_summary(page)
    (run_dir / "buttons_links_summary.json").write_text(
        json.dumps(summary, indent=2),
        encoding="utf-8",
    )
    (run_dir / f"{step_prefix}_buttons_links_summary.json").write_text(
        json.dumps(summary, indent=2),
        encoding="utf-8",
    )
    log.info("Saved debug artifacts for step %s (url=%s)", step_prefix, page.url)


def is_forbidden_control_text(text: str) -> bool:
    normalized = " ".join(text.split())
    return any(pattern.search(normalized) for pattern in FORBIDDEN_CONTROL_PATTERNS)


def is_never_click_text(text: str) -> bool:
    normalized = " ".join(text.split())
    if CREATE_PROFILE_PATTERN.search(normalized):
        return True
    return is_forbidden_control_text(normalized)


def is_safe_modal_close_text(text: str) -> bool:
    """True for non-submit dismiss controls: X, close, back, cancel."""
    normalized = " ".join(text.split()).strip()
    if not normalized:
        return False
    if is_forbidden_control_text(normalized):
        return False
    return any(pattern.search(normalized) for pattern in SAFE_MODAL_CLOSE_PATTERNS)


def is_safe_modal_close_aria(label: str) -> bool:
    normalized = " ".join(label.split()).strip()
    if not normalized:
        return False
    if is_forbidden_control_text(normalized):
        return False
    return bool(re.search(r"\b(close|dismiss|back|cancel)\b", normalized, re.I))


def is_modal_add_button_text(text: str) -> bool:
    """True for meal-item modal Add control only (not checkout/submit)."""
    normalized = " ".join(text.split()).strip()
    if not normalized or is_forbidden_control_text(normalized):
        return False
    return bool(MODAL_ADD_BUTTON_PATTERN.match(normalized))


def find_forbidden_controls(page: Page) -> list[dict[str, str]]:
    found: list[dict[str, str]] = []
    selectors = "a, button, [role='button'], input[type='submit'], input[type='button']"
    loc = page.locator(selectors)
    count = min(loc.count(), 300)
    for idx in range(count):
        item = loc.nth(idx)
        try:
            if not item.is_visible():
                continue
            text = (item.inner_text(timeout=300) or item.get_attribute("value") or "").strip()
            if not text:
                text = (item.get_attribute("aria-label") or "").strip()
            if not text or not is_forbidden_control_text(text):
                continue
            found.append(
                {
                    "text": text,
                    "tag": item.evaluate("el => el.tagName.toLowerCase()"),
                }
            )
        except Exception:
            continue
    return found


def detect_autosave_markers(page: Page, previous_html: str) -> bool:
    current = page.content()
    visible = get_visible_text(page)

    for pattern in AUTOSAVE_MARKERS:
        if pattern.search(visible) and not pattern.search(previous_html):
            return True
    return False


def detect_month_label(page: Page) -> Optional[str]:
    text = get_visible_text(page)
    date_match = DATE_ROW_PATTERN.search(text)
    if date_match:
        return date_match.group(0)
    match = MONTH_NAME_PATTERN.search(text)
    if match:
        return f"{match.group(1)} {match.group(2)}"
    return None


def is_profile_chooser_page(page: Page) -> bool:
    text = get_visible_text(page)
    return bool(PROFILE_CHOOSER_HEADING.search(text))


def is_meal_calendar_page(page: Page) -> bool:
    text = get_visible_text(page)
    if MEAL_CALENDAR_HEADING.search(text):
        return True
    if DATE_ROW_PATTERN.search(text) and discover_meal_card_labels(page):
        return True
    return False


def wait_for_meal_calendar(page: Page, log: logging.Logger, timeout_ms: int = MEAL_CALENDAR_WAIT_MS) -> bool:
    deadline = time.time() + (timeout_ms / 1000.0)
    while time.time() < deadline:
        if is_meal_calendar_page(page):
            log.info("Meal calendar detected (Choose your meals / meal cards)")
            return True
        human_pause(0.5)
    log.error("Timed out waiting for meal calendar")
    return False


def _control_matches(text: str, patterns: list[re.Pattern[str]]) -> bool:
    normalized = " ".join(text.split())
    return any(p.search(normalized) for p in patterns)


def _is_control_disabled(item: Any) -> bool:
    try:
        if not item.is_visible():
            return True
        disabled = item.get_attribute("disabled")
        if disabled is not None:
            return True
        aria = item.get_attribute("aria-disabled")
        if aria and aria.lower() == "true":
            return True
        return bool(item.evaluate("el => el.disabled === true"))
    except Exception:
        return False


def click_visible_control(
    page: Page,
    patterns: list[re.Pattern[str]],
    log: logging.Logger,
    *,
    purpose: str,
) -> bool:
    """Click first visible enabled control matching patterns; skip forbidden/create-profile."""
    selectors = "a, button, [role='button'], input[type='button']"
    loc = page.locator(selectors)
    count = min(loc.count(), 200)

    candidates: list[tuple[int, str, int]] = []
    for idx in range(count):
        item = loc.nth(idx)
        try:
            if not item.is_visible():
                continue
            text = (item.inner_text(timeout=300) or item.get_attribute("value") or "").strip()
            if not text:
                text = (item.get_attribute("aria-label") or "").strip()
            if not text or not _control_matches(text, patterns):
                continue
            if is_never_click_text(text):
                log.info("NEVER_CLICK_SKIPPED: %r (%s)", text, purpose)
                continue
            if NEXT_BUTTON_PATTERN.match(text.strip()) and _is_control_disabled(item):
                log.info("DISABLED_NEXT_SKIPPED (%s)", purpose)
                continue
            priority = 0 if re.match(r"^order\s*now$", text.strip(), re.I) else 1
            candidates.append((priority, text, idx))
        except Exception:
            continue

    candidates.sort(key=lambda c: (c[0], len(c[1])))
    for _priority, text, idx in candidates:
        item = loc.nth(idx)
        if _is_control_disabled(item):
            log.info("DISABLED_CONTROL_SKIPPED: %r (%s)", text, purpose)
            continue
        log.info("Clicking %r (%s)", text, purpose)
        try:
            item.click(timeout=5000)
            human_pause()
            return True
        except Exception as exc:
            log.warning("Could not click %r: %s", text, exc)
    return False


def click_order_now(page: Page, log: logging.Logger) -> bool:
    """Prefer green Order Now; fall back to nav Place Order only if Order Now absent."""
    if click_visible_control(page, ORDER_NOW_PATTERNS, log, purpose="order_now"):
        return True
    log.info("Order Now not found; trying Place Order fallback")
    return click_visible_control(page, PLACE_ORDER_FALLBACK_PATTERNS, log, purpose="place_order_fallback")


def discover_profile_cards(page: Page) -> list[dict[str, str]]:
    """Return raw profile card texts that include Select profile (exclude create-new)."""
    raw: list[dict[str, str]] = page.evaluate(
        """() => {
        const cards = [];
        const blocks = document.querySelectorAll('div, section, article, li, [class*="card" i]');
        for (const el of blocks) {
          const text = (el.innerText || '').trim().replace(/\\s+/g, ' ');
          if (!text || text.length > 800 || text.length < 20) continue;
          if (!/select profile/i.test(text)) continue;
          if (/create a new profile/i.test(text) && !/classroom:/i.test(text)) continue;
          if (!/classroom:|school:/i.test(text)) continue;
          cards.push({ text });
        }
        return cards.slice(0, 20);
    }"""
    )
    filtered: list[dict[str, str]] = []
    for card in raw:
        text = card.get("text", "")
        if CREATE_PROFILE_PATTERN.search(text) and "Classroom:" not in text:
            continue
        filtered.append({"text": text})
    return filtered


def normalize_profile_card_text(text: str) -> str:
    """Normalize profile card text for deduplication (whitespace + heading noise)."""
    lines: list[str] = []
    for raw_line in re.split(r"[\r\n]+", text):
        line = " ".join(raw_line.split()).strip()
        if not line:
            continue
        if PROFILE_CHOOSER_HEADING.search(line):
            continue
        if SELECT_PROFILE_PATTERN.fullmatch(line):
            continue
        lines.append(line.lower())
    return "|".join(lines)


def profile_card_signature(text: str) -> str:
    """Stable identity key for a logical profile (name, classroom, school)."""
    return normalize_profile_card_text(text)


def deduplicate_profile_cards(cards: list[dict[str, str]]) -> list[dict[str, str]]:
    """
    Collapse nested duplicate containers into one logical profile per signature.

    When parent and child nodes share the same profile content, keep the shorter
    text (inner container that directly owns Select profile).
    """
    by_signature: dict[str, dict[str, str]] = {}
    for card in cards:
        text = card.get("text", "")
        signature = profile_card_signature(text)
        if not signature:
            continue
        existing = by_signature.get(signature)
        if existing is None or len(text) < len(existing["text"]):
            by_signature[signature] = {"text": text, "signature": signature}
    return list(by_signature.values())


def _profile_card_matches(card_text: str, config: ProfileConfig) -> bool:
    hay = card_text.lower()
    if config.profile_name and config.profile_name.lower() not in hay:
        return False
    if config.school and config.school.lower() not in hay:
        return False
    return True


def _click_select_profile_for_card(
    page: Page,
    card: dict[str, str],
    config: ProfileConfig,
    log: logging.Logger,
) -> bool:
    """Click Select profile on the innermost container matching the deduplicated card."""
    anchor = config.profile_name or config.school
    if not anchor:
        for line in card.get("text", "").split("\n"):
            line = line.strip()
            if line and not SELECT_PROFILE_PATTERN.search(line) and ":" not in line:
                anchor = line
                break
    if not anchor:
        anchor = card.get("text", "")[:40]

    containers = page.locator("div, section, article, li").filter(
        has_text=re.compile(re.escape(anchor[:40]), re.I)
    )
    count = min(containers.count(), 30)
    best_idx: int | None = None
    best_len = 10**9
    for idx in range(count):
        item = containers.nth(idx)
        try:
            if not item.is_visible():
                continue
            text = (item.inner_text(timeout=500) or "").strip()
            if not SELECT_PROFILE_PATTERN.search(text):
                continue
            if config.profile_name and config.profile_name.lower() not in text.lower():
                continue
            if config.school and config.school.lower() not in text.lower():
                continue
            if len(text) < best_len:
                best_len = len(text)
                best_idx = idx
        except Exception:
            continue

    if best_idx is None:
        return False

    btn = containers.nth(best_idx).get_by_role("button", name=SELECT_PROFILE_PATTERN).first
    if _is_control_disabled(btn):
        log.error("Select profile button is disabled")
        return False
    log.info("Clicking Select profile on innermost matching container (%d chars)", best_len)
    btn.click(timeout=5000)
    human_pause()
    return True


def select_checkout_profile(page: Page, config: ProfileConfig, log: logging.Logger) -> bool:
    """On 'Who are you ordering for?' click Select profile for one/matching card."""
    if not is_profile_chooser_page(page):
        log.info("Profile chooser not detected; skipping profile selection")
        return True

    raw_cards = discover_profile_cards(page)
    log.info("Raw profile matches: %d", len(raw_cards))
    for card in raw_cards:
        log.info("  raw preview: %s", card.get("text", "")[:120])

    cards = deduplicate_profile_cards(raw_cards)
    log.info("Deduplicated profile matches: %d", len(cards))
    for card in cards:
        log.info("  deduped preview: %s", card.get("text", "")[:120])

    if not cards:
        log.error("Profile chooser visible but no profile cards found")
        return False

    eligible = [c for c in cards if _profile_card_matches(c["text"], config)]
    if config.profile_name or config.school:
        if len(eligible) != 1:
            log.error(
                "Expected exactly one deduplicated profile matching filters "
                "(name=%r school=%r); found %d",
                config.profile_name,
                config.school,
                len(eligible),
            )
            return False
        target = eligible[0]
    elif len(cards) == 1:
        target = cards[0]
    else:
        log.error(
            "Multiple deduplicated profile cards (%d) — pass --profile-name and/or --school",
            len(cards),
        )
        return False

    if _click_select_profile_for_card(page, target, config, log):
        return True

    log.warning("Innermost Select profile click failed; trying single global button")
    buttons = page.get_by_role("button", name=SELECT_PROFILE_PATTERN)
    count = buttons.count()
    if count == 1:
        if _is_control_disabled(buttons.first):
            log.error("Select profile button is disabled")
            return False
        buttons.first.click(timeout=5000)
        human_pause()
        return True

    log.error("Could not click Select profile (buttons=%d)", count)
    return False


def count_active_chooser_overlays(page: Page) -> int:
    """Count visible active profile chooser dropdown overlays."""
    loc = page.locator(CHOOSER_OVERLAY_ACTIVE_SELECTOR)
    visible = 0
    count = loc.count()
    for idx in range(count):
        try:
            if loc.nth(idx).is_visible():
                visible += 1
        except Exception:
            continue
    return visible


def profile_chooser_overlay_open(page: Page) -> bool:
    return count_active_chooser_overlays(page) > 0


def close_profile_chooser_overlay(page: Page, log: logging.Logger) -> OverlayCloseResult:
    """
    Close lingering profile chooser dropdown after Select profile.

    Tries, in order: click outside calendar, Escape, wait for hidden, verify count.
    """
    active_before = count_active_chooser_overlays(page)
    if active_before == 0:
        log.info("profile_overlay_open_before_close: false")
        log.info("profile_overlay_closed: true (strategy=none_needed)")
        return OverlayCloseResult(
            was_open=False,
            closed=True,
            strategy="none_needed",
            active_count_before=0,
            active_count_after=0,
        )

    log.info("profile_overlay_open_before_close: true (active_count=%d)", active_before)

    outside_targets = [
        page.get_by_text(re.compile(r"choose your meals", re.I)),
        page.locator("[class*='calendar' i]").first,
        page.locator("main").first,
        page.locator("body"),
    ]
    for target in outside_targets:
        try:
            if target.count() == 0:
                continue
            item = target.first
            if not item.is_visible(timeout=500):
                continue
            log.info("Attempting overlay close via click_outside")
            item.click(timeout=3000, position={"x": 8, "y": 8})
            human_pause(0.5)
            active_after = count_active_chooser_overlays(page)
            if active_after == 0:
                log.info("profile_overlay_closed: true (strategy=click_outside)")
                return OverlayCloseResult(
                    was_open=True,
                    closed=True,
                    strategy="click_outside",
                    active_count_before=active_before,
                    active_count_after=active_after,
                )
        except Exception as exc:
            log.debug("click_outside attempt failed: %s", exc)

    try:
        log.info("Attempting overlay close via escape")
        page.keyboard.press("Escape")
        human_pause(0.5)
        active_after = count_active_chooser_overlays(page)
        if active_after == 0:
            log.info("profile_overlay_closed: true (strategy=escape)")
            return OverlayCloseResult(
                was_open=True,
                closed=True,
                strategy="escape",
                active_count_before=active_before,
                active_count_after=active_after,
            )
    except Exception as exc:
        log.debug("escape attempt failed: %s", exc)

    try:
        log.info("Attempting overlay close via wait_hidden")
        page.locator(CHOOSER_OVERLAY_ACTIVE_SELECTOR).first.wait_for(state="hidden", timeout=4000)
        active_after = count_active_chooser_overlays(page)
        log.info("profile_overlay_closed: true (strategy=wait_hidden)")
        return OverlayCloseResult(
            was_open=True,
            closed=True,
            strategy="wait_hidden",
            active_count_before=active_before,
            active_count_after=active_after,
        )
    except Exception as exc:
        log.debug("wait_hidden attempt failed: %s", exc)

    active_after = count_active_chooser_overlays(page)
    closed = active_after == 0
    log.info(
        "profile_overlay_closed: %s (strategy=verify_count active_count=%d)",
        str(closed).lower(),
        active_after,
    )
    return OverlayCloseResult(
        was_open=True,
        closed=closed,
        strategy="verify_count" if closed else None,
        active_count_before=active_before,
        active_count_after=active_after,
    )


def ensure_profile_chooser_overlay_closed(page: Page, log: logging.Logger) -> OverlayCloseResult:
    """Ensure no active Chooser__options--active overlay blocks calendar clicks."""
    if not profile_chooser_overlay_open(page):
        log.info("profile_overlay_open_before_close: false")
        log.info("profile_overlay_closed: true (strategy=already_closed)")
        return OverlayCloseResult(
            was_open=False,
            closed=True,
            strategy="already_closed",
            active_count_after=0,
        )
    return close_profile_chooser_overlay(page, log)


def count_visible_modals(page: Page) -> int:
    """Count visible Modal / Modal__content overlays."""
    seen: set[str] = set()
    visible_count = 0
    for selector in (MODAL_CONTENT_SELECTOR, MODAL_ROOT_SELECTOR):
        loc = page.locator(selector)
        for idx in range(min(loc.count(), 15)):
            item = loc.nth(idx)
            try:
                if not item.is_visible():
                    continue
                handle = item.evaluate(
                    "el => el.className + '|' + Math.round(el.getBoundingClientRect().x)"
                )
                if handle in seen:
                    continue
                seen.add(handle)
                visible_count += 1
            except Exception:
                continue
    return visible_count


def meal_selection_modal_open(page: Page) -> bool:
    return count_visible_modals(page) > 0


def _first_visible_meal_modal(page: Page) -> Any | None:
    for selector in (MODAL_CONTENT_SELECTOR, MODAL_ROOT_SELECTOR):
        loc = page.locator(selector)
        for idx in range(min(loc.count(), 5)):
            item = loc.nth(idx)
            try:
                if item.is_visible():
                    return item
            except Exception:
                continue
    return None


def _modal_control_already_selected(item: Any) -> bool:
    try:
        if item.evaluate(
            "el => el.checked === true || el.getAttribute('aria-checked') === 'true'"
        ):
            return True
        class_name = (item.get_attribute("class") or "").lower()
        if "selected" in class_name or "active" in class_name:
            return True
        if item.get_attribute("aria-selected") == "true":
            return True
    except Exception:
        pass
    return False


def select_modal_regular_size(page: Page, log: logging.Logger) -> bool:
    """Select default Regular size inside the open meal item modal."""
    modal = _first_visible_meal_modal(page)
    if modal is None:
        log.warning("No visible meal modal for Regular size selection")
        return False

    selectors = "button, a, [role='button'], [role='radio'], label, input[type='radio']"
    loc = modal.locator(selectors)
    for idx in range(min(loc.count(), 40)):
        item = loc.nth(idx)
        try:
            if not item.is_visible():
                continue
            text = (item.inner_text(timeout=300) or item.get_attribute("value") or "").strip()
            if not text:
                text = (item.get_attribute("aria-label") or "").strip()
            if not text or not _control_matches(text, REGULAR_SIZE_PATTERNS):
                continue
            if is_forbidden_control_text(text):
                continue
            if _modal_control_already_selected(item):
                log.info("Regular size already selected in meal modal")
                return True
            if _is_control_disabled(item):
                log.info("Regular size control disabled; assuming default")
                return True
            log.info("Selecting Regular size in meal modal")
            item.click(timeout=4000)
            human_pause()
            return True
        except Exception:
            continue

    log.info("Regular size option not found in meal modal; proceeding to Add")
    return False


def click_meal_modal_add_button(page: Page, log: logging.Logger) -> bool:
    """Click Add only inside the visible meal item modal."""
    modal = _first_visible_meal_modal(page)
    if modal is None:
        log.warning("No visible meal modal for Add button")
        return False

    selectors = "button, a, [role='button'], input[type='button'], input[type='submit']"
    loc = modal.locator(selectors)
    for idx in range(min(loc.count(), 30)):
        item = loc.nth(idx)
        try:
            if not item.is_visible():
                continue
            text = (item.inner_text(timeout=300) or item.get_attribute("value") or "").strip()
            if not text:
                text = (item.get_attribute("aria-label") or "").strip()
            if not is_modal_add_button_text(text):
                continue
            if _is_control_disabled(item):
                log.warning("Add button disabled in meal modal")
                return False
            log.info("Clicking Add in meal modal")
            item.click(timeout=5000)
            human_pause()
            return True
        except Exception as exc:
            log.warning("Could not click Add in meal modal: %s", exc)
            continue
    return False


def wait_for_meal_modal_closed(
    page: Page,
    log: logging.Logger,
    timeout_ms: int = 10_000,
) -> bool:
    deadline = time.time() + (timeout_ms / 1000.0)
    while time.time() < deadline:
        if not meal_selection_modal_open(page):
            log.info("Meal modal closed after Add")
            return True
        human_pause(0.3)
    log.warning("Meal modal still open after Add (timeout %sms)", timeout_ms)
    return False


def add_selected_meal_via_modal(page: Page, log: logging.Logger) -> ModalAddResult:
    """
    After meal option click: select Regular size, click Add in modal, wait for close.
    Does not click Cancel. Add is allowed only inside the meal item modal.
    """
    result = ModalAddResult()
    if not meal_selection_modal_open(page):
        result.add_failed_reason = "modal_not_open"
        return result

    result.regular_size_selected = select_modal_regular_size(page, log)
    if not click_meal_modal_add_button(page, log):
        result.add_failed_reason = "add_button_not_found_or_disabled"
        return result

    result.add_clicked = True
    human_pause()
    if wait_for_meal_modal_closed(page, log):
        result.modal_closed = True
    else:
        result.add_failed_reason = "modal_still_open_after_add"
    return result


def _click_safe_modal_dismiss_button(page: Page, log: logging.Logger) -> bool:
    """Click X / close / back / cancel inside a visible modal (never submit/pay)."""
    roots = page.locator(MODAL_ROOT_SELECTOR)
    if roots.count() == 0:
        roots = page.locator(MODAL_CONTENT_SELECTOR)

    for root_idx in range(min(roots.count(), 5)):
        modal = roots.nth(root_idx)
        try:
            if not modal.is_visible():
                continue
        except Exception:
            continue

        dedicated = modal.locator(
            "button[aria-label*='close' i], [class*='close' i], [class*='Close' i]"
        )
        for idx in range(min(dedicated.count(), 10)):
            btn = dedicated.nth(idx)
            try:
                if not btn.is_visible():
                    continue
                aria = (btn.get_attribute("aria-label") or "").strip()
                text = (btn.inner_text(timeout=300) or "").strip()
                label = text or aria
                if label and is_forbidden_control_text(label):
                    log.info("MODAL_FORBIDDEN_CLOSE_SKIPPED: %r", label)
                    continue
                if not label and not btn.get_attribute("class"):
                    continue
                if label and not (is_safe_modal_close_text(text) or is_safe_modal_close_aria(aria)):
                    if "close" not in (btn.get_attribute("class") or "").lower():
                        continue
                log.info("Attempting modal close via safe_button: %r", label or "[close icon]")
                btn.click(timeout=3000)
                human_pause(0.5)
                if not meal_selection_modal_open(page):
                    return True
            except Exception:
                continue

        candidates = modal.locator("button, a, [role='button']")
        for idx in range(min(candidates.count(), 30)):
            btn = candidates.nth(idx)
            try:
                if not btn.is_visible():
                    continue
                aria = (btn.get_attribute("aria-label") or "").strip()
                text = (btn.inner_text(timeout=300) or "").strip()
                if not (is_safe_modal_close_text(text) or is_safe_modal_close_aria(aria)):
                    continue
                label = text or aria
                if is_forbidden_control_text(label):
                    log.info("MODAL_FORBIDDEN_CLOSE_SKIPPED: %r", label)
                    continue
                log.info("Attempting modal close via safe_button: %r", label)
                btn.click(timeout=3000)
                human_pause(0.5)
                if not meal_selection_modal_open(page):
                    return True
            except Exception:
                continue
    return False


def close_meal_selection_modal(page: Page, log: logging.Logger) -> OverlayCloseResult:
    """
    Close meal-selection Modal__content overlay after a day pick.

    Tries: safe X/back/cancel, click outside/backdrop, Escape, wait hidden, verify.
    """
    active_before = count_visible_modals(page)
    if active_before == 0:
        log.info("modal_open_before_close: false")
        log.info("modal_closed: true (strategy=none_needed)")
        return OverlayCloseResult(
            was_open=False,
            closed=True,
            strategy="none_needed",
            active_count_before=0,
            active_count_after=0,
        )

    log.info("modal_open_before_close: true (visible_count=%d)", active_before)

    if _click_safe_modal_dismiss_button(page, log):
        active_after = count_visible_modals(page)
        log.info("modal_closed: true (strategy=safe_button)")
        return OverlayCloseResult(
            was_open=True,
            closed=True,
            strategy="safe_button",
            active_count_before=active_before,
            active_count_after=active_after,
        )

    outside_targets = [
        page.locator(f"{MODAL_ROOT_SELECTOR}").first,
        page.get_by_text(re.compile(r"choose your meals", re.I)),
        page.locator("[class*='calendar' i]").first,
        page.locator("body"),
    ]
    for target in outside_targets:
        try:
            if target.count() == 0:
                continue
            item = target.first
            if not item.is_visible(timeout=500):
                continue
            log.info("Attempting modal close via click_outside")
            item.click(timeout=3000, position={"x": 8, "y": 8})
            human_pause(0.5)
            active_after = count_visible_modals(page)
            if active_after == 0:
                log.info("modal_closed: true (strategy=click_outside)")
                return OverlayCloseResult(
                    was_open=True,
                    closed=True,
                    strategy="click_outside",
                    active_count_before=active_before,
                    active_count_after=active_after,
                )
        except Exception as exc:
            log.debug("modal click_outside failed: %s", exc)

    try:
        log.info("Attempting modal close via escape")
        page.keyboard.press("Escape")
        human_pause(0.5)
        active_after = count_visible_modals(page)
        if active_after == 0:
            log.info("modal_closed: true (strategy=escape)")
            return OverlayCloseResult(
                was_open=True,
                closed=True,
                strategy="escape",
                active_count_before=active_before,
                active_count_after=active_after,
            )
    except Exception as exc:
        log.debug("modal escape failed: %s", exc)

    try:
        log.info("Attempting modal close via wait_hidden")
        page.locator(MODAL_CONTENT_SELECTOR).first.wait_for(state="hidden", timeout=4000)
        active_after = count_visible_modals(page)
        log.info("modal_closed: true (strategy=wait_hidden)")
        return OverlayCloseResult(
            was_open=True,
            closed=True,
            strategy="wait_hidden",
            active_count_before=active_before,
            active_count_after=active_after,
        )
    except Exception as exc:
        log.debug("modal wait_hidden failed: %s", exc)

    active_after = count_visible_modals(page)
    closed = active_after == 0
    log.info(
        "modal_closed: %s (strategy=verify_count visible_count=%d)",
        str(closed).lower(),
        active_after,
    )
    return OverlayCloseResult(
        was_open=True,
        closed=closed,
        strategy="verify_count" if closed else None,
        active_count_before=active_before,
        active_count_after=active_after,
    )


def ensure_meal_selection_modal_closed(page: Page, log: logging.Logger) -> OverlayCloseResult:
    """Ensure no visible Modal/Modal__content blocks the next calendar day click."""
    if not meal_selection_modal_open(page):
        log.info("modal_open_before_close: false")
        log.info("modal_closed: true (strategy=already_closed)")
        return OverlayCloseResult(
            was_open=False,
            closed=True,
            strategy="already_closed",
            active_count_after=0,
        )
    return close_meal_selection_modal(page, log)


def ensure_calendar_ui_unblocked(page: Page, log: logging.Logger) -> bool:
    """Close profile chooser overlay and meal modal before calendar interaction."""
    overlay = ensure_profile_chooser_overlay_closed(page, log)
    if not overlay.closed:
        return False
    modal = ensure_meal_selection_modal_closed(page, log)
    return modal.closed


def _page_is_loaded(page: Page) -> bool:
    url = (page.url or "").lower()
    return not url.startswith("about:") and url not in ("", "about:blank")


def session_login_required(page: Page) -> bool:
    """True when a loaded Simply Fresh page shows login form or logged-out state."""
    if not _page_is_loaded(page):
        return False
    if page_has_login_form(page):
        return True
    if "thesimplyfreshkitchen.com" in page.url.lower() and not page_looks_logged_in(page):
        return True
    return False


def navigate_to_meal_calendar(
    page: Page,
    run_dir: Path,
    config: ProfileConfig,
    log: logging.Logger,
) -> NavigationResult:
    """
    Logged-in flow: home or /profile -> Order Now -> profile chooser -> meal calendar.

    Login/session is checked only after navigating with storage state loaded — not on
    the initial about:blank page.
    """
    result = NavigationResult()
    clicked_order = False
    loaded_sf_page = False

    for url in (START_URL, PROFILE_URL):
        log.info("Step: loading %s", url)
        page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
        human_pause()
        save_step_debug(page, run_dir, "landing", PROBE_DIR, log)

        if session_login_required(page):
            log.error(
                "Login required or session expired after loading %s — "
                "run probe_simplyfresh.py --manual-login",
                url,
            )
            result.login_required = True
            save_step_debug(page, run_dir, "session_expired", PROBE_DIR, log)
            return result

        loaded_sf_page = True
        if click_order_now(page, log):
            clicked_order = True
            break

    if not loaded_sf_page:
        log.error("Could not load Simply Fresh home/profile pages")
        save_step_debug(page, run_dir, "landing_failed", PROBE_DIR, log)
        return result

    if not clicked_order:
        log.error("Could not find Order Now or Place Order on home/profile")
        save_step_debug(page, run_dir, "order_now_failed", PROBE_DIR, log)
        return result

    human_pause()
    save_step_debug(page, run_dir, "after_order_now", PROBE_DIR, log)

    if is_profile_chooser_page(page):
        result.profile_chooser_seen = True
        if not select_checkout_profile(page, config, log):
            save_step_debug(page, run_dir, "select_profile_failed", PROBE_DIR, log)
            return result
        result.profile_selected = True
        human_pause()
        save_step_debug(page, run_dir, "after_select_profile", PROBE_DIR, log)
        overlay = ensure_profile_chooser_overlay_closed(page, log)
        result.profile_overlay_close_strategy = overlay.strategy
        if not overlay.closed:
            log.error(
                "Profile chooser overlay still open after close attempts (active_count=%d)",
                overlay.active_count_after,
            )
            save_step_debug(page, run_dir, "profile_overlay_still_open", PROBE_DIR, log)
            return result
        save_step_debug(page, run_dir, "profile_overlay_closed", PROBE_DIR, log)

    if not wait_for_meal_calendar(page, log):
        save_step_debug(page, run_dir, "meal_calendar_timeout", PROBE_DIR, log)
        return result

    result.meal_calendar_reached = True
    result.ok = True
    save_step_debug(page, run_dir, "after_meal_calendar", PROBE_DIR, log)
    return result


def discover_meal_card_labels(page: Page) -> list[str]:
    """Meal card titles/descriptions visible on the calendar page."""
    raw: list[str] = page.evaluate(
        """() => {
        const labels = [];
        const seen = new Set();
        const push = (text) => {
          text = (text || '').trim().replace(/\\s+/g, ' ');
          if (!text || text.length < 8 || text.length > 160) return;
          if (seen.has(text)) return;
          seen.add(text);
          labels.push(text);
        };
        document.querySelectorAll(
          '[class*="meal" i], [class*="menu" i], [class*="card" i], [class*="item" i], label, button'
        ).forEach(el => {
          const text = (el.innerText || el.textContent || '').trim();
          if (!text) return;
          const lines = text.split(/\\n+/).map(l => l.trim()).filter(Boolean);
          if (lines.length === 0) return;
          const title = lines[0];
          if (/choose your meals|select profile|order now|classroom:|school:/i.test(title)) return;
          if (/^\\d{1,2}$/.test(title)) return;
          if (/^(Mon|Tue|Wed|Thu|Fri|Sat|Sun)/i.test(title)) return;
          push(title);
        });
        return labels.slice(0, 30);
    }"""
    )
    return raw


# Late imports from feasibility probe to avoid circular imports at module load.
def page_has_login_form(page: Page) -> bool:
    from probe_simplyfresh import page_has_login_form as _fn

    return _fn(page)


def page_looks_logged_in(page: Page) -> bool:
    from probe_simplyfresh import page_looks_logged_in as _fn

    return _fn(page)
