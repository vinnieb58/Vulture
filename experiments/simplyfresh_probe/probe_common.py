"""Shared helpers for Simply Fresh Kitchen probe scripts."""

from __future__ import annotations

import logging
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

try:
    from playwright.sync_api import Page
except ImportError:
    Page = Any  # type: ignore[misc, assignment]

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

ORDER_LINK_PATTERNS = [
    re.compile(r"place\s*order", re.I),
    re.compile(r"order\s*now", re.I),
    re.compile(r"order\s*meals", re.I),
]

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


def load_storage_state_path() -> Optional[str]:
    if STORAGE_STATE_PATH.exists():
        return str(STORAGE_STATE_PATH)
    return None


def new_run_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def safe_filename(label: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]+", "_", label).strip("_").lower()


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


def is_forbidden_control_text(text: str) -> bool:
    normalized = " ".join(text.split())
    return any(pattern.search(normalized) for pattern in FORBIDDEN_CONTROL_PATTERNS)


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
    visible = ""
    try:
        visible = page.inner_text("body")
    except Exception:
        visible = current

    for pattern in AUTOSAVE_MARKERS:
        if pattern.search(visible) and not pattern.search(previous_html):
            return True
    return False


def detect_month_label(page: Page) -> Optional[str]:
    text = ""
    try:
        text = page.inner_text("body")
    except Exception:
        text = page.content()
    match = MONTH_NAME_PATTERN.search(text)
    if match:
        return f"{match.group(1)} {match.group(2)}"
    return None
