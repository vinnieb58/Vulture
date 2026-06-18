"""Heron expense candidate schema, paths, and safety helpers."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_ROOT = Path("/mnt/pelican_backup/Heron")

STATUS_EXTRACTED = "extracted"
STATUS_NEEDS_REVIEW = "needs_review"
STATUS_REVIEWED = "reviewed"

ATOMIQ_URL = "https://bnr.atomiq.us/"

HERON_DIR = Path(__file__).resolve().parent
AUTH_DIR = HERON_DIR / ".auth"
STORAGE_STATE_PATH = AUTH_DIR / "atomiq_storage_state.json"

# Known category -> Atomiq cost code hints (conservative mapping only).
CATEGORY_COST_CODES: dict[str, str] = {
    "meals with tip": "402200",
    "meals & entertainment / meals": "402200",
    "meals": "402200",
    "hotel/lodging": None,
    "rental car": None,
    "parking": None,
    "airfare/travel fee": None,
}

KNOWN_CATEGORIES = tuple(CATEGORY_COST_CODES.keys())

FORBIDDEN_CONTROL_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"\bsubmit\b", re.I),
    re.compile(r"\bfinalize\b", re.I),
    re.compile(r"\bapprove\b", re.I),
    re.compile(r"\bcertify\b", re.I),
    re.compile(r"send\s+for\s+approval", re.I),
    re.compile(r"complete\s+report", re.I),
    re.compile(r"reimbursement\s+submit", re.I),
    re.compile(r"\bpay\b", re.I),
    re.compile(r"\bcheckout\b", re.I),
]


@dataclass
class ExpenseCandidate:
    """One receipt-derived expense line awaiting human review."""

    vendor: str | None = None
    transaction_date: str | None = None
    total_amount: str | None = None
    tax_amount: str | None = None
    tip_amount: str | None = None
    category_guess: str | None = None
    cost_code_guess: str | None = None
    business_purpose_guess: str | None = None
    source_file: str | None = None
    confidence: float = 0.0
    needs_review: bool = True
    status: str = STATUS_EXTRACTED
    notes: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ExpenseCandidate:
        known = {f.name for f in cls.__dataclass_fields__.values()}
        fields = {k: data[k] for k in known if k in data}
        extra = {k: v for k, v in data.items() if k not in known}
        if extra:
            fields.setdefault("extra", {}).update(extra)
        return cls(**fields)


def heron_paths(root: Path | str | None = None) -> dict[str, Path]:
    base = Path(root).expanduser() if root else DEFAULT_ROOT
    return {
        "root": base,
        "inbox": base / "inbox",
        "reviewed": base / "reviewed",
        "done": base / "done",
    }


def ensure_heron_dirs(root: Path | str | None = None) -> dict[str, Path]:
    paths = heron_paths(root)
    for key in ("inbox", "reviewed", "done"):
        paths[key].mkdir(parents=True, exist_ok=True)
    return paths


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def load_candidate(path: Path) -> ExpenseCandidate:
    return ExpenseCandidate.from_dict(read_json(path))


def save_candidate(path: Path, candidate: ExpenseCandidate) -> None:
    write_json(path, candidate.to_dict())


def is_reviewed(candidate: ExpenseCandidate | dict[str, Any]) -> bool:
    status = candidate.status if isinstance(candidate, ExpenseCandidate) else candidate.get("status")
    return status == STATUS_REVIEWED


def can_proceed_to_atomiq(candidate: ExpenseCandidate | dict[str, Any]) -> bool:
    return is_reviewed(candidate)


def is_forbidden_control_text(text: str) -> bool:
    normalized = " ".join(text.split())
    if not normalized:
        return False
    return any(pattern.search(normalized) for pattern in FORBIDDEN_CONTROL_PATTERNS)


def assert_safe_control_text(text: str, *, context: str = "control") -> None:
    if is_forbidden_control_text(text):
        raise SafetyError(f"Refusing to interact with forbidden {context}: {text!r}")


def guess_cost_code(category: str | None) -> str | None:
    if not category:
        return None
    key = category.strip().lower()
    if key in CATEGORY_COST_CODES:
        return CATEGORY_COST_CODES[key]
    for known, code in CATEGORY_COST_CODES.items():
        if known in key or key in known:
            return code
    return None


class SafetyError(RuntimeError):
    """Raised when Heron would cross a hard safety boundary."""


VIEWPORT = {"width": 1440, "height": 900}
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
LOCALE = "en-US"
TIMEZONE = "America/Chicago"
NAV_TIMEOUT_MS = 60_000


def resolve_storage_state_path() -> Path:
    override = __import__("os").getenv("HERON_ATOMIQ_STORAGE_STATE_PATH", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    return STORAGE_STATE_PATH.resolve()
