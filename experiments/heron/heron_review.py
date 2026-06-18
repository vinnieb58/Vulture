"""
Heron interactive review — confirm or edit extracted expense JSON.

Usage:
  python experiments/heron/heron_review.py --file /mnt/pelican_backup/Heron/reviewed/example.json
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from heron_schema import (
    ExpenseCandidate,
    STATUS_REVIEWED,
    guess_cost_code,
    load_candidate,
    save_candidate,
)

EDITABLE_FIELDS = [
    "vendor",
    "transaction_date",
    "total_amount",
    "tax_amount",
    "tip_amount",
    "category_guess",
    "cost_code_guess",
    "business_purpose_guess",
    "notes",
]


def prompt_field(label: str, current: str | None) -> str | None:
    display = current if current is not None else ""
    raw = input(f"{label} [{display}]: ").strip()
    if raw == "":
        return current
    if raw.lower() in {"null", "none", "-"}:
        return None
    return raw


def review_candidate(candidate: ExpenseCandidate, *, interactive: bool = True) -> ExpenseCandidate:
    print("\n--- Heron expense review ---")
    print(f"Source file: {candidate.source_file}")
    print(f"Status: {candidate.status} | confidence: {candidate.confidence} | needs_review: {candidate.needs_review}")
    if candidate.notes:
        print(f"Notes: {candidate.notes}")

    if not interactive:
        candidate.status = STATUS_REVIEWED
        candidate.needs_review = False
        return candidate

    for field_name in EDITABLE_FIELDS:
        current = getattr(candidate, field_name)
        updated = prompt_field(field_name, current)
        setattr(candidate, field_name, updated)

    if candidate.category_guess and not candidate.cost_code_guess:
        candidate.cost_code_guess = guess_cost_code(candidate.category_guess)

    confirm = input("\nMark as reviewed? [y/N]: ").strip().lower()
    if confirm not in {"y", "yes"}:
        print("Review cancelled — no changes saved.")
        return candidate

    candidate.status = STATUS_REVIEWED
    candidate.needs_review = False
    return candidate


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Heron CLI review for one expense JSON file")
    parser.add_argument("--file", type=Path, required=True, help="Path to reviewed candidate JSON")
    parser.add_argument(
        "--auto-approve",
        action="store_true",
        help="Mark reviewed without prompts (for tests/automation only).",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    path = args.file.expanduser().resolve()
    if not path.is_file():
        print(f"File not found: {path}", file=sys.stderr)
        return 1

    candidate = load_candidate(path)
    updated = review_candidate(candidate, interactive=not args.auto_approve)
    if updated.status != STATUS_REVIEWED:
        return 2

    save_candidate(path, updated)
    print(f"Saved reviewed expense: {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
