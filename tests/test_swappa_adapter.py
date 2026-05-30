import unittest
from unittest.mock import patch

from adapters.swappa import (
    _card_to_listing,
    _extract_model_slugs,
    _parse_price,
    search_swappa,
)
from bs4 import BeautifulSoup


_SEARCH_HTML = """
<html><body>
  <a href="/listings/macbook-air-2022-13">MacBook Air</a>
  <a href="/listings/steam-deck-oled">Steam Deck OLED</a>
  <a href="/listings/macbook-air-2022-13">duplicate</a>
</body></html>
"""

_LISTINGS_HTML = """
<html><body>
  <div class="xui_card_wrapper" data-code="LAFN63044" data-price="433">
    <div class="headline">Mint condition, barely used</div>
    <div class="ships_from">Austin, TX</div>
    <span itemprop="price" content="433">$433</span>
  </div>
  <div class="xui_card_wrapper" data-code="LAFO45889" data-price="237">
    <meta itemprop="description" content="MacBook Air 2020 13-inch">
    <div class="headline"></div>
  </div>
  <div class="xui_card_wrapper" data-price="100">
    <div class="headline">No code card</div>
  </div>
</body></html>
"""


class SwappaAdapterTests(unittest.TestCase):
    def test_parse_price(self):
        self.assertEqual(_parse_price("433"), 433)
        self.assertEqual(_parse_price("$1,299"), 1299)
        self.assertIsNone(_parse_price(None))
        self.assertIsNone(_parse_price(""))

    def test_extract_model_slugs_dedupes_and_preserves_order(self):
        slugs = _extract_model_slugs(_SEARCH_HTML)
        self.assertEqual(
            slugs,
            ["/listings/macbook-air-2022-13", "/listings/steam-deck-oled"],
        )

    def test_card_to_listing_prefers_headline_and_parses_location(self):
        soup = BeautifulSoup(_LISTINGS_HTML, "lxml")
        wrapper = soup.select(".xui_card_wrapper")[0]
        listing = _card_to_listing(wrapper)
        self.assertIsNotNone(listing)
        self.assertEqual(listing.source, "swappa")
        self.assertEqual(listing.title, "Mint condition, barely used")
        self.assertEqual(listing.price, 433)
        self.assertEqual(listing.location, "Austin, TX")
        self.assertEqual(
            listing.link,
            "https://swappa.com/listing/view/LAFN63044",
        )

    def test_card_to_listing_meta_description_fallback(self):
        soup = BeautifulSoup(_LISTINGS_HTML, "lxml")
        wrapper = soup.select(".xui_card_wrapper")[1]
        listing = _card_to_listing(wrapper)
        self.assertIsNotNone(listing)
        self.assertEqual(listing.title, "MacBook Air 2020 13-inch")
        self.assertEqual(listing.price, 237)
        self.assertIsNone(listing.location)

    def test_card_to_listing_skips_missing_code(self):
        soup = BeautifulSoup(_LISTINGS_HTML, "lxml")
        wrapper = soup.select(".xui_card_wrapper")[2]
        self.assertIsNone(_card_to_listing(wrapper))

    @patch("adapters.swappa._fetch_listings_for_slug")
    @patch("adapters.swappa._fetch_html")
    def test_search_swappa_aggregates_and_dedupes(
        self, mock_fetch_html, mock_fetch_slug
    ):
        mock_fetch_html.return_value = _SEARCH_HTML

        def _slug_listings(slug: str):
            if slug == "/listings/macbook-air-2022-13":
                from models.listing import Listing

                return [
                    Listing(
                        source="swappa",
                        title="Mac A",
                        price=400,
                        location=None,
                        link="https://swappa.com/listing/view/CODE1",
                    ),
                    Listing(
                        source="swappa",
                        title="Mac B",
                        price=300,
                        location="Houston, TX",
                        link="https://swappa.com/listing/view/CODE2",
                    ),
                ]
            return []

        mock_fetch_slug.side_effect = _slug_listings

        results = search_swappa("macbook air", limit=2)
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0].source, "swappa")
        mock_fetch_slug.assert_called_once_with("/listings/macbook-air-2022-13")

    @patch("adapters.swappa._fetch_html")
    def test_search_swappa_returns_empty_when_search_fails(self, mock_fetch_html):
        mock_fetch_html.return_value = None
        self.assertEqual(search_swappa("macbook air"), [])

    @patch("adapters.swappa._fetch_html")
    def test_search_swappa_returns_empty_when_no_slugs(self, mock_fetch_html):
        mock_fetch_html.return_value = "<html><body></body></html>"
        self.assertEqual(search_swappa("unknown gadget xyz"), [])


if __name__ == "__main__":
    unittest.main()
