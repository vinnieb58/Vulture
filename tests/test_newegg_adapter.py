"""Unit tests for the experimental Newegg adapter."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from adapters.newegg import (
    _card_to_listing,
    _parse_price,
    _parse_search_html,
    search_newegg,
)
from adapters.registry import get_adapter, get_capabilities, list_sources
from bs4 import BeautifulSoup
from engine.source_selection import resolve_source_sites, _source_allowed_for_vertical


_CARD_HTML = """
<div class="item-cell">
  <a class="item-title" href="https://www.newegg.com/test-gpu/p/N82E16814126761?Item=N82E16814126761">
    ASUS GeForce RTX 4070 12GB Graphics Card
  </a>
  <li class="price-current"><strong>$</strong>599<strong>.99</strong> –</li>
  <p class="price-ship">Free Shipping</p>
  <p class="item-promo">Limited time offer</p>
  <span class="item-rating-num">(42)</span>
</div>
<div class="item-cell">
  <a class="item-title" href="/broken-relative/p/1FT-00EY-00036">No price card</a>
</div>
<div class="item-cell">
  <div class="item-info">No title link</div>
  <li class="price-current">$100.00</li>
</div>
"""


class TestNeweggPriceParsing:
    def test_parse_simple_price(self):
        assert _parse_price("$599.99") == 599

    def test_parse_split_whitespace_price(self):
        assert _parse_price("$ 669 .99 –") == 669

    def test_parse_comma_price(self):
        assert _parse_price("$ 1,679 .00 –") == 1679

    def test_parse_missing_returns_none(self):
        assert _parse_price(None) is None
        assert _parse_price("") is None
        assert _parse_price("contact seller") is None


class TestNeweggCardParsing:
    def test_card_to_listing_extracts_fields(self):
        soup = BeautifulSoup(_CARD_HTML, "lxml")
        card = soup.select(".item-cell")[0]
        listing = _card_to_listing(card)
        assert listing is not None
        assert listing.source == "newegg"
        assert "RTX 4070" in listing.title
        assert listing.price == 599
        assert listing.location is None
        assert listing.link == "https://www.newegg.com/test-gpu/p/N82E16814126761"

    def test_card_without_title_or_link_skipped(self):
        soup = BeautifulSoup(_CARD_HTML, "lxml")
        assert _card_to_listing(soup.select(".item-cell")[2]) is None

    def test_relative_link_normalized(self):
        soup = BeautifulSoup(_CARD_HTML, "lxml")
        listing = _card_to_listing(soup.select(".item-cell")[1])
        assert listing is not None
        assert listing.link.startswith("https://www.newegg.com/")
        assert listing.price is None

    def test_parse_search_html_respects_limit(self):
        listings = _parse_search_html(_CARD_HTML, limit=1)
        assert len(listings) == 1
        assert listings[0].source == "newegg"

    def test_malformed_html_returns_empty(self):
        assert _parse_search_html("<html><body></body></html>", limit=5) == []


class TestNeweggRegistry:
    def test_registered_in_registry(self):
        assert "newegg" in list_sources()
        assert get_adapter("newegg") is search_newegg

    def test_capabilities_experimental(self):
        caps = get_capabilities("newegg")
        assert caps is not None
        assert caps["stable"] is False
        assert caps["experimental"] is True
        assert caps["requires_browser"] is False
        assert caps["requires_login"] is False
        assert caps["supports_location"] is False
        assert caps["location_control"] == "not_supported"
        assert caps["supports_radius"] is False
        assert caps["supports_price_filter_in_url"] is False
        assert caps["failure_mode"] == "returns_empty_list"

    def test_vertical_mapping(self):
        caps = get_capabilities("newegg") or {}
        verticals = set(caps.get("verticals") or [])
        assert "computer_parts" in verticals
        assert "gaming" in verticals
        assert "electronics" in verticals
        assert "retail" in verticals
        assert "laptops_computers" in verticals

    def test_allowed_for_computer_parts_not_vehicles(self):
        assert _source_allowed_for_vertical("newegg", "computer_parts") is True
        assert _source_allowed_for_vertical("newegg", "vehicles") is False

    def test_in_default_vertical_profiles(self):
        assert "newegg" in resolve_source_sites("computer_parts")
        assert "newegg" in resolve_source_sites("laptops_computers")
        assert resolve_source_sites("computer_parts", explicit_sources=["newegg"]) == ["newegg"]

    def test_vehicle_profile_unchanged(self):
        assert resolve_source_sites("vehicles") == ["craigslist", "carsdotcom", "offerup"]

    def test_tv_profile_unchanged(self):
        assert resolve_source_sites("tv_home_theater") == ["craigslist", "offerup"]


class TestNeweggGracefulFailure:
    @patch("adapters.newegg._fetch_search_html", return_value=None)
    def test_fetch_failure_returns_empty_list(self, _fetch):
        assert search_newegg("rtx 4070", limit=5) == []

    @patch("adapters.newegg._fetch_search_html", return_value=None)
    def test_fetch_failure_does_not_raise(self, _fetch):
        search_newegg("rtx 4070", limit=3)

    @patch("adapters.newegg._parse_search_html", side_effect=RuntimeError("boom"))
    @patch("adapters.newegg._fetch_search_html", return_value="<html></html>")
    def test_parse_exception_returns_empty_list(self, _fetch, _parse):
        assert search_newegg("rtx 4070") == []
