"""Shared result types for Pelican backup checks."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class BackupCheckResult:
    backup_id: str
    display_name: str
    status: str  # ok | warning | critical | error | skipped
    reason: str
    checked_at: str
    newest_backup_timestamp: str | None = None
    backup_age_hours: float | None = None
    warn_threshold_hours: float = 30.0
    critical_threshold_hours: float = 36.0
    target_available: bool = False
    checksum_status: str = "not_checked"  # ok | missing | invalid | not_checked | error
    timer: dict[str, Any] = field(default_factory=dict)
    service: dict[str, Any] = field(default_factory=dict)
    issue_codes: list[str] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def combine_status(*statuses: str) -> str:
    order = {"critical": 3, "error": 3, "warning": 2, "ok": 1, "skipped": 0}
    best = "ok"
    best_rank = 0
    for status in statuses:
        rank = order.get(status, 1)
        if rank > best_rank:
            best_rank = rank
            best = status
    return best


def checker_error_result(
    *,
    backup_id: str,
    display_name: str,
    checked_at: str,
    exc: Exception,
    warn_threshold_hours: float,
    critical_threshold_hours: float,
) -> BackupCheckResult:
    return BackupCheckResult(
        backup_id=backup_id,
        display_name=display_name,
        status="error",
        reason=f"Checker failed: {exc}",
        checked_at=checked_at,
        warn_threshold_hours=warn_threshold_hours,
        critical_threshold_hours=critical_threshold_hours,
        target_available=False,
        checksum_status="error",
        issue_codes=["CHECKER_ERROR"],
        details={"error": str(exc)},
    )
