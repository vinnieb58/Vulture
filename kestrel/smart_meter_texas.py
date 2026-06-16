"""
Smart Meter Texas data access — CSV import (v0) and probe-quality portal API fetch.

Read-only. Uses the residential portal JSON endpoints (unofficial, may change).
Official registered API (services.smartmetertexas.net) is not implemented in v0.
"""

from __future__ import annotations

import csv
import io
import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import requests

from kestrel.config import KestrelConfig, PROVIDER_SMART_METER_TEXAS
from kestrel.models import (
    EnergyInterval,
    hash_identifier,
    interval_end_from_start,
    normalize_account_identifier,
    utc_now_iso,
)

log = logging.getLogger(__name__)

SMT_BASE_URL = "https://www.smartmetertexas.com"
SMT_AUTH_URL = f"{SMT_BASE_URL}/commonapi/user/authenticate"
SMT_API_BASE = f"{SMT_BASE_URL}/api"
SMT_METER_URL = f"{SMT_API_BASE}/meter"
SMT_INTERVAL_URL = f"{SMT_API_BASE}/adhoc/intervalsynch"

CLIENT_HEADERS = {
    "accept": "application/json, text/plain, */*",
    "accept-language": "en-US,en;q=0.9",
    "content-type": "application/json",
    "origin": SMT_BASE_URL,
    "referer": f"{SMT_BASE_URL}/home",
    "user-agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
}

START_ALIASES = frozenset(
    {
        "start",
        "start time",
        "start_time",
        "starttime",
        "interval start",
        "interval_start",
        "intervalstart",
        "date time",
        "datetime",
        "date_time",
        "begin",
        "from",
        "reading start",
    }
)
END_ALIASES = frozenset(
    {
        "end",
        "end time",
        "end_time",
        "endtime",
        "interval end",
        "interval_end",
        "intervalend",
        "stop",
        "to",
        "reading end",
    }
)
USAGE_ALIASES = frozenset(
    {
        "usage",
        "kwh",
        "usage kwh",
        "kwh used",
        "energy",
        "consumption",
        "value",
        "interval usage",
        "interval kwh",
    }
)
DATE_ALIASES = frozenset({"date", "reading date", "day"})
SMT_USAGE_DATE_ALIASES = frozenset({"usage date"})
TIME_ALIASES = frozenset({"time", "reading time", "interval time"})
SMT_START_TIME_ALIASES = frozenset({"usage start time"})
SMT_END_TIME_ALIASES = frozenset({"usage end time"})
SMT_USAGE_KWH_ALIASES = frozenset({"usage kwh"})
ESIID_ALIASES = frozenset({"esiid"})
REVISION_DATE_ALIASES = frozenset({"revision date"})
ESTIMATED_ACTUAL_ALIASES = frozenset({"estimated actual"})
CONSUMPTION_TYPE_ALIASES = frozenset({"consumption surplusgeneration"})


class SmartMeterTexasError(Exception):
    """Safe, user-facing SMT fetch/import error (no secrets in message)."""


class SmartMeterTexasClient:
    """Probe-quality portal API client (read-only)."""

    def __init__(self, config: KestrelConfig, *, session: requests.Session | None = None):
        if not config.has_smt_credentials:
            raise SmartMeterTexasError("Smart Meter Texas username/password are required for live fetch.")
        self._config = config
        self._session = session or requests.Session()
        self._session.headers.update(CLIENT_HEADERS)
        self._token: str | None = None

    def authenticate(self) -> None:
        assert self._config.smt_username and self._config.smt_password
        response = self._session.post(
            SMT_AUTH_URL,
            json={
                "username": self._config.smt_username,
                "password": self._config.smt_password,
                "rememberMe": "true",
            },
            timeout=30,
        )
        if response.status_code in (400, 401, 403):
            raise SmartMeterTexasError(
                "Smart Meter Texas login failed. Check KESTREL_SMT_USERNAME and KESTREL_SMT_PASSWORD."
            )
        response.raise_for_status()
        payload = response.json()
        token = payload.get("token")
        if not token:
            raise SmartMeterTexasError("Smart Meter Texas login response did not include a token.")
        self._token = token
        self._session.headers["Authorization"] = f"Bearer {token}"
        log.info("Smart Meter Texas authentication succeeded")

    def list_meters(self) -> list[dict[str, Any]]:
        self._ensure_authenticated()
        response = self._session.post(SMT_METER_URL, json={"esiid": "*"}, timeout=30)
        response.raise_for_status()
        payload = response.json()
        meters = payload.get("data") or []
        if not isinstance(meters, list):
            raise SmartMeterTexasError("Unexpected meter list response from Smart Meter Texas.")
        return meters

    def fetch_day_intervals(self, esiid: str, day: date) -> list[EnergyInterval]:
        self._ensure_authenticated()
        day_str = day.strftime("%m/%d/%Y")
        response = self._session.post(
            SMT_INTERVAL_URL,
            json={
                "startDate": day_str,
                "endDate": day_str,
                "reportFormat": "JSON",
                "ESIID": [esiid],
                "versionDate": None,
                "readDate": None,
                "versionNum": None,
                "dataType": None,
            },
            timeout=60,
        )
        response.raise_for_status()
        payload = response.json()
        return parse_interval_synch_payload(
            payload,
            day=day,
            tz_name=self._config.timezone,
            account_id=esiid,
            raw_source="smt_portal_api",
        )

    def _ensure_authenticated(self) -> None:
        if not self._token:
            self.authenticate()


def fetch_intervals(
    config: KestrelConfig,
    *,
    start: date,
    end: date,
    account_id: str | None = None,
) -> list[EnergyInterval]:
    """
    Fetch 15-minute intervals from the SMT residential portal API.

    This uses unofficial portal endpoints and may break without notice.
    """
    client = SmartMeterTexasClient(config)
    client.authenticate()

    esiid = account_id or config.smt_account_id
    meter_number: str | None = None
    if not esiid:
        meters = client.list_meters()
        if not meters:
            raise SmartMeterTexasError(
                "No meters found on Smart Meter Texas account. "
                "Set KESTREL_SMT_ACCOUNT_ID to your ESIID."
            )
        if len(meters) > 1:
            log.warning(
                "Multiple meters found; using the first meter. "
                "Set KESTREL_SMT_ACCOUNT_ID to select a specific ESIID."
            )
        esiid = str(meters[0].get("esiid", "")).strip()
        meter_number = str(meters[0].get("meterNumber", "")).strip() or None
        if not esiid:
            raise SmartMeterTexasError("Could not determine ESIID from Smart Meter Texas meter list.")

    account_hash = hash_identifier(esiid)
    meter_hash = hash_identifier(meter_number) if meter_number else None

    if end < start:
        raise SmartMeterTexasError("End date must be on or after start date.")

    intervals: list[EnergyInterval] = []
    cursor = start
    while cursor <= end:
        try:
            day_rows = client.fetch_day_intervals(esiid, cursor)
        except requests.RequestException as exc:
            raise SmartMeterTexasError(
                f"Failed to fetch Smart Meter Texas data for {cursor.isoformat()}: {exc}"
            ) from exc
        for row in day_rows:
            intervals.append(
                EnergyInterval(
                    provider=row.provider,
                    start_ts=row.start_ts,
                    end_ts=row.end_ts,
                    kwh=row.kwh,
                    meter_id_hash=meter_hash or row.meter_id_hash,
                    account_id_hash=account_hash,
                    raw_source=row.raw_source,
                    created_at=utc_now_iso(),
                )
            )
        cursor += timedelta(days=1)

    return intervals


def import_csv_file(
    path: Path | str,
    *,
    tz_name: str = "America/Chicago",
    account_id: str | None = None,
    meter_id: str | None = None,
) -> list[EnergyInterval]:
    csv_path = Path(path)
    if not csv_path.is_file():
        raise SmartMeterTexasError(f"CSV file not found: {csv_path}")
    content = csv_path.read_text(encoding="utf-8-sig")
    return parse_csv_content(
        content,
        tz_name=tz_name,
        account_id=account_id,
        meter_id=meter_id,
        raw_source=f"csv:{csv_path.name}",
    )


def parse_csv_content(
    content: str,
    *,
    tz_name: str = "America/Chicago",
    account_id: str | None = None,
    meter_id: str | None = None,
    raw_source: str = "csv",
) -> list[EnergyInterval]:
    reader = csv.DictReader(io.StringIO(content))
    if not reader.fieldnames:
        raise SmartMeterTexasError("CSV has no header row.")

    columns = {_normalize_header(name): name for name in reader.fieldnames if name}
    mapping = _resolve_columns(columns)
    tz = ZoneInfo(tz_name)
    account_hash = hash_identifier(account_id)
    meter_hash = hash_identifier(meter_id)

    intervals: list[EnergyInterval] = []
    for line_no, row in enumerate(reader, start=2):
        try:
            interval = _parse_csv_row(
                row,
                mapping,
                tz,
                default_account_hash=account_hash,
                raw_source=raw_source,
            )
        except ValueError as exc:
            raise SmartMeterTexasError(f"CSV line {line_no}: {exc}") from exc
        intervals.append(
            EnergyInterval(
                provider=PROVIDER_SMART_METER_TEXAS,
                start_ts=interval.start_ts,
                end_ts=interval.end_ts,
                kwh=interval.kwh,
                meter_id_hash=meter_hash,
                account_id_hash=interval.account_id_hash or account_hash,
                raw_source=interval.raw_source or raw_source,
                created_at=utc_now_iso(),
            )
        )

    intervals.sort(key=lambda item: item.start_ts)
    return intervals


def parse_interval_synch_payload(
    payload: dict[str, Any],
    *,
    day: date,
    tz_name: str,
    account_id: str | None = None,
    raw_source: str = "smt_portal_api",
) -> list[EnergyInterval]:
    data = payload.get("data") or {}
    if data.get("errorCode"):
        message = str(data.get("errorMessage") or "unknown error")
        raise SmartMeterTexasError(f"Smart Meter Texas interval error: {message}")

    energy_entries = data.get("energyData") or []
    consumption = next((entry for entry in energy_entries if entry.get("RT") == "C"), None)
    if consumption is None and energy_entries:
        consumption = energy_entries[0]

    if not consumption:
        return []

    rd = str(consumption.get("RD") or "")
    values = [_parse_usage_value(part) for part in rd.split(",") if part.strip() != ""]
    tz = ZoneInfo(tz_name)
    day_start = datetime(day.year, day.month, day.day, tzinfo=tz)
    account_hash = hash_identifier(account_id)

    intervals: list[EnergyInterval] = []
    for index, kwh in enumerate(values):
        start_local = day_start + timedelta(minutes=15 * index)
        end_local = start_local + timedelta(minutes=15)
        start_utc = start_local.astimezone(timezone.utc).replace(microsecond=0)
        end_utc = end_local.astimezone(timezone.utc).replace(microsecond=0)
        intervals.append(
            EnergyInterval(
                provider=PROVIDER_SMART_METER_TEXAS,
                start_ts=start_utc.isoformat(),
                end_ts=end_utc.isoformat(),
                kwh=kwh,
                account_id_hash=account_hash,
                raw_source=raw_source,
                created_at=utc_now_iso(),
            )
        )
    return intervals


@dataclass(frozen=True)
class _ParsedCsvRow:
    start_ts: str
    end_ts: str
    kwh: float
    account_id_hash: str | None = None
    raw_source: str | None = None


def _parse_csv_row(
    row: dict[str, str | None],
    mapping: dict[str, str | None],
    tz: ZoneInfo,
    *,
    default_account_hash: str | None,
    raw_source: str,
) -> _ParsedCsvRow:
    if _is_smt_portal_export(mapping):
        return _parse_smt_portal_row(row, mapping, tz, default_account_hash=default_account_hash, raw_source=raw_source)

    start_col = mapping.get("start")
    end_col = mapping.get("end")
    usage_col = mapping.get("usage")
    date_col = mapping.get("date")
    time_col = mapping.get("time")

    if usage_col is None:
        raise ValueError("could not find a usage/kWh column")

    kwh = _parse_usage_value(row.get(usage_col) or "")

    if start_col and row.get(start_col):
        start_local = _parse_datetime(str(row[start_col]), tz)
        if end_col and row.get(end_col):
            end_local = _parse_datetime(str(row[end_col]), tz)
        else:
            end_local = interval_end_from_start(start_local, 15)
    elif date_col and time_col and row.get(date_col) and row.get(time_col):
        combined = f"{row[date_col]} {row[time_col]}"
        start_local = _parse_datetime(combined, tz)
        end_local = interval_end_from_start(start_local, 15)
    else:
        raise ValueError("could not find start/end or date+time columns")

    start_utc = start_local.astimezone(timezone.utc).replace(microsecond=0)
    end_utc = end_local.astimezone(timezone.utc).replace(microsecond=0)
    return _ParsedCsvRow(
        start_ts=start_utc.isoformat(),
        end_ts=end_utc.isoformat(),
        kwh=kwh,
        account_id_hash=default_account_hash,
        raw_source=raw_source,
    )


def _is_smt_portal_export(mapping: dict[str, str | None]) -> bool:
    return all(
        mapping.get(key)
        for key in ("smt_usage_date", "smt_start_time", "smt_end_time", "smt_usage_kwh")
    )


def _parse_smt_portal_row(
    row: dict[str, str | None],
    mapping: dict[str, str | None],
    tz: ZoneInfo,
    *,
    default_account_hash: str | None,
    raw_source: str,
) -> _ParsedCsvRow:
    usage_date_col = mapping["smt_usage_date"]
    start_time_col = mapping["smt_start_time"]
    end_time_col = mapping["smt_end_time"]
    usage_kwh_col = mapping["smt_usage_kwh"]

    usage_date = (row.get(usage_date_col) or "").strip()
    start_time = (row.get(start_time_col) or "").strip()
    end_time = (row.get(end_time_col) or "").strip()
    if not usage_date or not start_time or not end_time:
        raise ValueError("missing USAGE_DATE, USAGE_START_TIME, or USAGE_END_TIME value")

    kwh = _parse_usage_value(row.get(usage_kwh_col) or "")
    start_local = _combine_smt_date_time(usage_date, start_time, tz)
    end_local = _combine_smt_end_datetime(usage_date, start_time, end_time, tz)

    esiid_col = mapping.get("esiid")
    row_account_hash = default_account_hash
    if esiid_col and row.get(esiid_col):
        normalized = normalize_account_identifier(str(row[esiid_col]))
        row_account_hash = hash_identifier(normalized)

    return _ParsedCsvRow(
        start_ts=start_local.astimezone(timezone.utc).replace(microsecond=0).isoformat(),
        end_ts=end_local.astimezone(timezone.utc).replace(microsecond=0).isoformat(),
        kwh=kwh,
        account_id_hash=row_account_hash,
        raw_source=_append_smt_row_metadata(raw_source, row, mapping),
    )


def _append_smt_row_metadata(
    raw_source: str,
    row: dict[str, str | None],
    mapping: dict[str, str | None],
) -> str:
    parts = [raw_source]
    estimated_col = mapping.get("estimated_actual")
    if estimated_col and row.get(estimated_col):
        parts.append(f"est={row[estimated_col].strip()}")
    consumption_col = mapping.get("consumption_type")
    if consumption_col and row.get(consumption_col):
        parts.append(f"type={row[consumption_col].strip()}")
    revision_col = mapping.get("revision_date")
    if revision_col and row.get(revision_col):
        parts.append(f"revision={row[revision_col].strip()}")
    return ";".join(parts)


def _combine_smt_date_time(usage_date: str, time_value: str, tz: ZoneInfo) -> datetime:
    day = _parse_smt_date(usage_date)
    hour, minute = _parse_smt_time(time_value)
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=tz)


def _combine_smt_end_datetime(
    usage_date: str,
    start_time: str,
    end_time: str,
    tz: ZoneInfo,
) -> datetime:
    end_local = _combine_smt_date_time(usage_date, end_time, tz)
    start_hour, start_minute = _parse_smt_time(start_time)
    end_hour, end_minute = _parse_smt_time(end_time)
    if (end_hour, end_minute) <= (start_hour, start_minute):
        end_local += timedelta(days=1)
    return end_local


def _parse_smt_date(value: str) -> date:
    text = value.strip().lstrip("'")
    try:
        return datetime.strptime(text, "%m/%d/%Y").date()
    except ValueError as exc:
        raise ValueError(f"unrecognized USAGE_DATE format: {value!r}") from exc


def _parse_smt_time(value: str) -> tuple[int, int]:
    text = value.strip().lstrip("'")
    try:
        parsed = datetime.strptime(text, "%H:%M")
    except ValueError as exc:
        raise ValueError(f"unrecognized USAGE_*_TIME format: {value!r}") from exc
    return parsed.hour, parsed.minute


def _resolve_columns(columns: dict[str, str]) -> dict[str, str | None]:
    def pick(aliases: frozenset[str]) -> str | None:
        for alias in aliases:
            if alias in columns:
                return columns[alias]
        return None

    usage_col = pick(USAGE_ALIASES) or pick(SMT_USAGE_KWH_ALIASES)

    return {
        "start": pick(START_ALIASES),
        "end": pick(END_ALIASES),
        "usage": usage_col,
        "date": pick(DATE_ALIASES),
        "time": pick(TIME_ALIASES),
        "smt_usage_date": pick(SMT_USAGE_DATE_ALIASES),
        "smt_start_time": pick(SMT_START_TIME_ALIASES),
        "smt_end_time": pick(SMT_END_TIME_ALIASES),
        "smt_usage_kwh": pick(SMT_USAGE_KWH_ALIASES),
        "esiid": pick(ESIID_ALIASES),
        "revision_date": pick(REVISION_DATE_ALIASES),
        "estimated_actual": pick(ESTIMATED_ACTUAL_ALIASES),
        "consumption_type": pick(CONSUMPTION_TYPE_ALIASES),
    }


def _normalize_header(name: str) -> str:
    cleaned = re.sub(r"[^a-z0-9]+", " ", name.strip().lower())
    return re.sub(r"\s+", " ", cleaned).strip()


def _parse_datetime(value: str, tz: ZoneInfo) -> datetime:
    text = value.strip()
    if not text:
        raise ValueError("empty datetime value")

    formats = (
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%m/%d/%Y %H:%M:%S",
        "%m/%d/%Y %H:%M",
        "%m/%d/%Y %I:%M:%S %p",
        "%m/%d/%Y %I:%M %p",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M",
    )
    for fmt in formats:
        try:
            parsed = datetime.strptime(text, fmt)
            if parsed.tzinfo is None:
                return parsed.replace(tzinfo=tz)
            return parsed
        except ValueError:
            continue

    # ISO fallback
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=tz)
        return parsed
    except ValueError as exc:
        raise ValueError(f"unrecognized datetime format: {value!r}") from exc


def _parse_usage_value(value: str) -> float:
    text = value.strip()
    if not text or text == "-":
        return 0.0
    if "-" in text and not text.startswith("-"):
        text = text.split("-", 1)[0]
    try:
        return round(float(text), 6)
    except ValueError as exc:
        raise ValueError(f"invalid kWh value: {value!r}") from exc
