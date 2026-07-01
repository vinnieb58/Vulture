"""Tests for Heron expense candidate schema."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
HERON_DIR = REPO_ROOT / "experiments" / "heron"
sys.path.insert(0, str(HERON_DIR))

from heron_extract import extract_from_file, run_extraction
from heron_schema import (
    STATUS_EXTRACTED,
    STATUS_REVIEWED,
    ExpenseCandidate,
    can_proceed_to_atomiq,
    ensure_heron_dirs,
    guess_cost_code,
    is_reviewed,
    load_candidate,
    save_candidate,
)


class TestExpenseCandidate:
    def test_round_trip_json(self, tmp_path: Path) -> None:
        candidate = ExpenseCandidate(
            vendor="Corner Cafe",
            transaction_date="2026-06-01",
            total_amount="42.50",
            category_guess="meals with tip",
            cost_code_guess="402200",
            source_file="/mnt/pelican_backup/Heron/inbox/receipt.pdf",
            confidence=0.85,
            needs_review=False,
            status=STATUS_REVIEWED,
        )
        path = tmp_path / "candidate.json"
        save_candidate(path, candidate)
        loaded = load_candidate(path)
        assert loaded.vendor == "Corner Cafe"
        assert loaded.cost_code_guess == "402200"
        assert loaded.status == STATUS_REVIEWED

    def test_unknown_fields_preserved_in_extra(self) -> None:
        data = {"vendor": "Test", "status": STATUS_EXTRACTED, "custom_flag": True}
        candidate = ExpenseCandidate.from_dict(data)
        assert candidate.vendor == "Test"
        assert candidate.extra.get("custom_flag") is True

    def test_guess_cost_code_meals(self) -> None:
        assert guess_cost_code("meals with tip") == "402200"
        assert guess_cost_code("Meals & Entertainment / Meals") == "402200"
        assert guess_cost_code("parking") is None

    def test_review_gate(self) -> None:
        extracted = ExpenseCandidate(status=STATUS_EXTRACTED)
        reviewed = ExpenseCandidate(status=STATUS_REVIEWED)
        assert not is_reviewed(extracted)
        assert is_reviewed(reviewed)
        assert not can_proceed_to_atomiq(extracted)
        assert can_proceed_to_atomiq(reviewed)


class TestExtraction:
    def test_extract_pdf_conservative(self, tmp_path: Path) -> None:
        receipt = tmp_path / "hotel_receipt.pdf"
        text = b"""(
Thank you
) (
Marriott Houston
) (
Date: 06/15/2026
) (
Room Total
) (
$189.00
) (
Tax
) (
$15.12
)"""
        receipt.write_bytes(text)
        candidate = extract_from_file(receipt)
        assert candidate.vendor == "Marriott Houston"
        assert candidate.transaction_date == "2026-06-15"
        assert candidate.total_amount == "189.00"
        assert candidate.tax_amount == "15.12"
        assert candidate.category_guess == "hotel/lodging"
        assert candidate.source_file == str(receipt)
        assert candidate.status == STATUS_EXTRACTED

    def test_image_without_ocr_needs_review(self, tmp_path: Path) -> None:
        image = tmp_path / "scan.png"
        image.write_bytes(b"\x89PNG\r\n")
        candidate = extract_from_file(image)
        assert candidate.vendor is None
        assert candidate.total_amount is None
        assert candidate.needs_review is True
        assert candidate.notes is not None

    def test_run_extraction_writes_reviewed_json(self, tmp_path: Path) -> None:
        root = tmp_path / "Heron"
        paths = ensure_heron_dirs(root)
        receipt = paths["inbox"] / "lunch.pdf"
        receipt.write_bytes(b"(Joe's Grill)(\nTotal: $23.45\nTip: $4.00\n)")
        written = run_extraction(root)
        assert len(written) == 1
        data = json.loads(written[0].read_text(encoding="utf-8"))
        assert data["status"] == STATUS_EXTRACTED
        assert data["source_file"] == str(receipt)
        assert receipt.exists()

    def test_null_for_unknown_values(self, tmp_path: Path) -> None:
        empty = tmp_path / "blank.pdf"
        empty.write_bytes(b"%PDF-1.4 empty")
        candidate = extract_from_file(empty)
        assert candidate.vendor is None
        assert candidate.transaction_date is None
        assert candidate.total_amount is None
        assert candidate.tip_amount is None
