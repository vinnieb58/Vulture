"""Unit tests for Target probe parsing and blocking detection."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from experiments.adapters.target_probe import (
    ProbeListing,
    build_product_url,
    build_search_url,
    decode_title,
    detect_html_blocking,
    detect_redsky_blocking,
    extract_from_dom,
    extract_from_redsky_json,
    extract_listings_from_html,
    has_real_search_results,
    parse_price,
    probe_query,
    redsky_product_to_probe_listing,
)

FIXTURE_JSON = Path(__file__).resolve().parent / "fixtures" / "target_redsky_search_response.json"
FIXTURE_DOM = Path(__file__).resolve().parent / "fixtures" / "target_search_dom_snippet.html"
REDSKY_DATA = json.loads(FIXTURE_JSON.read_text(encoding="utf-8"))
DOM_HTML = FIXTURE_DOM.read_text(encoding="utf-8")

BLOCKED_REDSKY = '{"captchaRelativeURL":"/captcha?trackingId=abc"}'
BLOCKED_HTML = """
<html><head><title>Access Denied</title></head>
<body><div id="px-captcha">Verify</div></body></html>
"""


class TestTargetPriceParsing:
    def test_parse_dollar_string(self):
        assert parse_price("$549.99") == 549

    def test_parse_numeric(self):
        assert parse_price(699.99) == 699

    def test_see_price_in_cart_returns_none(self):
        assert parse_price("See price in cart") is None

    def test_parse_missing(self):
        assert parse_price(None) is None


class TestTargetTitleDecoding:
    def test_decode_html_entities(self):
        assert decode_title("Genexa Kids&#39; Medicine") == "Genexa Kids' Medicine"


class TestTargetProductUrl:
    def test_build_product_url(self):
        assert build_product_url("87767195") == "https://www.target.com/p/-/A-87767195"

    def test_build_search_url(self):
        assert build_search_url("steam deck") == "https://www.target.com/s?searchTerm=steam+deck"


class TestTargetRedskyExtraction:
    def test_has_real_search_results(self):
        assert has_real_search_results(REDSKY_DATA) is True

    def test_no_results_filler(self):
        filler = {
            "data": {
                "search": {
                    "search_response": {"metadata": {"total_results": 200}, "facet_list": []},
                    "products": [{"tcin": "1", "item": {"product_description": {"title": "x"}}}],
                }
            }
        }
        assert has_real_search_results(filler) is False

    def test_redsky_product_to_probe_listing(self):
        product = REDSKY_DATA["data"]["search"]["products"][0]
        listing = redsky_product_to_probe_listing(product)
        assert listing is not None
        assert listing.source == "target"
        assert "Steam Deck" in listing.title
        assert listing.price == 549
        assert "87767195" in listing.link
        assert listing.image is not None

    def test_map_restricted_price_uses_current_retail(self):
        product = REDSKY_DATA["data"]["search"]["products"][1]
        listing = redsky_product_to_probe_listing(product)
        assert listing is not None
        assert listing.price == 699
        assert "89456123" in listing.link

    def test_tcin_fallback_link(self):
        product = REDSKY_DATA["data"]["search"]["products"][3]
        listing = redsky_product_to_probe_listing(product)
        assert listing is not None
        assert listing.link == "https://www.target.com/p/-/A-80000001"

    def test_extract_from_redsky_respects_limit(self):
        listings = extract_from_redsky_json(REDSKY_DATA, limit=1)
        assert len(listings) == 1
        assert isinstance(listings[0], ProbeListing)

    def test_extract_from_redsky_skips_ads(self):
        listings = extract_from_redsky_json(REDSKY_DATA, limit=5)
        assert all("00000000" not in lst.link for lst in listings)


class TestTargetDomExtraction:
    def test_extract_from_dom(self):
        listings = extract_from_dom(DOM_HTML, limit=5)
        assert len(listings) >= 2
        assert listings[0].title is not None
        assert "87767195" in listings[0].link

    def test_extract_listings_from_html_prefers_dom(self):
        listings, method = extract_listings_from_html(DOM_HTML, limit=5)
        assert method == "dom_fallback"
        assert len(listings) >= 1


class TestTargetBlockingDetection:
    def test_redsky_captcha_detected(self):
        blocking = detect_redsky_blocking(403, BLOCKED_REDSKY)
        assert blocking["blocking_detected"] is True
        assert "redsky_captcha" in blocking["triggered_indicators"]

    def test_html_blocking_detected(self):
        blocking = detect_html_blocking(
            403,
            "Access Denied",
            BLOCKED_HTML,
            "https://www.target.com/blocked",
            "https://www.target.com/s?searchTerm=steam+deck",
            listing_count=0,
        )
        assert blocking["blocking_detected"] is True


class TestTargetProbeGracefulFailure:
    @patch("experiments.adapters.target_probe.fetch_redsky")
    @patch("experiments.adapters.target_probe.fetch_requests_html")
    def test_redsky_block_returns_empty(self, mock_html, mock_redsky):
        mock_redsky.return_value = {
            "method": "redsky_api",
            "query": "steam deck",
            "url": "https://redsky.target.com/...",
            "status_code": 403,
            "body": BLOCKED_REDSKY,
            "data": None,
            "elapsed_ms": 100,
            "error": None,
            "visitor_id": "test",
        }
        mock_html.return_value = {
            "method": "requests_html",
            "query": "steam deck",
            "url": "https://www.target.com/s?searchTerm=steam+deck",
            "final_url": "https://www.target.com/s?searchTerm=steam+deck",
            "status_code": 200,
            "redirect_chain": [],
            "html": DOM_HTML,
            "html_length": len(DOM_HTML),
            "elapsed_ms": 200,
            "error": None,
        }
        result = probe_query("steam deck", limit=3, quiet=True)
        assert len(result["listings"]) >= 1

    @patch("experiments.adapters.target_probe.fetch_redsky")
    @patch("experiments.adapters.target_probe.fetch_requests_html")
    def test_all_failures_return_empty_without_crash(self, mock_html, mock_redsky):
        mock_redsky.return_value = {
            "method": "redsky_api",
            "query": "steam deck",
            "url": "https://redsky.target.com/...",
            "status_code": 403,
            "body": BLOCKED_REDSKY,
            "data": None,
            "elapsed_ms": 100,
            "error": None,
            "visitor_id": "test",
        }
        mock_html.return_value = {
            "method": "requests_html",
            "query": "steam deck",
            "url": "https://www.target.com/s?searchTerm=steam+deck",
            "final_url": "https://www.target.com/s?searchTerm=steam+deck",
            "status_code": 200,
            "redirect_chain": [],
            "html": "<html><body></body></html>",
            "html_length": 30,
            "elapsed_ms": 150,
            "error": None,
        }
        result = probe_query("steam deck", limit=3, quiet=True)
        assert result["listings"] == []
        assert result["blocking_detected"] is True
