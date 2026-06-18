"""Tests for Heron Atomiq safety controls."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
HERON_DIR = REPO_ROOT / "experiments" / "heron"
sys.path.insert(0, str(HERON_DIR))

from heron_atomiq_draft import build_draft_plan
from heron_schema import (
    STATUS_EXTRACTED,
    STATUS_REVIEWED,
    SafetyError,
    assert_safe_control_text,
    is_forbidden_control_text,
    save_candidate,
)


class TestForbiddenControlDetection:
    @pytest.mark.parametrize(
        "text",
        [
            "Submit Report",
            "Finalize Expense",
            "Approve",
            "Certify Report",
            "Send for Approval",
            "Complete Report",
            "Reimbursement Submit",
            "Pay Now",
            "Checkout",
        ],
    )
    def test_forbidden_controls_detected(self, text: str) -> None:
        assert is_forbidden_control_text(text)

    @pytest.mark.parametrize(
        "text",
        [
            "Save Draft",
            "Add Expense Line",
            "Upload Receipt",
            "Create New",
            "My Expenses",
            "Browse",
            "Cancel",
        ],
    )
    def test_safe_controls_allowed(self, text: str) -> None:
        assert not is_forbidden_control_text(text)

    def test_assert_safe_control_raises(self) -> None:
        with pytest.raises(SafetyError, match="forbidden"):
            assert_safe_control_text("Submit for approval")

    def test_empty_text_not_forbidden(self) -> None:
        assert not is_forbidden_control_text("")
        assert not is_forbidden_control_text("   ")


class TestDraftGates:
    def test_unreviewed_expense_blocked(self, tmp_path: Path) -> None:
        from heron_schema import ExpenseCandidate

        expense = tmp_path / "pending.json"
        save_candidate(
            expense,
            ExpenseCandidate(status=STATUS_EXTRACTED, source_file="/tmp/receipt.pdf"),
        )
        with pytest.raises(SafetyError, match="only reviewed"):
            build_draft_plan(expense)

    def test_reviewed_expense_allowed(self, tmp_path: Path) -> None:
        from heron_schema import ExpenseCandidate

        expense = tmp_path / "reviewed.json"
        save_candidate(
            expense,
            ExpenseCandidate(
                status=STATUS_REVIEWED,
                vendor="Test Vendor",
                transaction_date="2026-06-01",
                total_amount="10.00",
                source_file=str(tmp_path / "receipt.pdf"),
            ),
        )
        plan = build_draft_plan(expense)
        assert plan.vendor == "Test Vendor"
        assert any("draft" in action.lower() for action in plan.actions)
