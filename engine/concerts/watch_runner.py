"""Run active concert watches and emit alerts for new events."""

from __future__ import annotations

import logging

from engine.concerts.dedupe import MergedConcertEvent
from engine.concerts.notifier import send_concert_alert
from engine.concerts.repository import (
    alert_exists,
    list_watches,
    record_alert,
    upsert_provider_events,
    watch_to_criteria,
)
from engine.concerts.search import search_concerts

log = logging.getLogger(__name__)


def run_concert_watches() -> dict:
    """
    Check all active watches, persist events, and alert on new event_dedupe_keys.

    Returns summary dict with counts for logging/ops.
    """
    watches = list_watches(active_only=True)
    summary = {
        "watches_checked": len(watches),
        "events_found": 0,
        "alerts_sent": 0,
        "errors": [],
    }

    for watch in watches:
        criteria = watch_to_criteria(watch)
        try:
            result = search_concerts(criteria)
        except Exception as exc:
            log.exception("Watch #%s search failed", watch.id)
            summary["errors"].append(f"watch #{watch.id}: {exc}")
            continue

        summary["events_found"] += len(result.events)
        upsert_provider_events(result.events)

        for event in result.events:
            if alert_exists(watch.id, event.event_dedupe_key):
                continue
            if _send_watch_alert(watch.id, event):
                summary["alerts_sent"] += 1

    log.info(
        "Concert watch cycle: watches=%d events=%d alerts=%d",
        summary["watches_checked"],
        summary["events_found"],
        summary["alerts_sent"],
    )
    return summary


def _send_watch_alert(watch_id: int, event: MergedConcertEvent) -> bool:
    if send_concert_alert(event):
        record_alert(watch_id, event.event_dedupe_key)
        return True
    # Still record alert to avoid spam retries when webhook is down? 
    # For v1, only record on successful send so webhook recovery re-alerts.
    return False
