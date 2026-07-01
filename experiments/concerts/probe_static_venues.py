#!/usr/bin/env python3
"""
Houston-area venue page probe (read-only).

Tests venue-specific event listings without API keys. Many Live Nation venue
pages are client-rendered placeholders from datacenter IPs.

Usage:
    python experiments/concerts/probe_static_venues.py
    python experiments/concerts/probe_static_venues.py --venue white_oak
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from datetime import date
from typing import Any, Optional

from bs4 import BeautifulSoup

from probe_common import (
    BROWSER_HEADERS,
    NormalizedEvent,
    REPO_ROOT,
    build_normalized_event,
    http_get_text,
    save_artifact,
    setup_logging,
)

SOURCE = "static_venues"

VENUES: dict[str, dict[str, str]] = {
    "cynthia_woods": {
        "label": "The Cynthia Woods Mitchell Pavilion",
        "url": "https://www.thewoodlandscenter.org/events",
    },
    "713_music_hall": {
        "label": "713 Music Hall",
        "url": "https://www.713musichall.com/events",
    },
    "house_of_blues_houston": {
        "label": "House of Blues Houston",
        "url": "https://www.houseofblues.com/houston/shows",
    },
    "white_oak": {
        "label": "White Oak Music Hall",
        "url": "https://whiteoakmusichall.com/events/",
    },
}

DATE_PATTERN = re.compile(
    r"\b(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
    r"Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"
    r"\s+\d{1,2},?\s+\d{4}\b",
    re.I,
)


def _parse_ld_json_events(html: str) -> list[dict[str, Any]]:
    soup = BeautifulSoup(html, "html.parser")
    events: list[dict[str, Any]] = []
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            payload = json.loads(script.string or "")
        except (TypeError, json.JSONDecodeError):
            continue
        nodes = payload if isinstance(payload, list) else [payload]
        for node in nodes:
            if not isinstance(node, dict):
                continue
            if node.get("@type") == "Event":
                events.append(node)
            if node.get("@type") == "ItemList":
                for entry in node.get("itemListElement") or []:
                    item = entry.get("item") if isinstance(entry, dict) else None
                    if isinstance(item, dict) and item.get("@type") == "Event":
                        events.append(item)
    return events


def _heuristic_events(html: str, *, venue_label: str, page_url: str) -> list[dict[str, Any]]:
    soup = BeautifulSoup(html, "html.parser")
    events: list[dict[str, Any]] = []
    for link in soup.select("a[href]"):
        href = link.get("href") or ""
        text = link.get_text(" ", strip=True)
        if not text or len(text) < 4:
            continue
        if not any(token in href.lower() for token in ("event", "show", "ticket", "concert")):
            continue
        events.append(
            {
                "name": text[:160],
                "url": href if href.startswith("http") else page_url,
                "venue": venue_label,
            }
        )
        if len(events) >= 20:
            break
    if events:
        return events

    body_text = soup.get_text(" ", strip=True)
    for match in DATE_PATTERN.finditer(body_text):
        events.append(
            {
                "name": f"{venue_label} event ({match.group(0)})",
                "starts_at": match.group(0),
                "url": page_url,
                "venue": venue_label,
            }
        )
        if len(events) >= 5:
            break
    return events


def normalize_venue_event(raw: dict[str, Any], *, venue_key: str, venue_label: str) -> NormalizedEvent:
    ticket_url = str(raw.get("url") or "")
    location = raw.get("location") or {}
    address = location.get("address") or {} if isinstance(location, dict) else {}
    return build_normalized_event(
        source=f"{SOURCE}:{venue_key}",
        provider_event_id=str(raw.get("@id") or raw.get("id") or extract_id_from_url(ticket_url)),
        artist_or_title=str(raw.get("name") or ""),
        venue=str(location.get("name") if isinstance(location, dict) else "") or venue_label,
        city=str(address.get("addressLocality") or "Houston"),
        state=str(address.get("addressRegion") or "TX"),
        starts_at=str(raw.get("startDate") or raw.get("starts_at") or ""),
        ticket_url=ticket_url,
        genre_or_classification="",
        raw_url=ticket_url,
    )


def extract_id_from_url(url: str) -> str:
    match = re.search(r"/(\d{5,})(?:[/?#]|$)", url or "")
    return match.group(1) if match else ""


def _page_title(html: str) -> str:
    title = BeautifulSoup(html, "html.parser").title
    if title is None:
        return ""
    return title.get_text(strip=True)


def probe_venue(key: str, meta: dict[str, str], log: logging.Logger) -> dict[str, Any]:
    url = meta["url"]
    log.info("GET %s (%s)", url, meta["label"])
    try:
        response = http_get_text(url, headers=BROWSER_HEADERS, timeout=30)
        status = response.status_code
        html = response.text
    except Exception as exc:
        return {
            "venue_key": key,
            "label": meta["label"],
            "url": url,
            "error": str(exc),
            "http_status": None,
            "blocked": True,
            "normalized_events": [],
        }

    blocked = status in {403, 406, 429} or "sound check" in html.lower() or "forbidden" in html.lower()
    ld_events = _parse_ld_json_events(html) if status == 200 else []
    heuristic = _heuristic_events(html, venue_label=meta["label"], page_url=url) if status == 200 else []
    raw_events = ld_events or heuristic
    normalized = [
        normalize_venue_event(item, venue_key=key, venue_label=meta["label"])
        for item in raw_events[:10]
    ]

    return {
        "venue_key": key,
        "label": meta["label"],
        "url": url,
        "final_url": response.url,
        "http_status": status,
        "html_bytes": len(html),
        "blocked": blocked,
        "ld_json_count": len(ld_events),
        "heuristic_count": len(heuristic),
        "normalized_events": [event.to_dict() for event in normalized],
        "title": _page_title(html) if status == 200 else "",
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Read-only Houston venue page probe")
    parser.add_argument(
        "--venue",
        choices=sorted(VENUES),
        help="Probe a single venue (default: all configured venues)",
    )
    parser.add_argument("--json", action="store_true", help="Print JSON only")
    parser.add_argument("--artifact-label", default="venues")
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    log = setup_logging("probe_static_venues")
    args = build_parser().parse_args(argv)
    selected = {args.venue: VENUES[args.venue]} if args.venue else VENUES

    results = [probe_venue(key, meta, log) for key, meta in selected.items()]
    flat_events = [
        NormalizedEvent(**event)
        for result in results
        for event in result.get("normalized_events", [])
    ]

    artifact = save_artifact(
        SOURCE,
        args.artifact_label,
        {"venues": results},
        events=flat_events,
        meta={"venue_count": len(results), "result_count": len(flat_events)},
    )

    if args.json:
        print(json.dumps(results, indent=2, ensure_ascii=False))
        return 0

    print(f"source={SOURCE}")
    print(f"venue_count={len(results)}")
    print(f"normalized_count={len(flat_events)}")
    print(f"artifact={artifact.relative_to(REPO_ROOT)}")
    print("venues=")
    print(json.dumps(results, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
