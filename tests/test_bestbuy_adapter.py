"""Unit tests for Best Buy adapter parsing and registry metadata."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from adapters.bestbuy import (
    _card_to_listing,
    _parse_listings,
    _parse_price,
    search_bestbuy,
)
from adapters.registry import get_capabilities
from bs4 import BeautifulSoup


_LIST_ITEM_HTML = """
<html><body>
  <div class="list-item">
    <a class="sku-title" href="https://www.bestbuy.com/product/nvidia-geforce-rtx-4070/abc/sku/123456">
      <span class="nc-product-title">NVIDIA GeForce RTX 4070 12GB</span>
    </a>
    <span class="font-500">$599.99</span>
  </div>
  <div class="list-item">
    <a class="sku-title" href="https://www.bestbuy.com/product/bad-card/no-title/sku/999">
      <span class="nc-product-title"></span>
    </a>
    <span class="font-500">$100.00</span>
  </div>
</body></html>
"""

_PRODUCT_LIST_HTML = """
<html><body>
  <li class="product-list-item">
    <a class="product-list-item-link"
       href="https://www.bestbuy.com/product/apple-macbook-air-13/JJGCQLKXL7">
      Apple - MacBook Air 13-inch Laptop - M5
    </a>
    <span class="font-500">$949.00</span>
    <span class="fulfillment-pickup">Pick up in 1 hour</span>
  </li>
  <li class="product-list-item">
    <span class="font-500">$499.00</span>
  </li>
</body></html>
"""


class TestBestbuyParsePrice:
    def test_dollar_with_cents_truncates(self):
        assert _parse_price("$599.99") == 599

    def test_comma_thousands(self):
        assert _parse_price("$1,299.00") == 1299

    def test_missing_returns_none(self):
        assert _parse_price(None) is None
        assert _parse_price("") is None
        assert _parse_price("call for price") is None


class TestBestbuyCardParsing:
    def test_list_item_layout(self):
        soup = BeautifulSoup(_LIST_ITEM_HTML, "lxml")
        card = soup.select_one(".list-item")
        listing = _card_to_listing(card)
        assert listing is not None
        assert listing.source == "bestbuy"
        assert listing.title == "NVIDIA GeForce RTX 4070 12GB"
        assert listing.price == 599
        assert listing.link.startswith("https://www.bestbuy.com/product/")
        assert listing.location is None

    def test_product_list_layout_with_pickup(self):
        soup = BeautifulSoup(_PRODUCT_LIST_HTML, "lxml")
        card = soup.select_one("li.product-list-item")
        listing = _card_to_listing(card)
        assert listing is not None
        assert "MacBook Air" in listing.title
        assert listing.price == 949
        assert listing.location == "Pick up in 1 hour"

    def test_skips_card_without_title(self):
        soup = BeautifulSoup(_LIST_ITEM_HTML, "lxml")
        cards = soup.select(".list-item")
        assert _card_to_listing(cards[1]) is None

    def test_skips_card_without_link(self):
        html = """
        <li class="product-list-item">
          <span class="font-500">$499.00</span>
        </li>
        """
        soup = BeautifulSoup(html, "lxml")
        assert _card_to_listing(soup.select_one("li")) is None

    def test_malformed_card_does_not_crash_parse_listings(self):
        html = """
        <html><body>
          <div class="list-item"><!-- empty --></div>
          <div class="list-item">
            <a class="sku-title" href="https://www.bestbuy.com/product/ok/x/sku/1">
              <span class="nc-product-title">Valid GPU</span>
            </a>
            <span class="font-500">$400.00</span>
          </div>
        </body></html>
        """
        listings = _parse_listings(html, limit=5)
        assert len(listings) == 1
        assert listings[0].title == "Valid GPU"


class TestBestbuyRegistry:
    def test_marked_experimental_browser_required(self):
        caps = get_capabilities("bestbuy")
        assert caps is not None
        assert caps.get("stable") is False
        assert caps.get("experimental") is True
        assert caps.get("requires_browser") is True
        assert caps.get("requires_login") is False
        assert caps.get("supports_location") is False
        assert caps.get("location_control") == "not_supported"
        assert caps.get("failure_mode") == "returns_empty_list"
        assert "computer_parts" in caps.get("verticals", [])
        assert "laptops_computers" in caps.get("verticals", [])
        assert "retail" in caps.get("verticals", [])


class TestBestbuyGracefulFailure:
    @patch("adapters.bestbuy._fetch_html", return_value=None)
    def test_fetch_failure_returns_empty_list(self, _fetch):
        assert search_bestbuy("rtx 4070", limit=5) == []

    @patch("adapters.bestbuy._fetch_html", return_value=None)
    def test_fetch_failure_does_not_raise(self, _fetch):
        search_bestbuy("macbook air", limit=3)

    @patch("adapters.bestbuy._fetch_html", return_value="<html><body></body></html>")
    def test_empty_html_returns_empty_list(self, _fetch):
        assert search_bestbuy("gaming laptop", limit=5) == []

    @patch("adapters.bestbuy._fetch_html", return_value=_LIST_ITEM_HTML)
    def test_search_returns_listings(self, _fetch):
        results = search_bestbuy("rtx 4070", limit=5)
        assert len(results) == 1
        assert results[0].source == "bestbuy"
        assert results[0].price == 599
