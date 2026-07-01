#!/usr/bin/env python3
"""
Songkick probe (read-only HTML recon).

Songkick shut down new API keys; this probe attempts public metro/artist pages.
No API key is used. Expect HTTP 406 from many datacenter IPs.

Usage:
    python experiments/concerts/probe_songkick.py --city Houston --state TX
    python experiments/concerts/probe_songkick.py --artist "Breaking Benjamin"
"""

from __future__ import annotations

import json
import logging
import re
from datetime import date
from typing import Any, Optional
from urllib.parse import quote_plus

from bs4 import BeautifulSoup

from probe_common import (
    BROWSER_HEADERS,
    NormalizedEvent,
    artist_slug,
    build_normalized_event,
    handle_probe_main,
    http_get_text,
    save_artifact,
    setup_logging,
)

SOURCE = "songkick"

# Known metro area IDs (public Songkick URLs).
METRO_AREAS = {
    "houston": 12283,
    "austin": 26330,
    "dallas": 35129,
}


def _parse_event_cards(html: str) -> list[dict[str, Any]]:
    soup = BeautifulSoup(html, "html.parser")
    events: list[dict[str, Any]] = []

    for node in soup.select("li.event-listings-element, div.event-listing"):
        link = node.select_one("a[href*='/concerts/']")
        if not link:
            continue
        href = link.get("href") or ""
        title_node = node.select_one(".artists strong, .summary-line, h3, .event-details h3")
        venue_node = node.select_one(".venue-link, .location, .venue")
        time_node = node.select_one("time")
        events.append(
            {
                "title": (title_node.get_text(" ", strip=True) if title_node else link.get_text(" ", strip=True)),
                "venue": venue_node.get_text(" ", strip=True) if venue_node else "",
                "starts_at": time_node.get("datetime") if time_node else "",
                "url": href if href.startswith("http") else f"https://www.songkick.com{href}",
            }
        )

    if events:
        return events

    # Fallback: mine concert links from page when card markup differs.
    seen: set[str] = set()
    for link in soup.select("a[href*='/concerts/']"):
        href = link.get("href") or ""
        if href in seen:
            continue
        seen.add(href)
        text = link.get_text(" ", strip=True)
        if not text:
            continue
        events.append(
            {
                "title": text,
                "venue": "",
                "starts_at": "",
                "url": href if href.startswith("http") else f"https://www.songkick.com{href}",
            }
        )
        if len(events) >= 50:
            break
    return events


def _event_id_from_url(url: str) -> str:
    match = re.search(r"/concerts/(\d+)", url or "")
    return match.group(1) if match else ""


def normalize_songkick_event(raw: dict[str, Any], *, city: str = "", state: str = "") -> NormalizedEvent:
    url = str(raw.get("url") or "")
    return build_normalized_event(
        source=SOURCE,
        provider_event_id=_event_id_from_url(url),
        artist_or_title=str(raw.get("title") or ""),
        venue=str(raw.get("venue") or ""),
        city=city,
        state=state,
        starts_at=str(raw.get("starts_at") or ""),
        ticket_url=url,
        genre_or_classification="",
        raw_url=url,
    )


def _build_url(args: Any) -> tuple[str, str, str]:
    city = (args.city or "").strip()
    state = (args.state or "TX").strip()
    if args.artist:
        slug = artist_slug(args.artist)
        return (
            f"https://www.songkick.com/search?query={quote_plus(args.artist)}",
            city,
            state,
        )
    if city:
        metro_id = METRO_AREAS.get(city.lower())
        if metro_id is None:
            raise ValueError(
                f"Unknown Songkick metro for city={city!r}. "
                f"Known: {', '.join(sorted(METRO_AREAS))}"
            )
        slug = city.lower().replace(" ", "-")
        return (
            f"https://www.songkick.com/metro-areas/{metro_id}-{slug}-{state.lower()}",
            city,
            state,
        )
    raise ValueError("Songkick probe requires --city or --artist")


def run_probe(args: Any, start: date, end: date, log: logging.Logger):
    url, city, state = _build_url(args)
    log.info("GET %s", url)
    response = http_get_text(url, headers=BROWSER_HEADERS)
    status = response.status_code
    html = response.text

    blocked = status == 406 or len(html) < 100
    events_raw: list[dict[str, Any]] = []
    normalized: list[NormalizedEvent] = []

    if not blocked:
        events_raw = _parse_event_cards(html)
        for item in events_raw:
            event = normalize_songkick_event(item, city=city, state=state)
            normalized.append(event)
            if len(normalized) >= args.limit:
                break

    sample = {
        "request_url": url,
        "http_status": status,
        "blocked": blocked,
        "html_bytes": len(html),
        "events_sample": events_raw[:3],
    }
    artifact = save_artifact(
        SOURCE,
        args.artifact_label,
        sample,
        events=normalized,
        meta={"http_status": status, "result_count": len(normalized), "blocked": blocked},
    )
    notes = [
        f"date_window={start.isoformat()}..{end.isoformat()} (not enforced on HTML scrape)",
        "no public API key path; metro/artist HTML only",
        "blocked=True (HTTP 406) from this host" if blocked else "parsed public HTML",
    ]
    return normalized, artifact, notes


def main() -> int:
    return handle_probe_main(setup_logging("probe_songkick"), source=SOURCE, run_probe=run_probe)


if __name__ == "__main__":
    raise SystemExit(main())
