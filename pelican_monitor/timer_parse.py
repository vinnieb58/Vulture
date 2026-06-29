"""Parse systemd timer NextElapseUSecRealtime values from systemctl show."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

# systemctl show --value examples:
#   1740000000000000
#   Tue 2026-06-23 03:02:08 CDT
NEXT_ELAPSE_REALTIME_RE = re.compile(
    r"^(?P<dow>[A-Za-z]{3})\s+(?P<date>\d{4}-\d{2}-\d{2})\s+(?P<time>\d{2}:\d{2}:\d{2})\s+(?P<tz>\S+)$"
)

_INVALID_REALTIME = frozenset({"", "0", "n/a", "na", "none", "unknown", "unavailable", "not-found"})


def parse_next_elapse_realtime(
    raw: str,
    *,
    local_tz: ZoneInfo,
    now: datetime | None = None,
) -> tuple[bool, str | None]:
    """
    Interpret NextElapseUSecRealtime from ``systemctl show``.

    Returns (has_future_run, next_run_iso_or_none).

    Accepts integer microsecond timestamps or human-readable local datetimes such as
    ``Tue 2026-06-23 03:02:08 CDT``. Does not consult NextElapseUSecMonotonic.
    """
    text = (raw or "").strip()
    if not text or text.lower() in _INVALID_REALTIME:
        return False, None

    moment = now or datetime.now(tz=timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)

    # Integer microseconds since epoch (some systemd builds).
    if text.isdigit():
        usec = int(text)
        if usec <= 0:
            return False, None
        try:
            ts = datetime.fromtimestamp(usec / 1_000_000, tz=timezone.utc)
        except (OSError, OverflowError, ValueError):
            return False, None
        has_future = ts > moment
        return has_future, ts.isoformat(timespec="seconds")

    match = NEXT_ELAPSE_REALTIME_RE.match(text)
    if not match:
        return False, None

    try:
        naive = datetime.strptime(
            f"{match.group('date')} {match.group('time')}",
            "%Y-%m-%d %H:%M:%S",
        )
        ts = naive.replace(tzinfo=local_tz)
    except ValueError:
        return False, None

    ts_utc = ts.astimezone(timezone.utc)
    has_future = ts_utc > moment
    return has_future, ts.isoformat(timespec="seconds")
