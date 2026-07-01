"""
Heron Atomiq UI probe — read-only inspection of Brown & Root expense entry flow.

Usage:
  python experiments/heron/heron_atomiq_probe.py --headed --save-session
  python experiments/heron/heron_atomiq_probe.py --headed --use-session --dump-ui
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from heron_schema import (
    ATOMIQ_URL,
    AUTH_DIR,
    LOCALE,
    NAV_TIMEOUT_MS,
    TIMEZONE,
    USER_AGENT,
    VIEWPORT,
    resolve_storage_state_path,
)

if TYPE_CHECKING:
    from playwright.sync_api import Page

try:
    from playwright.sync_api import Page as _Page, sync_playwright
except ImportError:
    _Page = Any  # type: ignore[misc, assignment]
    sync_playwright = None  # type: ignore[assignment]

PROBE_DIR = Path(__file__).resolve().parent
ARTIFACTS_DIR = PROBE_DIR / "artifacts"


def setup_logging() -> logging.Logger:
    logging.basicConfig(level=logging.INFO, format="%(levelname)-8s %(message)s", stream=sys.stdout)
    return logging.getLogger("heron.atomiq_probe")


def dump_page_ui(page: _Page) -> dict[str, Any]:
    """Collect visible form fields, buttons, labels, selects, and ARIA roles."""
    return page.evaluate(
        """() => {
        const visible = (el) => {
          if (!el) return false;
          const style = window.getComputedStyle(el);
          if (style.visibility === 'hidden' || style.display === 'none') return false;
          const rect = el.getBoundingClientRect();
          return rect.width > 0 && rect.height > 0;
        };

        const textOf = (el) => {
          const t = (el.innerText || el.textContent || el.value || '').trim().replace(/\\s+/g, ' ');
          if (t) return t.slice(0, 300);
          return (el.getAttribute('aria-label') || el.getAttribute('title') || '').trim().slice(0, 300);
        };

        const fields = [];
        const selectors = 'input, textarea, select, button, a, [role], label';
        const seen = new Set();
        for (const el of document.querySelectorAll(selectors)) {
          if (!visible(el)) continue;
          const key = el.tagName + '|' + (el.getAttribute('name') || '') + '|' + textOf(el);
          if (seen.has(key)) continue;
          seen.add(key);
          const entry = {
            tag: el.tagName.toLowerCase(),
            type: el.getAttribute('type'),
            name: el.getAttribute('name'),
            id: el.getAttribute('id'),
            role: el.getAttribute('role'),
            text: textOf(el),
            placeholder: el.getAttribute('placeholder'),
            aria_label: el.getAttribute('aria-label'),
            href: el.getAttribute('href'),
            disabled: !!(el.disabled || el.getAttribute('aria-disabled') === 'true'),
          };
          if (el.tagName === 'SELECT') {
            entry.options = Array.from(el.options || []).slice(0, 50).map(o => ({
              value: o.value,
              text: (o.text || '').trim().slice(0, 120),
              selected: o.selected,
            }));
          }
          fields.push(entry);
          if (fields.length >= 400) break;
        }
        return {
          url: location.href,
          title: document.title,
          captured_at: new Date().toISOString(),
          fields,
        };
    }"""
    )


def save_session(context: Any, log: logging.Logger) -> Path:
    AUTH_DIR.mkdir(parents=True, exist_ok=True)
    path = resolve_storage_state_path()
    context.storage_state(path=str(path))
    log.info("Saved Atomiq session state: %s", path)
    return path


def run_probe(
    *,
    headed: bool,
    save_session_flag: bool,
    use_session: bool,
    dump_ui: bool,
    wait_seconds: int,
) -> int:
    log = setup_logging()
    if sync_playwright is None:
        print(
            "Playwright is required. Install with: pip install playwright && playwright install chromium",
            file=sys.stderr,
        )
        return 1

    ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir = ARTIFACTS_DIR / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    storage_path = resolve_storage_state_path()
    launch_kwargs: dict[str, Any] = {"headless": not headed}

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(**launch_kwargs)
        context_kwargs: dict[str, Any] = {
            "viewport": VIEWPORT,
            "user_agent": USER_AGENT,
            "locale": LOCALE,
            "timezone_id": TIMEZONE,
        }
        if use_session and storage_path.is_file():
            context_kwargs["storage_state"] = str(storage_path)
            log.info("Loading session from %s", storage_path)
        elif use_session:
            log.warning("No session file at %s — manual login required", storage_path)

        context = browser.new_context(**context_kwargs)
        page = context.new_page()
        page.set_default_timeout(NAV_TIMEOUT_MS)

        log.info("Opening Atomiq (manual login only — no SSO/MFA bypass): %s", ATOMIQ_URL)
        page.goto(ATOMIQ_URL, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
        log.info(
            "Complete login in the browser. Waiting %d seconds before probe actions...",
            wait_seconds,
        )
        time.sleep(wait_seconds)

        if save_session_flag:
            save_session(context, log)

        if dump_ui:
            ui_dump = dump_page_ui(page)
            out_path = run_dir / "atomiq_ui_dump.json"
            out_path.write_text(json.dumps(ui_dump, indent=2) + "\n", encoding="utf-8")
            log.info("Wrote UI dump (%d fields) to %s", len(ui_dump.get("fields", [])), out_path)
            print(json.dumps(ui_dump, indent=2))
        else:
            log.info("Probe idle — use --dump-ui to capture form fields after navigation.")

        log.info("Probe complete. Browser will close.")
        context.close()
        browser.close()

    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Heron Atomiq read-only UI probe (manual login, no submission)."
    )
    parser.add_argument("--headed", action="store_true", help="Run Chromium in headed mode.")
    parser.add_argument(
        "--save-session",
        action="store_true",
        help="Save Playwright storage state to experiments/heron/.auth/ after wait.",
    )
    parser.add_argument(
        "--use-session",
        action="store_true",
        help="Load saved storage state before opening Atomiq.",
    )
    parser.add_argument(
        "--dump-ui",
        action="store_true",
        help="Dump visible form fields, buttons, labels, and selects to JSON.",
    )
    parser.add_argument(
        "--wait-seconds",
        type=int,
        default=120,
        help="Seconds to wait for manual login/navigation (default: 120).",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if not args.headed:
        logging.getLogger("heron.atomiq_probe").warning(
            "Headless probe may block on SSO — prefer --headed on Raven with xvfb-run."
        )
    return run_probe(
        headed=args.headed,
        save_session_flag=args.save_session,
        use_session=args.use_session,
        dump_ui=args.dump_ui,
        wait_seconds=args.wait_seconds,
    )


if __name__ == "__main__":
    sys.exit(main())
