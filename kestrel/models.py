"""Kestrel data models."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone


@dataclass(frozen=True)
class EnergyInterval:
    provider: str
    start_ts: str
    end_ts: str
    kwh: float
    meter_id_hash: str | None = None
    account_id_hash: str | None = None
    raw_source: str | None = None
    created_at: str | None = None

    def with_hashes(
        self,
        *,
        meter_id: str | None = None,
        account_id: str | None = None,
    ) -> "EnergyInterval":
        return EnergyInterval(
            provider=self.provider,
            start_ts=self.start_ts,
            end_ts=self.end_ts,
            kwh=self.kwh,
            meter_id_hash=hash_identifier(meter_id) if meter_id else self.meter_id_hash,
            account_id_hash=hash_identifier(account_id) if account_id else self.account_id_hash,
            raw_source=self.raw_source,
            created_at=self.created_at,
        )


def normalize_account_identifier(value: str | None) -> str | None:
    """Normalize account/meter identifiers before hashing (strip Excel apostrophe prefix)."""
    if not value:
        return None
    text = value.strip()
    if text.startswith("'"):
        text = text[1:].strip()
    return text or None


def hash_identifier(value: str | None) -> str | None:
    """Return a short stable hash for account/meter identifiers (never log raw values)."""
    normalized = normalize_account_identifier(value)
    if not normalized:
        return None
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    return digest[:16]


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def interval_end_from_start(start: datetime, minutes: int = 15) -> datetime:
    return start + timedelta(minutes=minutes)
