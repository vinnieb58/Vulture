"""Unit tests for Walmart probe parsing and blocking detection."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from experiments.adapters.walmart_probe import (
    ProbeListing,
    build_search_url,
    detect_blocking,
    extract_from_dom,
    extract_from_next_data,
    extract_items_from_next_data,
    extract_listings,
    extract_next_data_blob,
    item_dict_to_probe_listing,
    parse_price,
    normalize_link,
    probe_query,
)
from unittest.mock import patch

FIXTURE_PATH = Path(__file__).resolve().parent / "fixtures" / "walmart_search_next_data_snippet.html"
FIXTURE_HTML = FIXTURE_PATH.read_text(encoding="utf-8")

BLOCKED_HTML = """
<html><head><title>Robot or human?</title></head>
<body><div id="px-captcha">Verify</div></body></html>
"""


class TestWalmartPriceParsing:
    def test_parse_dollar_string(self):
        assert parse_price("$549.00") == 549

    def test_parse_numeric(self):
        assert parse_price(699.99) == 699

    def test_parse_missing(self):
        assert parse_price(None) is None
        assert parse_price("contact seller") is None


class TestWalmartLinkNormalization:
    def test_relative_canonical(self):
        assert (
            normalize_link("/ip/steam-deck/5012345678")
            == "https://www.walmart.com/ip/steam-deck/5012345678"
        )

    def test_item_id_fallback(self):
        assert normalize_link(None, item_id="5012345678") == "https://www.walmart.com/ip/5012345678"


class TestWalmartNextDataExtraction:
    def test_extract_next_data_blob(self):
        data = extract_next_data_blob(FIXTURE_HTML)
        assert data is not None
        assert "props" in data

    def test_extract_items_filters_non_products(self):
        data = extract_next_data_blob(FIXTURE_HTML)
        items = extract_items_from_next_data(data)
        typenames = {i.get("__typename") for i in items}
        assert "Product" in typenames
        assert "AdPlaceholder" in typenames

    def test_item_dict_to_probe_listing(self):
        data = extract_next_data_blob(FIXTURE_HTML)
        products = [
            i for i in extract_items_from_next_data(data) if i.get("__typename") == "Product"
        ]
        listing = item_dict_to_probe_listing(products[0])
        assert listing is not None
        assert listing.source == "walmart"
        assert "Steam Deck" in listing.title
        assert listing.price == 549
        assert listing.location is not None
        assert "5012345678" in listing.link
        assert listing.image is not None

    def test_item_with_price_field_only(self):
        item = {
            "__typename": "Product",
            "name": "ASUS ROG Ally",
            "usItemId": "5098765432",
            "canonicalUrl": "/ip/asus-rog-ally/5098765432",
            "price": 699.99,
            "fulfillmentBadge": "Pickup today",
        }
        listing = item_dict_to_probe_listing(item)
        assert listing is not None
        assert listing.price == 699
        assert listing.location == "Pickup today"

    def test_broken_item_skipped(self):
        item = {"__typename": "Product", "name": "No link or id"}
        assert item_dict_to_probe_listing(item) is None

    def test_item_id_fallback_link(self):
        item = {"__typename": "Product", "name": "Deck only id", "usItemId": "5000000001"}
        listing = item_dict_to_probe_listing(item)
        assert listing is not None
        assert listing.link == "https://www.walmart.com/ip/5000000001"

    def test_extract_from_next_data_respects_limit(self):
        listings = extract_from_next_data(FIXTURE_HTML, limit=1)
        assert len(listings) == 1
        assert isinstance(listings[0], ProbeListing)

    def test_extract_listings_prefers_json(self):
        listings, method = extract_listings(FIXTURE_HTML, limit=5)
        assert method == "next_data_json"
        assert len(listings) == 2


class TestWalmartDomFallback:
    def test_extract_from_dom(self):
        listings = extract_from_dom(FIXTURE_HTML, limit=5)
        assert len(listings) >= 1
        assert listings[0].title is not None
        assert "5012345678" in listings[0].link


class TestWalmartBlockingDetection:
    def test_blocked_page_detected(self):
        blocking = detect_blocking(
            200,
            "Robot or human?",
            BLOCKED_HTML,
            "https://www.walmart.com/blocked?url=abc",
            "https://www.walmart.com/search?q=steam+deck",
            listing_count=0,
        )
        assert blocking["blocking_detected"] is True
        assert any("blocked" in t or "robot" in t for t in blocking["triggered_indicators"])

    def test_fixture_not_flagged_as_blocked(self):
        listings, _ = extract_listings(FIXTURE_HTML, limit=5)
        blocking = detect_blocking(
            200,
            "steam deck - Walmart.com",
            FIXTURE_HTML,
            "https://www.walmart.com/search?q=steam+deck",
            "https://www.walmart.com/search?q=steam+deck",
            listing_count=len(listings),
        )
        assert blocking["blocking_detected"] is False


class TestWalmartSearchUrl:
    def test_build_search_url(self):
        url = build_search_url("steam deck")
        assert url == "https://www.walmart.com/search?q=steam+deck"


class TestWalmartProbeGracefulFailure:
    @patch("experiments.adapters.walmart_probe.fetch_query")
    def test_fetch_error_returns_empty(self, mock_fetch):
        mock_fetch.return_value = {
            "method": "requests",
            "query": "steam deck",
            "url": "https://www.walmart.com/search?q=steam+deck",
            "final_url": "https://www.walmart.com/blocked",
            "status_code": None,
            "redirect_chain": [],
            "html": "",
            "html_length": 0,
            "elapsed_ms": 100,
            "error": "request_timeout",
        }
        result = probe_query("steam deck", limit=3, quiet=True)
        assert result["listings"] == []
        assert result["blocking_detected"] is True
        assert result["error"] == "request_timeout"

    @patch("experiments.adapters.walmart_probe.fetch_query")
    def test_blocked_html_returns_empty_listings(self, mock_fetch):
        mock_fetch.return_value = {
            "method": "requests",
            "query": "steam deck",
            "url": "https://www.walmart.com/search?q=steam+deck",
            "final_url": "https://www.walmart.com/blocked?url=abc",
            "status_code": 200,
            "redirect_chain": [],
            "html": BLOCKED_HTML,
            "html_length": len(BLOCKED_HTML),
            "elapsed_ms": 200,
            "error": None,
        }
        result = probe_query("steam deck", limit=3, quiet=True)
        assert result["listings"] == []
        assert result["blocking_detected"] is True

    @patch("experiments.adapters.walmart_probe.fetch_query")
    def test_probe_does_not_raise_on_parse_failure(self, mock_fetch):
        mock_fetch.return_value = {
            "method": "requests",
            "query": "steam deck",
            "url": "https://www.walmart.com/search?q=steam+deck",
            "final_url": "https://www.walmart.com/search?q=steam+deck",
            "status_code": 200,
            "redirect_chain": [],
            "html": "<html><body>garbage</body></html>",
            "html_length": 30,
            "elapsed_ms": 150,
            "error": None,
        }
        result = probe_query("steam deck", limit=3, quiet=True)
        assert result["listings"] == []
