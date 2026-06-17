"""
Simply Fresh Kitchen meal-selection dry-run probe
=================================================
Navigates to the order calendar and optionally selects non-vegetarian
meal options for up to N weekdays. Does NOT submit, save order, checkout,
pay, confirm, or finalize anything.

Usage:
    python3 experiments/simplyfresh_probe/probe_meal_selection.py --inspect-only --headless
    python3 experiments/simplyfresh_probe/probe_meal_selection.py --dry-run-select --max-days 3 --headless
    python3 experiments/simplyfresh_probe/probe_meal_selection.py --inspect-only --headless \\
        --profile-name "Vincent Bergeron" --school "MEADOW MONTESSORI SCHOOL"
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

# Allow imports from probe package directory when run as a script.
_PROBE_DIR = Path(__file__).resolve().parent
if str(_PROBE_DIR) not in sys.path:
    sys.path.insert(0, str(_PROBE_DIR))

from meal_classification import MealChoiceResult, choose_non_vegetarian_option, classify_meal_option
from probe_common import (
    NAV_TIMEOUT_MS,
    PROBE_DIR,
    ProfileConfig,
    TIMEZONE,
    USER_AGENT,
    VIEWPORT,
    capture_named,
    detect_autosave_markers,
    detect_month_label,
    discover_meal_card_labels,
    ensure_profile_chooser_overlay_closed,
    find_forbidden_controls,
    human_pause,
    is_forbidden_control_text,
    is_meal_calendar_page,
    load_storage_state_path,
    navigate_to_meal_calendar,
    new_run_id,
    profile_chooser_overlay_open,
    resolve_auth_storage_path,
    safe_filename,
    save_step_debug,
    setup_logging,
)

try:
    from playwright.sync_api import Page, TimeoutError as PlaywrightTimeout, sync_playwright
except ImportError:
    print("ERROR: playwright is not installed.")
    print("  .venv/bin/pip install -r experiments/simplyfresh_probe/requirements.txt")
    print("  .venv/bin/playwright install chromium")
    sys.exit(1)

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
    profile_chooser_seen: bool = False
    profile_selected: bool = False
    meal_calendar_reached: bool = False
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
    parser.add_argument(
        "--profile-name",
        default=None,
        help='Child profile name on checkout chooser (e.g. "Vincent Bergeron")',
    )
    parser.add_argument(
        "--school",
        default=None,
        help='School name on checkout chooser (e.g. "MEADOW MONTESSORI SCHOOL")',
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


def profile_config_from_args(args: argparse.Namespace) -> ProfileConfig:
    return ProfileConfig(profile_name=args.profile_name, school=args.school)


def discover_calendar_days(page: Page) -> list[CalendarDay]:
    """Find day circle buttons on the meal calendar (Choose your meals)."""
    raw: list[dict[str, Any]] = page.evaluate(
        """() => {
        const results = [];
        const seen = new Set();
        const inCalendar = document.body.innerText.includes('Choose your meals') ||
          /\\b(January|February|March|April|May|June|July|August|September|October|November|December)\\s+\\d{1,2},\\s+\\d{4}\\b/i.test(document.body.innerText);

        const candidates = document.querySelectorAll(
          'button, a, [role="button"], [role="gridcell"], span, div'
        );
        for (const el of candidates) {
          const text = (el.innerText || el.textContent || '').trim();
          if (!/^\\d{1,2}$/.test(text)) continue;
          const aria = (el.getAttribute('aria-label') || '').trim();
          const dataDate = el.getAttribute('data-date') || el.getAttribute('data-day') || '';
          const label = aria || text;
          const parentText = (el.closest('[class*="calendar" i], [role="grid"], section, div')?.innerText || '').slice(0, 200);
          if (!inCalendar && !dataDate && !parentText) continue;
          const key = dataDate || label;
          if (seen.has(key)) continue;
          seen.add(key);
          const disabled = el.hasAttribute('disabled') ||
            el.getAttribute('aria-disabled') === 'true' ||
            (el.className || '').toLowerCase().includes('disabled');
          const selected = el.getAttribute('aria-selected') === 'true' ||
            el.getAttribute('aria-current') === 'date' ||
            (el.className || '').toLowerCase().includes('selected') ||
            (el.className || '').toLowerCase().includes('active');
          results.push({
            day_id: dataDate || text,
            label: label,
            enabled: !disabled,
            is_weekend: /\\b(sat|sun|saturday|sunday)\\b/i.test(label + ' ' + parentText),
            already_selected: selected,
            meal_options_visible: false,
            selected_option: null,
            meal_options: [],
          });
          if (results.length >= 31) break;
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
    """Return visible meal card titles/descriptions and coarse selection state."""
    card_labels = discover_meal_card_labels(page)
    if card_labels:
        return [{"label": label, "checked": "false"} for label in card_labels]

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
          if (!label || label.length > 160 || seen.has(label)) continue;
          seen.add(label);
          const checked = input.checked || input.getAttribute('aria-checked') === 'true';
          options.push({ label, checked: checked ? 'true' : 'false' });
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
    overlay = ensure_profile_chooser_overlay_closed(page, log)
    if not overlay.closed:
        log.error(
            "Cannot click day %r — profile chooser overlay still open (active_count=%d)",
            day.label,
            overlay.active_count_after,
        )
        return False
    if profile_chooser_overlay_open(page):
        log.error("Cannot click day %r — Chooser__options--active still visible", day.label)
        return False

    label = day.label
    try:
        page.get_by_role("button", name=re.compile(rf"^{re.escape(label)}$", re.I)).first.click(timeout=4000)
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
        page.get_by_text(label, exact=False).first.click(timeout=4000)
        human_pause()
        return True
    except Exception:
        pass
    try:
        page.get_by_label(label, exact=False).first.click(timeout=4000)
        human_pause()
        return True
    except Exception:
        pass
    try:
        page.get_by_role("radio", name=re.compile(re.escape(label[:50]), re.I)).first.click(timeout=4000)
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


def build_recommendation(report: MealSelectionReport, mode: str) -> str:
    if not report.logged_in:
        return "Refresh session with probe_simplyfresh.py --manual-login, then rerun."
    if not report.order_page_reached:
        return "Inspect after_order_now / after_select_profile artifacts; fix Order Now navigation."
    if not report.meal_calendar_reached:
        return "Profile/calendar step failed — check after_select_profile and visible_text.txt."
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
    print(f"profile_chooser_seen: {str(report.profile_chooser_seen).lower()}")
    print(f"profile_selected: {str(report.profile_selected).lower()}")
    print(f"meal_calendar_reached: {str(report.meal_calendar_reached).lower()}")
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
    profile_config: ProfileConfig,
) -> MealSelectionReport:
    run_id = new_run_id()
    run_dir = PROBE_DIR / "artifacts" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    report = MealSelectionReport()
    mode = "inspect-only" if inspect_only else "dry-run-select"

    storage_state = load_storage_state_path(log)
    if not storage_state:
        auth_path = resolve_auth_storage_path()
        log.error("Missing storage state at %s", auth_path)
        report.recommended_next_step = "Run probe_simplyfresh.py --manual-login first."
        print_report(report)
        return report

    log.info("Run ID: %s mode=%s max_days=%s", run_id, mode, max_days)
    log.info(
        "Profile filters: name=%r school=%r",
        profile_config.profile_name,
        profile_config.school,
    )
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
            nav = navigate_to_meal_calendar(page, run_dir, profile_config, log)
            if not nav.ok:
                capture_named(page, run_dir, "navigation_failed", PROBE_DIR, log)
                save_step_debug(page, run_dir, "navigation_failed", PROBE_DIR, log)
                if nav.login_required:
                    report.recommended_next_step = (
                        "Session expired or login required — run "
                        "probe_simplyfresh.py --manual-login to refresh storage state."
                    )
                print_report(report)
                return report

            report.logged_in = True
            report.order_page_reached = True
            report.profile_chooser_seen = nav.profile_chooser_seen
            report.profile_selected = nav.profile_selected
            report.meal_calendar_reached = nav.meal_calendar_reached
            capture_named(page, run_dir, "before_order_page", PROBE_DIR, log)
            save_step_debug(page, run_dir, "before_calendar", PROBE_DIR, log)

            forbidden = find_forbidden_controls(page)
            if forbidden:
                report.forbidden_controls_detected = [f["text"] for f in forbidden]
                log.warning("Forbidden controls visible (will not click): %s", report.forbidden_controls_detected)
                capture_named(page, run_dir, "forbidden_controls_visible", PROBE_DIR, log)

            report.month_detected = detect_month_label(page)
            days = discover_calendar_days(page)
            report.calendar_days = days
            report.days_detected = len(days)

            # If day circles not found, still capture visible meal cards on current date.
            if not days and report.meal_calendar_reached:
                labels = discover_meal_card_labels(page)
                if labels:
                    log.info("Meal cards on current date: %s", labels)
                    pseudo = CalendarDay(
                        day_id="current",
                        label=report.month_detected or "current",
                        enabled=True,
                        is_weekend=False,
                        already_selected=False,
                        meal_options_visible=True,
                        meal_options=labels,
                    )
                    days = [pseudo]
                    report.calendar_days = days
                    report.days_detected = 1

            selectable = [d for d in days if is_selectable_weekday(d)]
            report.selectable_days_detected = len(selectable)

            calendar_map_path = run_dir / "calendar_map.json"
            calendar_map_path.write_text(
                json.dumps(
                    {
                        "month_detected": report.month_detected,
                        "meal_cards_visible": discover_meal_card_labels(page),
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
                    if day.meal_options_visible or not day.meal_options:
                        day = enrich_day_from_page(page, day)
                        log_day_summary(day)
                    continue

                report.days_attempted += 1
                attempted += 1
                html_before = page.content()

                if day.day_id != "current" and not click_calendar_day(page, day):
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

            post_forbidden = find_forbidden_controls(page)
            for text in (f["text"] for f in post_forbidden):
                if text not in report.forbidden_controls_detected:
                    report.forbidden_controls_detected.append(text)

        except PlaywrightTimeout as exc:
            log.error("Timeout: %s", exc)
            capture_named(page, run_dir, "timeout", PROBE_DIR, log)
            save_step_debug(page, run_dir, "timeout", PROBE_DIR, log)
        except Exception as exc:
            log.error("Probe failed: %s", exc)
            capture_named(page, run_dir, "error", PROBE_DIR, log)
            save_step_debug(page, run_dir, "error", PROBE_DIR, log)
        finally:
            report_path = run_dir / "meal_selection_report.json"
            report.recommended_next_step = build_recommendation(report, mode)
            report_path.write_text(
                json.dumps(
                    {
                        "logged_in": report.logged_in,
                        "order_page_reached": report.order_page_reached,
                        "profile_chooser_seen": report.profile_chooser_seen,
                        "profile_selected": report.profile_selected,
                        "meal_calendar_reached": report.meal_calendar_reached,
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
        profile_config=profile_config_from_args(args),
    )
    if not report.logged_in or not report.order_page_reached or not report.meal_calendar_reached:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
