"""Tests for safe Simply Fresh modal dismiss heuristics."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "experiments" / "simplyfresh_probe"))

from probe_common import is_safe_modal_close_aria, is_safe_modal_close_text


def test_safe_modal_close_allows_dismiss_controls():
    assert is_safe_modal_close_text("Close")
    assert is_safe_modal_close_text("Cancel")
    assert is_safe_modal_close_text("Back")
    assert is_safe_modal_close_text("x")
    assert is_safe_modal_close_aria("Close dialog")


def test_safe_modal_close_rejects_submit_and_pay():
    assert not is_safe_modal_close_text("Submit")
    assert not is_safe_modal_close_text("Checkout")
    assert not is_safe_modal_close_text("Pay Now")
    assert not is_safe_modal_close_text("Confirm Order")
    assert not is_safe_modal_close_aria("Continue to payment")


def test_modal_add_button_allowed_only_for_add():
    from probe_common import is_modal_add_button_text

    assert is_modal_add_button_text("Add")
    assert is_modal_add_button_text("add")
    assert not is_modal_add_button_text("Add to cart checkout")
    assert not is_modal_add_button_text("Submit")
    assert not is_modal_add_button_text("Pay")
    assert not is_modal_add_button_text("Confirm")
