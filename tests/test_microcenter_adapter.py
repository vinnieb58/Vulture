"""Unit tests for Micro Center adapter (no live network)."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from adapters.microcenter import (
    build_search_url,
    card_to_listing,
    is_page_blocked,
    parse_listings_from_html,
    parse_price_int,
    resolve_storeid,
    search_microcenter,
    summarize_availability,
)
from adapters.registry import get_adapter, get_capabilities, list_sources
from engine.source_selection import resolve_source_sites
from bs4 import BeautifulSoup

FIXTURES = Path(__file__).resolve().parent / "fixtures"


class TestMicrocenterRegistry:
    def test_registered_experimental(self):
        assert "microcenter" in list_sources()
        caps = get_capabilities("microcenter")
        assert caps is not None
        assert caps["stable"] is False
        assert caps["experimental"] is True
        assert caps["requires_browser"] is True
        assert caps["requires_login"] is False
        assert caps["supports_location"] is True
        assert caps["location_control"] == "storeid"
        assert caps["supports_radius"] is False
        assert caps["supports_price_filter_in_url"] is False
        assert "retail" in caps["verticals"]
        assert get_adapter("microcenter") is not None

    def test_not_default_computer_parts_source(self):
        sites = resolve_source_sites("computer_parts")
        assert "microcenter" not in sites


class TestMicrocenterUrlAndStore:
    def test_build_search_url_with_storeid(self):
        url = build_search_url("rtx 4070", "115")
        assert "Ntt=rtx+4070" in url or "Ntt=rtx%204070" in url
        assert "Ntk=all" in url
        assert "sortby=match" in url
        assert "storeid=115" in url

    def test_resolve_storeid_explicit(self):
        assert resolve_storeid(None, 141) == "141"
        assert resolve_storeid("houston", "155") == "155"
        assert resolve_storeid("houston", None) == "155"

    def test_resolve_storeid_city_name(self):
        assert resolve_storeid("brooklyn", None) == "115"

    def test_resolve_storeid_default(self):
        assert resolve_storeid(None, None) == "141"


class TestMicrocenterParsing:
    def test_parse_price_int(self):
        assert parse_price_int("$349.99") == 349
        assert parse_price_int("349.99") == 349
        assert parse_price_int("") is None

    def test_summarize_availability_strips_quick_view(self):
        raw = "25+ IN STOCK at Brooklyn Store QUICK VIEW Ryzen 7 7800X3D"
        assert "QUICK VIEW" not in (summarize_availability(raw) or "")

    def test_card_to_listing_from_fixture(self):
        html = (FIXTURES / "microcenter_product_card.html").read_text(encoding="utf-8")
        card = BeautifulSoup(html, "lxml").select_one("li.product_wrapper")
        listing = card_to_listing(card, store_id="115")
        assert listing is not None
        assert listing.source == "microcenter"
        assert "7800X3D" in listing.title
        assert listing.price == 349
        assert listing.link.startswith("https://www.microcenter.com/product/674503")
        assert listing.location is not None
        assert "Brooklyn" in listing.location

    def test_parse_listings_from_html_snippet(self):
        html = (FIXTURES / "microcenter_search_snippet.html").read_text(encoding="utf-8")
        listings = parse_listings_from_html(html, limit=5, store_id="115")
        assert len(listings) == 1
        assert listings[0].price == 349


class TestMicrocenterBlockDetection:
    def test_blocked_just_a_moment_title(self):
        blocked, reason = is_page_blocked("Just a moment...", "<html></html>", 0)
        assert blocked is True
        assert "challenge_title" in reason

    def test_not_blocked_with_products(self):
        html = (FIXTURES / "microcenter_search_snippet.html").read_text(encoding="utf-8")
        blocked, _ = is_page_blocked("ryzen : Micro Center", html, 1)
        assert blocked is False


class TestMicrocenterGracefulFailure:
    @patch("adapters.microcenter._fetch_html", return_value=(None, "", 0))
    def test_fetch_failure_returns_empty(self, _fetch):
        assert search_microcenter("rtx 4070", storeid=141, limit=5) == []

    @patch("adapters.microcenter._fetch_html", return_value=(None, "", 0))
    def test_fetch_failure_does_not_raise(self, _fetch):
        search_microcenter("ryzen 7 7800x3d", limit=3)

    @patch(
        "adapters.microcenter._fetch_html",
        return_value=("Just a moment...", "Just a moment...", 0),
    )
    def test_challenge_page_returns_empty(self, _fetch):
        results = search_microcenter("rtx 4070", storeid=141, limit=5)
        assert results == []

    @patch("adapters.microcenter._fetch_html")
    def test_success_path_uses_parser(self, mock_fetch):
        html = (FIXTURES / "microcenter_search_snippet.html").read_text(encoding="utf-8")
        mock_fetch.return_value = (html, "ryzen : Micro Center", 1)
        results = search_microcenter("ryzen 7 7800x3d", storeid=115, limit=5)
        assert len(results) == 1
        assert results[0].source == "microcenter"
