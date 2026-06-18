"""
Heron Atomiq draft entry — create draft expense lines only (never submit/approve).

Usage:
  python experiments/heron/heron_atomiq_draft.py --expense /mnt/pelican_backup/Heron/reviewed/example.json --dry-run --headed
  python experiments/heron/heron_atomiq_draft.py --expense /mnt/pelican_backup/Heron/reviewed/example.json --live --headed
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from heron_schema import (
    ATOMIQ_URL,
    LOCALE,
    NAV_TIMEOUT_MS,
    SafetyError,
    TIMEZONE,
    USER_AGENT,
    VIEWPORT,
    assert_safe_control_text,
    can_proceed_to_atomiq,
    is_forbidden_control_text,
    load_candidate,
    resolve_storage_state_path,
)

if TYPE_CHECKING:
    from playwright.sync_api import Page

try:
    from playwright.sync_api import Page as _Page, sync_playwright
except ImportError:
    _Page = Any  # type: ignore[misc, assignment]
    sync_playwright = None  # type: ignore[assignment]


@dataclass
class DraftPlan:
    expense_file: Path
    vendor: str | None
    transaction_date: str | None
    total_amount: str | None
    category_guess: str | None
    cost_code_guess: str | None
    business_purpose_guess: str | None
    receipt_path: str | None
    actions: list[str] = field(default_factory=list)
    blocked_controls: list[str] = field(default_factory=list)


def setup_logging() -> logging.Logger:
    logging.basicConfig(level=logging.INFO, format="%(levelname)-8s %(message)s", stream=sys.stdout)
    return logging.getLogger("heron.atomiq_draft")


def find_forbidden_controls(page: _Page) -> list[str]:
    found: list[str] = []
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
            if text and is_forbidden_control_text(text):
                found.append(text)
        except Exception:
            continue
    return found


def refuse_if_forbidden_controls(page: _Page) -> None:
    forbidden = find_forbidden_controls(page)
    if forbidden:
        samples = ", ".join(repr(t) for t in forbidden[:5])
        raise SafetyError(f"Forbidden controls visible on page — refusing to continue: {samples}")


def build_draft_plan(expense_path: Path) -> DraftPlan:
    candidate = load_candidate(expense_path)
    if not can_proceed_to_atomiq(candidate):
        raise SafetyError(
            f"Expense status is {candidate.status!r} — only reviewed expenses can be drafted. "
            "Run heron_review.py first."
        )
    plan = DraftPlan(
        expense_file=expense_path,
        vendor=candidate.vendor,
        transaction_date=candidate.transaction_date,
        total_amount=candidate.total_amount,
        category_guess=candidate.category_guess,
        cost_code_guess=candidate.cost_code_guess,
        business_purpose_guess=candidate.business_purpose_guess,
        receipt_path=candidate.source_file,
    )
    plan.actions = [
        "Open Atomiq My Expenses",
        "Create New report (TVL | TRAVEL Card - PAY BANK)",
        "Set Business Division: Engineering",
        "Set Business Unit: Engineering Office BR",
        "Add expense line (draft only)",
        f"Expense Date: {candidate.transaction_date}",
        f"Expense Category: {candidate.category_guess}",
        f"Expense Amount: {candidate.total_amount}",
        f"Vendor Name: {candidate.vendor}",
        "Original Receipt: Y",
        f"Detailed Business Purpose: {candidate.business_purpose_guess}",
        f"Cost Type / Cost Code: {candidate.cost_code_guess}",
        f"Upload receipt: {candidate.source_file}",
        "Save draft — stop before submit/approve/certify",
    ]
    return plan


def print_plan_summary(plan: DraftPlan, *, live: bool) -> None:
    mode = "LIVE" if live else "DRY-RUN"
    print(f"\n=== Heron Atomiq draft ({mode}) ===")
    print(f"Expense file: {plan.expense_file}")
    for action in plan.actions:
        print(f"  - {action}")
    if plan.blocked_controls:
        print("Blocked forbidden controls:")
        for text in plan.blocked_controls:
            print(f"  ! {text}")


def fill_field_if_present(page: _Page, label_pattern: str, value: str | None, log: logging.Logger) -> bool:
    if not value:
        return False
    loc = page.get_by_label(label_pattern, exact=False)
    if loc.count() == 0:
        loc = page.locator(f"input[placeholder*='{label_pattern}' i], textarea[placeholder*='{label_pattern}' i]")
    if loc.count() == 0:
        log.info("Field not found for %r — skipping", label_pattern)
        return False
    field = loc.first
    field.fill(value)
    log.info("Filled %r -> %r", label_pattern, value)
    return True


def click_safe_control(page: _Page, pattern: str, log: logging.Logger) -> bool:
    loc = page.get_by_role("button", name=pattern)
    if loc.count() == 0:
        loc = page.get_by_role("link", name=pattern)
    if loc.count() == 0:
        return False
    text = loc.first.inner_text(timeout=500).strip()
    assert_safe_control_text(text, context="button")
    loc.first.click(timeout=5000)
    log.info("Clicked safe control: %r", text)
    time.sleep(1.0)
    return True


def run_live_draft(page: _Page, plan: DraftPlan, log: logging.Logger) -> None:
    refuse_if_forbidden_controls(page)

    click_safe_control(page, "My Expenses", log)
    refuse_if_forbidden_controls(page)

    if not click_safe_control(page, "Create New", log):
        click_safe_control(page, "Create", log)
    refuse_if_forbidden_controls(page)

    fill_field_if_present(page, "Expense Date", plan.transaction_date, log)
    fill_field_if_present(page, "Vendor", plan.vendor, log)
    fill_field_if_present(page, "Expense Amount", plan.total_amount, log)
    fill_field_if_present(page, "Amount", plan.total_amount, log)
    fill_field_if_present(page, "Business Purpose", plan.business_purpose_guess, log)
    fill_field_if_present(page, "Cost Code", plan.cost_code_guess, log)

    if plan.receipt_path and Path(plan.receipt_path).is_file():
        upload = page.locator("input[type='file']")
        if upload.count() > 0:
            upload.first.set_input_files(plan.receipt_path)
            log.info("Uploaded receipt: %s", plan.receipt_path)
        else:
            log.warning("Receipt file exists but no file input found on page")

    refuse_if_forbidden_controls(page)
    log.info("Draft entry steps applied — stopping before any submit/approve/certify control.")


def run_draft(
    expense_path: Path,
    *,
    live: bool,
    headed: bool,
    use_session: bool,
    wait_seconds: int,
) -> int:
    log = setup_logging()
    plan = build_draft_plan(expense_path)
    print_plan_summary(plan, live=live)

    if not live:
        log.info("Dry-run complete — pass --live to perform browser actions.")
        return 0

    if sync_playwright is None:
        print(
            "Playwright is required for --live. Install with: pip install playwright && playwright install chromium",
            file=sys.stderr,
        )
        return 1

    storage_path = resolve_storage_state_path()
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=not headed)
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
            log.warning("No session file — manual login required")

        context = browser.new_context(**context_kwargs)
        page = context.new_page()
        page.set_default_timeout(NAV_TIMEOUT_MS)

        page.goto(ATOMIQ_URL, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
        log.info("Waiting %d seconds for manual login if needed...", wait_seconds)
        time.sleep(wait_seconds)

        run_live_draft(page, plan, log)

        context.close()
        browser.close()

    print("\nDraft summary: expense line prepared in Atomiq; no submit/approve/certify action taken.")
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Heron Atomiq draft expense entry (default dry-run; --live required to act)."
    )
    parser.add_argument("--expense", type=Path, required=True, help="Reviewed expense JSON file")
    parser.add_argument("--live", action="store_true", help="Perform browser draft actions.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Plan only without browser changes (default when --live is omitted).",
    )
    parser.add_argument("--headed", action="store_true", help="Run Chromium in headed mode.")
    parser.add_argument("--use-session", action="store_true", help="Load saved Atomiq session state.")
    parser.add_argument(
        "--wait-seconds",
        type=int,
        default=90,
        help="Seconds to wait for manual login before drafting (default: 90).",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.live and args.dry_run:
        print("Cannot use --live and --dry-run together.", file=sys.stderr)
        return 2
    live = bool(args.live)
    try:
        return run_draft(
            args.expense.expanduser().resolve(),
            live=live,
            headed=args.headed,
            use_session=args.use_session,
            wait_seconds=args.wait_seconds,
        )
    except SafetyError as exc:
        print(f"SAFETY BLOCK: {exc}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    sys.exit(main())
