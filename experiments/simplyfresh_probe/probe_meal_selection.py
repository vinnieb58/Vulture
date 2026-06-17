"""
Simply Fresh Kitchen meal-selection dry-run probe
=================================================
Navigates to the order calendar and optionally selects non-vegetarian
meal options for up to N weekdays. Does NOT submit, save order, checkout,
pay, confirm, or finalize anything.

Usage:
    python3 experiments/simplyfresh_probe/probe_meal_selection.py --inspect-only --headless
    python3 experiments/simplyfresh_probe/probe_meal_selection.py --dry-run-select --max-days 3 --headless
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, Optional

# Allow imports from probe package directory when run as a script.
_PROBE_DIR = Path(__file__).resolve().parent
if str(_PROBE_DIR) not in sys.path:
    sys.path.insert(0, str(_PROBE_DIR))

from meal_classification import MealChoiceResult, choose_non_vegetarian_option, classify_meal_option
from probe_common import (
    NAV_TIMEOUT_MS,
    PROBE_DIR,
    START_URL,
    STORAGE_STATE_PATH,
    TIMEZONE,
    USER_AGENT,
    VIEWPORT,
    capture_named,
    detect_autosave_markers,
    detect_month_label,
    find_forbidden_controls,
    has_display,
    human_pause,
    is_forbidden_control_text,
    load_storage_state_path,
    new_run_id,
    safe_filename,
    setup_logging,
)

try:
    from playwright.sync_api import Page, TimeoutError as PlaywrightTimeout, sync_playwright
except ImportError:
    print("ERROR: playwright is not installed.")
    print("  .venv/bin/pip install -r experiments/simplyfresh_probe/requirements.txt")
    print("  .venv/bin/playwright install chromium")
    sys.exit(1)

# Reuse login/navigation helpers from feasibility probe.
from probe_simplyfresh import (  # noqa: E402
    ORDER_LINK_PATTERNS,
    click_safe_link,
    find_clickable_candidates,
    page_has_login_form,
    page_looks_logged_in,
)

log = setup_logging("simplyfresh_meal_selection")

LOCALE = "en-US"

WEEKEND_PATTERN = re.compile(r"\b(sat|sun|saturday|sunday)\b", re.I)


@dataclass
class CalendarDay:
    day_id: str
    label: str
    enabled: bool
    is_weekend: bool
    already_selected: bool
    meal_options_visible: bool
    selected_option: Optional[str] = None
    meal_options: list[str] = field(default_factory=list)
    skip_reason: Optional[str] = None


@dataclass
class MealSelectionReport:
    logged_in: bool = False
    order_page_reached: bool = False
    month_detected: Optional[str] = None
    days_detected: int = 0
    selectable_days_detected: int = 0
    days_attempted: int = 0
    days_selected: int = 0
    days_skipped: int = 0
    uncertain_days: int = 0
    vegetarian_options_detected: int = 0
    non_vegetarian_options_detected: int = 0
    forbidden_controls_detected: list[str] = field(default_factory=list)
    autosave_risk_detected: bool = False
    recommended_next_step: str = ""
    calendar_days: list[CalendarDay] = field(default_factory=list)
    day_results: list[dict[str, Any]] = field(default_factory=list)


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Simply Fresh Kitchen meal-selection dry-run (no submit/finalize)",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--inspect-only",
        action="store_true",
        help="Map calendar and meal options without clicking meals",
    )
    mode.add_argument(
        "--dry-run-select",
        action="store_true",
        help="Attempt safe non-vegetarian selections up to --max-days",
    )
    parser.add_argument(
        "--max-days",
        type=int,
        default=3,
        help="Max weekdays to attempt in --dry-run-select (default: 3)",
    )
    parser.add_argument("--headed", action="store_true")
    parser.add_argument("--headless", action="store_true")
    parser.add_argument(
        "--continue-after-autosave",
        action="store_true",
        help="Continue selecting after AUTOSAVE_RISK_DETECTED (default: stop)",
    )
    return parser.parse_args(argv)


def resolve_headed(args: argparse.Namespace) -> bool:
    if args.headed:
        return True
    if args.headless:
        return False
    return False


def discover_calendar_days(page: Page) -> list[CalendarDay]:
    """Find day cells/buttons in the visible ordering calendar."""
    raw: list[dict[str, Any]] = page.evaluate(
        """() => {
        const results = [];
        const seen = new Set();
        const candidates = document.querySelectorAll(
          'button, a, [role="button"], [role="gridcell"], td, div, span'
        );
        const monthPattern = /\\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\\b/i;
        for (const el of candidates) {
          const text = (el.innerText || el.textContent || '').trim().replace(/\\s+/g, ' ');
          if (!text || text.length > 40) continue;
          const aria = (el.getAttribute('aria-label') || '').trim();
          const dataDate = el.getAttribute('data-date') || el.getAttribute('data-day') || '';
          const label = aria || text;
          const dayNum = /^\\d{1,2}$/.test(text) ? text : null;
          const looksLikeDay = dayNum || dataDate || /\\b\\d{1,2}\\b/.test(label);
          if (!looksLikeDay) continue;
          const inCalendar = el.closest('[class*="calendar" i], [class*="Calendar"], table, [role="grid"]');
          if (!inCalendar && !dataDate && !dayNum) continue;
          const key = (dataDate || label) + '|' + (el.className || '');
          if (seen.has(key)) continue;
          seen.add(key);
          const disabled = el.hasAttribute('disabled') ||
            el.getAttribute('aria-disabled') === 'true' ||
            (el.className || '').toLowerCase().includes('disabled');
          const selected = el.getAttribute('aria-selected') === 'true' ||
            (el.className || '').toLowerCase().includes('selected') ||
            (el.className || '').toLowerCase().includes('active');
          results.push({
            day_id: dataDate || label || text,
            label: label,
            enabled: !disabled,
            is_weekend: /\\b(sat|sun|saturday|sunday)\\b/i.test(label),
            already_selected: selected,
            meal_options_visible: false,
            selected_option: null,
            meal_options: [],
          });
          if (results.length >= 40) break;
        }
        return results;
    }"""
    )

    days: list[CalendarDay] = []
    for item in raw:
        days.append(
            CalendarDay(
                day_id=str(item.get("day_id", "")),
                label=str(item.get("label", "")),
                enabled=bool(item.get("enabled", False)),
                is_weekend=bool(item.get("is_weekend", False)),
                already_selected=bool(item.get("already_selected", False)),
                meal_options_visible=bool(item.get("meal_options_visible", False)),
                selected_option=item.get("selected_option"),
                meal_options=list(item.get("meal_options") or []),
            )
        )
    return days


def discover_meal_options(page: Page) -> list[dict[str, str]]:
    """Return visible meal option labels and coarse selection state."""
    raw: list[dict[str, str]] = page.evaluate(
        """() => {
        const options = [];
        const seen = new Set();
        const inputs = document.querySelectorAll('input[type="radio"], [role="radio"]');
        for (const input of inputs) {
          let label = '';
          const id = input.id;
          if (id) {
            const lbl = document.querySelector(`label[for="${id}"]`);
            if (lbl) label = (lbl.innerText || '').trim();
          }
          if (!label) {
            const parent = input.closest('label');
            if (parent) label = (parent.innerText || '').trim();
          }
          if (!label) {
            label = (input.getAttribute('aria-label') || input.value || '').trim();
          }
          label = label.replace(/\\s+/g, ' ');
          if (!label || label.length > 120 || seen.has(label)) continue;
          seen.add(label);
          const checked = input.checked || input.getAttribute('aria-checked') === 'true';
          options.push({ label, checked: checked ? 'true' : 'false' });
        }
        if (options.length === 0) {
          document.querySelectorAll('[class*="meal" i], [class*="menu-item" i], [class*="entree" i]').forEach(el => {
            const label = (el.innerText || '').trim().replace(/\\s+/g, ' ');
            if (!label || label.length > 120 || seen.has(label)) return;
            if (label.split(' ').length > 12) return;
            seen.add(label);
            options.push({ label, checked: 'false' });
          });
        }
        return options.slice(0, 20);
    }"""
    )
    return raw


def get_selected_meal_label(options: list[dict[str, str]]) -> Optional[str]:
    for opt in options:
        if opt.get("checked") == "true":
            return opt.get("label")
    return None


def click_calendar_day(page: Page, day: CalendarDay) -> bool:
    label = day.label
    try:
        page.get_by_role("button", name=re.compile(re.escape(label[:20]), re.I)).first.click(timeout=4000)
        human_pause()
        return True
    except Exception:
        pass
    try:
        if day.day_id:
            loc = page.locator(f'[data-date="{day.day_id}"]')
            if loc.count() > 0:
                loc.first.click(timeout=4000)
                human_pause()
                return True
    except Exception:
        pass
    try:
        page.locator(f"text={label}").first.click(timeout=4000)
        human_pause()
        return True
    except Exception as exc:
        log.warning("Could not open day %s: %s", label, exc)
        return False


def click_meal_option(page: Page, label: str) -> bool:
    if is_forbidden_control_text(label):
        log.warning("FORBIDDEN_MEAL_CONTROL_SKIPPED: %s", label)
        return False
    try:
        page.get_by_label(label, exact=False).first.click(timeout=4000)
        human_pause()
        return True
    except Exception:
        pass
    try:
        page.get_by_role("radio", name=re.compile(re.escape(label[:40]), re.I)).first.click(timeout=4000)
        human_pause()
        return True
    except Exception:
        pass
    try:
        page.locator(f"label:has-text('{label[:60]}')").first.click(timeout=4000)
        human_pause()
        return True
    except Exception as exc:
        log.warning("Could not select meal %r: %s", label, exc)
        return False


def enrich_day_from_page(page: Page, day: CalendarDay) -> CalendarDay:
    options_raw = discover_meal_options(page)
    labels = [o["label"] for o in options_raw if o.get("label")]
    day.meal_options = labels
    day.meal_options_visible = len(labels) > 0
    day.selected_option = get_selected_meal_label(options_raw)
    day.already_selected = day.already_selected or day.selected_option is not None
    return day


def is_selectable_weekday(day: CalendarDay) -> bool:
    if day.is_weekend or WEEKEND_PATTERN.search(day.label):
        day.skip_reason = day.skip_reason or "weekend"
        return False
    if not day.enabled:
        day.skip_reason = day.skip_reason or "disabled"
        return False
    return True


def log_day_summary(day: CalendarDay) -> None:
    log.info(
        "Day %r enabled=%s weekend=%s already_selected=%s meal_options_visible=%s "
        "selected=%r options=%s skip=%s",
        day.label,
        day.enabled,
        day.is_weekend,
        day.already_selected,
        day.meal_options_visible,
        day.selected_option,
        day.meal_options,
        day.skip_reason,
    )


def navigate_to_order_page(page: Page, run_dir: Path) -> bool:
    log.info("Step: loading homepage")
    page.goto(START_URL, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
    human_pause()

    if not page_looks_logged_in(page) or page_has_login_form(page):
        log.error("Not logged in — run probe_simplyfresh.py --manual-login first")
        return False

    log.info("Step: clicking PLACE ORDER")
    class _Artifacts:
        def capture(self, pg: Page, label: str) -> None:
            capture_named(pg, run_dir, safe_filename(label), PROBE_DIR, log)

    if not click_safe_link(page, ORDER_LINK_PATTERNS, _Artifacts(), "place_order"):
        for candidate in find_clickable_candidates(page):
            haystack = f"{candidate['text']} {candidate['href']}"
            if any(p.search(haystack) for p in ORDER_LINK_PATTERNS):
                if is_forbidden_control_text(candidate["text"]):
                    continue
                try:
                    page.get_by_role(
                        "link",
                        name=re.compile(re.escape(candidate["text"][:30]), re.I),
                    ).first.click(timeout=5000)
                    human_pause()
                    break
                except Exception:
                    continue
        else:
            log.error("Could not reach PLACE ORDER page")
            return False

    human_pause()
    return True


def build_recommendation(report: MealSelectionReport, mode: str) -> str:
    if not report.logged_in:
        return "Refresh session with probe_simplyfresh.py --manual-login, then rerun."
    if not report.order_page_reached:
        return "Inspect before_order_page artifacts and fix PLACE ORDER navigation."
    if report.forbidden_controls_detected:
        return "Review forbidden controls in artifacts; keep dry-run stops before those buttons."
    if report.autosave_risk_detected:
        return "Autosave may persist selections — review first day artifacts before expanding --max-days."
    if mode == "inspect-only":
        return "Review calendar_map.json and HTML snapshots; then run --dry-run-select --max-days 3."
    if report.uncertain_days > 0:
        return "Harden meal label patterns for uncertain days using each_day_* artifacts."
    if report.days_selected > 0:
        return "Dry-run selections succeeded; manually verify cart/order state was not finalized."
    return "No selections made; inspect calendar_map.json for day/meal selector updates."


def print_report(report: MealSelectionReport) -> None:
    forbidden = report.forbidden_controls_detected or ["none"]
    print("\n" + "=" * 60)
    print("SIMPLY FRESH KITCHEN MEAL SELECTION REPORT")
    print("=" * 60)
    print(f"logged_in: {str(report.logged_in).lower()}")
    print(f"order_page_reached: {str(report.order_page_reached).lower()}")
    print(f"month_detected: {report.month_detected or 'unknown'}")
    print(f"days_detected: {report.days_detected}")
    print(f"selectable_days_detected: {report.selectable_days_detected}")
    print(f"days_attempted: {report.days_attempted}")
    print(f"days_selected: {report.days_selected}")
    print(f"days_skipped: {report.days_skipped}")
    print(f"uncertain_days: {report.uncertain_days}")
    print(f"vegetarian_options_detected: {report.vegetarian_options_detected}")
    print(f"non_vegetarian_options_detected: {report.non_vegetarian_options_detected}")
    print(f"forbidden_controls_detected: {','.join(forbidden)}")
    print(f"autosave_risk_detected: {str(report.autosave_risk_detected).lower()}")
    print(f"recommended_next_step: {report.recommended_next_step}")
    print("=" * 60 + "\n")


def run_meal_probe(
    inspect_only: bool,
    dry_run_select: bool,
    max_days: int,
    headed: bool,
    continue_after_autosave: bool,
) -> MealSelectionReport:
    run_id = new_run_id()
    run_dir = PROBE_DIR / "artifacts" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    report = MealSelectionReport()
    mode = "inspect-only" if inspect_only else "dry-run-select"

    storage_state = load_storage_state_path()
    if not storage_state:
        log.error("Missing storage state at %s", STORAGE_STATE_PATH)
        report.recommended_next_step = "Run probe_simplyfresh.py --manual-login first."
        print_report(report)
        return report

    log.info("Run ID: %s mode=%s max_days=%s", run_id, mode, max_days)
    log.info("Headed: %s", headed)

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=not headed)
        context = browser.new_context(
            viewport=VIEWPORT,
            user_agent=USER_AGENT,
            locale=LOCALE,
            timezone_id=TIMEZONE,
            storage_state=storage_state,
        )
        page = context.new_page()
        page.set_default_timeout(NAV_TIMEOUT_MS)

        try:
            if not navigate_to_order_page(page, run_dir):
                capture_named(page, run_dir, "before_order_page_failed", PROBE_DIR, log)
                print_report(report)
                return report

            report.logged_in = True
            report.order_page_reached = True
            capture_named(page, run_dir, "before_order_page", PROBE_DIR, log)

            forbidden = find_forbidden_controls(page)
            if forbidden:
                report.forbidden_controls_detected = [f["text"] for f in forbidden]
                log.warning("Forbidden controls visible (will not click): %s", report.forbidden_controls_detected)
                capture_named(page, run_dir, "forbidden_controls_visible", PROBE_DIR, log)

            report.month_detected = detect_month_label(page)
            days = discover_calendar_days(page)
            report.calendar_days = days
            report.days_detected = len(days)

            selectable = [d for d in days if is_selectable_weekday(d)]
            report.selectable_days_detected = len(selectable)

            calendar_map_path = run_dir / "calendar_map.json"
            calendar_map_path.write_text(
                json.dumps(
                    {
                        "month_detected": report.month_detected,
                        "days": [asdict(d) for d in days],
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
            log.info("Wrote %s", calendar_map_path.relative_to(PROBE_DIR))

            for day in days:
                log_day_summary(day)

            if not inspect_only and not dry_run_select:
                inspect_only = True

            attempted = 0
            for day in selectable:
                if dry_run_select and attempted >= max_days:
                    break

                day_slug = safe_filename(day.day_id or day.label)[:40]
                capture_named(page, run_dir, f"each_day_before_{day_slug}", PROBE_DIR, log)

                if inspect_only:
                    # Inspect-only: map day metadata only; do not open days or click meals.
                    log_day_summary(day)
                    continue

                # dry-run-select
                report.days_attempted += 1
                attempted += 1
                html_before = page.content()

                if not click_calendar_day(page, day):
                    day.skip_reason = "could_not_open_day"
                    report.days_skipped += 1
                    report.day_results.append({"day": day.label, "status": "skipped", "reason": day.skip_reason})
                    continue

                day = enrich_day_from_page(page, day)
                for label in day.meal_options:
                    cls = classify_meal_option(label)
                    if cls == "vegetarian":
                        report.vegetarian_options_detected += 1
                    elif cls == "non_vegetarian":
                        report.non_vegetarian_options_detected += 1

                if not day.meal_options:
                    day.skip_reason = "no_meal_options"
                    report.days_skipped += 1
                    report.day_results.append({"day": day.label, "status": "skipped", "reason": day.skip_reason})
                    continue

                choice: MealChoiceResult = choose_non_vegetarian_option(day.meal_options)
                if choice.selected is None:
                    log.warning("UNCERTAIN_MEAL_SKIPPED day=%r options=%s", day.label, day.meal_options)
                    report.uncertain_days += 1
                    report.days_skipped += 1
                    report.day_results.append(
                        {
                            "day": day.label,
                            "status": "uncertain",
                            "reason": choice.reason,
                            "options": day.meal_options,
                        }
                    )
                    continue

                if not click_meal_option(page, choice.selected):
                    report.days_skipped += 1
                    report.day_results.append(
                        {"day": day.label, "status": "skipped", "reason": "click_failed", "target": choice.selected}
                    )
                    continue

                human_pause()
                if detect_autosave_markers(page, html_before):
                    log.warning("AUTOSAVE_RISK_DETECTED after selecting day=%r", day.label)
                    report.autosave_risk_detected = True
                    capture_named(page, run_dir, f"autosave_risk_{day_slug}", PROBE_DIR, log)
                    report.day_results.append(
                        {"day": day.label, "status": "selected", "meal": choice.selected, "autosave_risk": True}
                    )
                    report.days_selected += 1
                    capture_named(page, run_dir, f"each_day_after_{day_slug}", PROBE_DIR, log)
                    if not continue_after_autosave:
                        log.warning("Stopping after AUTOSAVE_RISK_DETECTED (use --continue-after-autosave to proceed)")
                        break
                    continue

                day = enrich_day_from_page(page, day)
                report.days_selected += 1
                report.day_results.append(
                    {"day": day.label, "status": "selected", "meal": choice.selected, "reason": choice.reason}
                )
                capture_named(page, run_dir, f"each_day_after_{day_slug}", PROBE_DIR, log)
                log.info("Selected %r for day %r", choice.selected, day.label)

            # Re-scan forbidden controls after interactions
            post_forbidden = find_forbidden_controls(page)
            for text in (f["text"] for f in post_forbidden):
                if text not in report.forbidden_controls_detected:
                    report.forbidden_controls_detected.append(text)

        except PlaywrightTimeout as exc:
            log.error("Timeout: %s", exc)
            capture_named(page, run_dir, "timeout", PROBE_DIR, log)
        except Exception as exc:
            log.error("Probe failed: %s", exc)
            capture_named(page, run_dir, "error", PROBE_DIR, log)
        finally:
            report_path = run_dir / "meal_selection_report.json"
            report.recommended_next_step = build_recommendation(report, mode)
            report_path.write_text(
                json.dumps(
                    {
                        "logged_in": report.logged_in,
                        "order_page_reached": report.order_page_reached,
                        "month_detected": report.month_detected,
                        "days_detected": report.days_detected,
                        "selectable_days_detected": report.selectable_days_detected,
                        "days_attempted": report.days_attempted,
                        "days_selected": report.days_selected,
                        "days_skipped": report.days_skipped,
                        "uncertain_days": report.uncertain_days,
                        "vegetarian_options_detected": report.vegetarian_options_detected,
                        "non_vegetarian_options_detected": report.non_vegetarian_options_detected,
                        "forbidden_controls_detected": report.forbidden_controls_detected,
                        "autosave_risk_detected": report.autosave_risk_detected,
                        "recommended_next_step": report.recommended_next_step,
                        "day_results": report.day_results,
                        "calendar_days": [asdict(d) for d in report.calendar_days],
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
            log.info("Wrote %s", report_path.relative_to(PROBE_DIR))
            context.close()
            browser.close()

    print_report(report)
    return report


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    if not args.inspect_only and not args.dry_run_select:
        args.inspect_only = True
        log.info("No mode specified; defaulting to --inspect-only")

    if args.max_days < 1:
        log.error("--max-days must be >= 1")
        return 2

    report = run_meal_probe(
        inspect_only=args.inspect_only,
        dry_run_select=args.dry_run_select,
        max_days=args.max_days,
        headed=resolve_headed(args),
        continue_after_autosave=args.continue_after_autosave,
    )
    if not report.logged_in or not report.order_page_reached:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
