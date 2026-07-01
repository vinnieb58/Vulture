"""
Heron receipt extraction — conservative OCR-free parsing from inbox files.

Usage:
  python experiments/heron/heron_extract.py --root /mnt/pelican_backup/Heron
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
from datetime import datetime
from pathlib import Path

from heron_schema import (
    ExpenseCandidate,
    STATUS_EXTRACTED,
    ensure_heron_dirs,
    guess_cost_code,
    save_candidate,
)

RECEIPT_SUFFIXES = {".pdf", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".tif", ".tiff"}

DATE_PATTERNS = [
    re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b"),
    re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b"),
    re.compile(
        r"\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+(\d{1,2}),?\s+(\d{4})\b",
        re.I,
    ),
]

AMOUNT_PATTERN = re.compile(r"(?:\$|USD\s*)?(\d{1,3}(?:,\d{3})*(?:\.\d{2})|\d+\.\d{2})")
TOTAL_HINTS = re.compile(r"\b(total|amount\s+due|grand\s+total|balance\s+due)\b", re.I)
TAX_HINTS = re.compile(r"\b(tax|sales\s+tax|vat)\b", re.I)
TIP_HINTS = re.compile(r"\b(tip|gratuity)\b", re.I)

CATEGORY_KEYWORDS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\b(hotel|lodging|inn|motel|marriott|hilton|hyatt)\b", re.I), "hotel/lodging"),
    (re.compile(r"\b(rental\s+car|hertz|avis|enterprise|budget\s+rent)\b", re.I), "rental car"),
    (re.compile(r"\b(parking|garage|valet)\b", re.I), "parking"),
    (re.compile(r"\b(airfare|airline|flight|baggage|travel\s+fee)\b", re.I), "airfare/travel fee"),
    (re.compile(r"\b(restaurant|cafe|grill|bar|dining|meal|tip|gratuity)\b", re.I), "meals with tip"),
]

VENDOR_SKIP = re.compile(
    r"^(receipt|invoice|thank\s+you|customer\s+copy|merchant\s+copy|page\s+\d+|%pdf)",
    re.I,
)


def _looks_like_vendor_line(line: str) -> bool:
    if not line or VENDOR_SKIP.match(line):
        return False
    if re.search(r"[^\x20-\x7e]", line):
        return False
    printable_ratio = sum(ch.isalnum() or ch.isspace() for ch in line) / len(line)
    return printable_ratio >= 0.85


def setup_logging() -> logging.Logger:
    logging.basicConfig(level=logging.INFO, format="%(levelname)-8s %(message)s", stream=sys.stdout)
    return logging.getLogger("heron.extract")


def extract_text_from_pdf(path: Path) -> str:
    """Best-effort text from PDF streams without external OCR libraries."""
    raw = path.read_bytes()
    chunks: list[str] = []
    for match in re.finditer(rb"\(([^()\\]{2,200})\)", raw):
        try:
            chunks.append(match.group(1).decode("latin-1", errors="ignore"))
        except Exception:
            continue
    for match in re.finditer(rb"\[([^\]]{2,200})\]", raw):
        try:
            chunks.append(match.group(1).decode("latin-1", errors="ignore"))
        except Exception:
            continue
    if chunks:
        return "\n".join(chunks)
    return ""


def read_receipt_text(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return extract_text_from_pdf(path)
    if suffix in RECEIPT_SUFFIXES:
        # No OCR — image receipts require human review.
        return ""
    return ""


def normalize_date(match: re.Match[str]) -> str | None:
    try:
        if match.re is DATE_PATTERNS[0]:
            return f"{match.group(1)}-{match.group(2)}-{match.group(3)}"
        if match.re is DATE_PATTERNS[1]:
            month, day, year = int(match.group(1)), int(match.group(2)), int(match.group(3))
            return f"{year:04d}-{month:02d}-{day:02d}"
        if match.re is DATE_PATTERNS[2]:
            month_name = match.group(1)[:3].title()
            dt = datetime.strptime(f"{month_name} {match.group(2)} {match.group(3)}", "%b %d %Y")
            return dt.strftime("%Y-%m-%d")
    except (ValueError, OverflowError):
        return None
    return None


def find_transaction_date(text: str) -> str | None:
    for pattern in DATE_PATTERNS:
        match = pattern.search(text)
        if match:
            parsed = normalize_date(match)
            if parsed:
                return parsed
    return None


def find_amount_near_hint(text: str, hint: re.Pattern[str]) -> str | None:
    lines = [ln.strip() for ln in text.splitlines()]
    for idx, line in enumerate(lines):
        if not hint.search(line):
            continue
        search_lines = [line]
        for offset in range(1, 4):
            if idx + offset < len(lines):
                search_lines.append(lines[idx + offset])
        for candidate_line in search_lines:
            amounts = AMOUNT_PATTERN.findall(candidate_line)
            if amounts:
                return amounts[-1].replace(",", "")
    return None


def find_total_amount(text: str) -> str | None:
    hinted = find_amount_near_hint(text, TOTAL_HINTS)
    if hinted:
        return hinted
    amounts = [a.replace(",", "") for a in AMOUNT_PATTERN.findall(text)]
    if not amounts:
        return None
    try:
        return max(amounts, key=lambda a: float(a))
    except ValueError:
        return amounts[-1]


def guess_vendor(text: str, source_file: str) -> str | None:
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    for line in lines[:12]:
        if len(line) < 3 or len(line) > 80:
            continue
        if not _looks_like_vendor_line(line):
            continue
        if AMOUNT_PATTERN.search(line) and TOTAL_HINTS.search(line):
            continue
        if re.search(r"\d{3}[-.\s]?\d{3}[-.\s]?\d{4}", line):
            continue
        return line
    return None


def guess_category(text: str) -> str | None:
    for pattern, label in CATEGORY_KEYWORDS:
        if pattern.search(text):
            return label
    return None


def guess_business_purpose(category: str | None, vendor: str | None) -> str | None:
    if not category and not vendor:
        return None
    if category and vendor:
        return f"{category.replace('/', ' ')} expense at {vendor}"
    if vendor:
        return f"Business expense at {vendor}"
    return f"Business {category} expense"


def score_confidence(candidate: ExpenseCandidate, text: str) -> float:
    score = 0.0
    if candidate.vendor:
        score += 0.2
    if candidate.transaction_date:
        score += 0.2
    if candidate.total_amount:
        score += 0.3
    if candidate.category_guess:
        score += 0.15
    if text.strip():
        score += 0.15
    return round(min(score, 1.0), 2)


def extract_from_file(path: Path) -> ExpenseCandidate:
    text = read_receipt_text(path)
    source_file = str(path)

    vendor = guess_vendor(text, path.name) if text else None
    transaction_date = find_transaction_date(text) if text else None
    total_amount = find_total_amount(text) if text else None
    tax_amount = find_amount_near_hint(text, TAX_HINTS) if text else None
    tip_amount = find_amount_near_hint(text, TIP_HINTS) if text else None
    category_guess = guess_category(text) if text else None
    cost_code_guess = guess_cost_code(category_guess)
    business_purpose_guess = guess_business_purpose(category_guess, vendor)

    candidate = ExpenseCandidate(
        vendor=vendor,
        transaction_date=transaction_date,
        total_amount=total_amount,
        tax_amount=tax_amount,
        tip_amount=tip_amount,
        category_guess=category_guess,
        cost_code_guess=cost_code_guess,
        business_purpose_guess=business_purpose_guess,
        source_file=source_file,
        status=STATUS_EXTRACTED,
    )
    candidate.confidence = score_confidence(candidate, text)
    candidate.needs_review = candidate.confidence < 0.6 or not text.strip()
    if not text.strip() and path.suffix.lower() != ".pdf":
        candidate.notes = "Image receipt — no OCR; manual review required."
    elif not text.strip():
        candidate.notes = "No extractable text found; manual review required."
    return candidate


def reviewed_output_path(reviewed_dir: Path, source: Path) -> Path:
    return reviewed_dir / f"{source.stem}.json"


def list_inbox_files(inbox: Path) -> list[Path]:
    if not inbox.is_dir():
        return []
    files = [p for p in sorted(inbox.iterdir()) if p.is_file() and p.suffix.lower() in RECEIPT_SUFFIXES]
    return files


def run_extraction(root: Path, *, overwrite: bool = False) -> list[Path]:
    paths = ensure_heron_dirs(root)
    written: list[Path] = []
    for receipt in list_inbox_files(paths["inbox"]):
        out_path = reviewed_output_path(paths["reviewed"], receipt)
        if out_path.exists() and not overwrite:
            logging.getLogger("heron.extract").info("Skipping existing %s", out_path.name)
            continue
        candidate = extract_from_file(receipt)
        save_candidate(out_path, candidate)
        written.append(out_path)
        logging.getLogger("heron.extract").info(
            "Wrote %s (confidence=%.2f needs_review=%s)",
            out_path,
            candidate.confidence,
            candidate.needs_review,
        )
    return written


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Heron receipt extraction (conservative, no OCR)")
    parser.add_argument(
        "--root",
        type=Path,
        default=Path("/mnt/pelican_backup/Heron"),
        help="Heron data root on Raven (default: /mnt/pelican_backup/Heron)",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Overwrite existing reviewed JSON files.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    log = setup_logging()
    args = parse_args(argv)
    paths = ensure_heron_dirs(args.root)
    log.info("Heron inbox: %s", paths["inbox"])
    written = run_extraction(args.root, overwrite=args.overwrite)
    log.info("Extraction complete — %d reviewed candidate(s) written", len(written))
    return 0


if __name__ == "__main__":
    sys.exit(main())
