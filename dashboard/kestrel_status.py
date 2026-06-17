"""Defensive Kestrel status JSON reader for the Nest dashboard."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

KESTREL_STATUS_PATH = Path(
    os.environ.get("KESTREL_STATUS_PATH", "/app/data/kestrel/kestrel_status.json")
)

_SENSITIVE_KEYS = frozenset({
    "account_id",
    "account_id_hash",
    "meter_id",
    "meter_id_hash",
    "esiid",
    "credentials",
    "password",
    "username",
    "raw_source",
    "db_path",
    "smt_username",
    "smt_password",
    "token",
    "cookie",
    "cookies",
})

_HASH_KEY_PATTERN = re.compile(r".*hash.*", re.IGNORECASE)
_BEARER = re.compile(r"(?i)\bbearer\s+\S+")
_ESIID = re.compile(r"\b\d{15,22}\b")
_ENV_SECRET = re.compile(r"(?i)\b(PASSWORD|USERNAME|TOKEN|SECRET)\s*=\s*\S+")


def _redact_message(text: str | None) -> str | None:
    if not text:
        return text
    result = text
    result = _BEARER.sub("Bearer [REDACTED]", result)
    result = _ENV_SECRET.sub(r"\1=[REDACTED]", result)
    result = _ESIID.sub("[REDACTED_ESIID]", result)
    return result


def _is_sensitive_key(key: str) -> bool:
    return key in _SENSITIVE_KEYS or bool(_HASH_KEY_PATTERN.match(key))


def _strip_sensitive(value: Any) -> Any:
    """Recursively remove sensitive keys from nested structures."""
    if isinstance(value, dict):
        return {
            key: _strip_sensitive(item)
            for key, item in value.items()
            if not _is_sensitive_key(key)
        }
    if isinstance(value, list):
        return [_strip_sensitive(item) for item in value]
    return value


def _format_range(range_start: str | None, range_end: str | None) -> str | None:
    if range_start and range_end:
        return f"{range_start} → {range_end}"
    if range_start:
        return f"from {range_start}"
    if range_end:
        return f"through {range_end}"
    return None


def _parse_peak_interval_kwh(raw: dict[str, Any]) -> float | None:
    peak = raw.get("peak_interval")
    if isinstance(peak, dict):
        kwh = peak.get("kwh")
        if isinstance(kwh, (int, float)):
            return float(kwh)

    peak_kwh = raw.get("peak_interval_kwh")
    if isinstance(peak_kwh, (int, float)):
        return float(peak_kwh)
    return None


def _parse_top_intervals(raw: dict[str, Any]) -> list[dict[str, Any]]:
    top = raw.get("top_intervals")
    if not isinstance(top, list):
        return []

    parsed: list[dict[str, Any]] = []
    for item in top[:5]:
        if not isinstance(item, dict):
            continue
        clean = _strip_sensitive(item)
        start_ts = clean.get("start_ts")
        kwh = clean.get("kwh")
        if not start_ts or not isinstance(kwh, (int, float)):
            continue
        end_ts = clean.get("end_ts")
        est_kw = clean.get("estimated_peak_kw")
        if not isinstance(est_kw, (int, float)):
            est_kw = clean.get("estimated_kw")
        parsed.append(
            {
                "start_ts": str(start_ts),
                "end_ts": str(end_ts) if end_ts else None,
                "kwh": float(kwh),
                "estimated_peak_kw": float(est_kw) if isinstance(est_kw, (int, float)) else None,
            }
        )
    return parsed


def _parse_daily_totals(raw: dict[str, Any]) -> dict[str, float]:
    daily = raw.get("daily_totals")
    if isinstance(daily, list):
        totals: dict[str, float] = {}
        for item in daily:
            if not isinstance(item, dict):
                continue
            day = item.get("date") or item.get("day")
            kwh = item.get("kwh")
            if day and isinstance(kwh, (int, float)):
                totals[str(day)] = float(kwh)
        return dict(sorted(totals.items()))
    if not isinstance(daily, dict):
        return {}
    return {
        str(day): float(value)
        for day, value in sorted(daily.items())
        if isinstance(value, (int, float))
    }


def _has_energy_data(
    *,
    interval_count: int | None,
    total_kwh: float | None,
) -> bool:
    if interval_count is not None and interval_count > 0:
        return True
    return total_kwh is not None and total_kwh > 0


def read_kestrel_status() -> dict[str, Any]:
    """
    Load a sanitized Kestrel status snapshot.

    Returns a dict safe for dashboard templates. Never raises.
    ``state`` is one of: ``available``, ``no_data``, ``error``.
    """
    result: dict[str, Any] = {
        "state": "no_data",
        "headline": "No energy data yet",
        "warning": None,
        "generated_at": None,
        "range": None,
        "range_start": None,
        "range_end": None,
        "interval_count": None,
        "total_kwh": None,
        "peak_interval_kwh": None,
        "estimated_peak_kw": None,
        "missing_interval_count": None,
        "top_intervals": [],
        "daily_totals": {},
        "last_refresh_attempt_at": None,
        "last_refresh_success_at": None,
        "last_refresh_source": None,
        "last_refresh_status": None,
        "last_refresh_message": None,
    }

    if not KESTREL_STATUS_PATH.exists():
        result["warning"] = "Kestrel status file not found"
        return result

    try:
        text = KESTREL_STATUS_PATH.read_text(encoding="utf-8")
    except OSError as exc:
        result["state"] = "error"
        result["headline"] = "Could not read energy status"
        result["warning"] = f"Could not read Kestrel status file: {exc}"
        return result

    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        result["state"] = "error"
        result["headline"] = "Energy status unavailable"
        result["warning"] = f"Invalid Kestrel status JSON: {exc}"
        return result

    if not isinstance(raw, dict):
        result["state"] = "error"
        result["headline"] = "Energy status unavailable"
        result["warning"] = "Kestrel status JSON must be an object"
        return result

    clean = _strip_sensitive(raw)

    generated_at = clean.get("generated_at") or clean.get("updated_at") or clean.get("last_updated")
    if generated_at is not None:
        result["generated_at"] = str(generated_at)

    range_start = clean.get("range_start")
    range_end = clean.get("range_end")
    if range_start is not None:
        result["range_start"] = str(range_start)
    if range_end is not None:
        result["range_end"] = str(range_end)
    result["range"] = _format_range(
        str(range_start) if range_start else None,
        str(range_end) if range_end else None,
    )

    interval_count = clean.get("interval_count")
    if isinstance(interval_count, (int, float)):
        result["interval_count"] = int(interval_count)

    total_kwh = clean.get("total_kwh")
    if isinstance(total_kwh, (int, float)):
        result["total_kwh"] = float(total_kwh)

    result["peak_interval_kwh"] = _parse_peak_interval_kwh(clean)

    estimated_peak_kw = clean.get("estimated_peak_kw")
    if isinstance(estimated_peak_kw, (int, float)):
        result["estimated_peak_kw"] = float(estimated_peak_kw)

    missing_interval_count = clean.get("missing_interval_count")
    if isinstance(missing_interval_count, (int, float)):
        result["missing_interval_count"] = int(missing_interval_count)

    result["top_intervals"] = _parse_top_intervals(clean)
    result["daily_totals"] = _parse_daily_totals(clean)

    for field in (
        "last_refresh_attempt_at",
        "last_refresh_success_at",
        "last_refresh_source",
        "last_refresh_status",
        "last_refresh_message",
    ):
        value = clean.get(field)
        if value is not None:
            if field == "last_refresh_message":
                result[field] = _redact_message(str(value))
            else:
                result[field] = str(value)

    if result.get("last_refresh_status") == "failed":
        result["warning"] = result.get("last_refresh_message") or "Last live refresh failed"
    elif result.get("last_refresh_status") == "partial":
        result["warning"] = (
            result.get("last_refresh_message")
            or "Latest interval data unavailable (likely TDSP lag)"
        )
    elif result.get("last_refresh_status") == "unsupported":
        result["warning"] = result.get("last_refresh_message") or "Live refresh is not supported"

    if _has_energy_data(
        interval_count=result["interval_count"],
        total_kwh=result["total_kwh"],
    ):
        result["state"] = "available"
        result["headline"] = "Energy data available"
    elif clean.get("status") == "available":
        result["state"] = "available"
        result["headline"] = "Energy data available"
    elif result["interval_count"] == 0:
        result["state"] = "no_data"
        result["headline"] = "No energy data yet"
    else:
        result["state"] = "no_data"
        result["headline"] = "No energy data yet"

    return result
