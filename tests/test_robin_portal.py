"""Unit tests for Robin portal HTML discovery helpers."""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from robin.portal import discover_photo_candidates_from_html


SAMPLE_HTML = """
<html><body>
  <div data-date="2026-06-10">
    <img src="/static/logo-icon.png" alt="logo">
    <img src="https://portal.example.com/media/kids/2026-06-10/lunch.jpg" alt="Lunch">
  </div>
  <img data-src="https://portal.example.com/media/kids/2026-06-12/nap.jpeg">
  <a href="https://portal.example.com/media/kids/old-2019-01-01.jpg">old</a>
</body></html>
"""


class TestRobinPortalDiscovery:
    def test_discovers_image_urls_from_html(self) -> None:
        candidates = discover_photo_candidates_from_html(
            SAMPLE_HTML,
            base_url="https://portal.example.com/photos",
        )
        urls = {candidate.url for candidate in candidates}
        assert "https://portal.example.com/media/kids/2026-06-10/lunch.jpg" in urls
        assert "https://portal.example.com/media/kids/2026-06-12/nap.jpeg" in urls
        assert not any("logo-icon" in url for url in urls)

    def test_since_date_filters_detected_dates(self) -> None:
        candidates = discover_photo_candidates_from_html(
            SAMPLE_HTML,
            base_url="https://portal.example.com/photos",
            since_date=date(2026, 6, 11),
        )
        urls = {candidate.url for candidate in candidates}
        assert "https://portal.example.com/media/kids/2026-06-12/nap.jpeg" in urls
        assert "https://portal.example.com/media/kids/2026-06-10/lunch.jpg" not in urls
        assert "https://portal.example.com/media/kids/old-2019-01-01.jpg" not in urls

    def test_parses_iso_date_near_image(self) -> None:
        candidates = discover_photo_candidates_from_html(
            SAMPLE_HTML,
            base_url="https://portal.example.com/photos",
        )
        by_url = {candidate.url: candidate for candidate in candidates}
        lunch = by_url["https://portal.example.com/media/kids/2026-06-10/lunch.jpg"]
        assert lunch.detected_date == date(2026, 6, 10)
