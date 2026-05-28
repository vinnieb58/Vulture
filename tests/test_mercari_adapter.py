import unittest
from unittest.mock import patch

from adapters.mercari import (
    _extract_items,
    _is_relevant_to_query,
    _normalize_listing,
    _normalize_price,
    search_mercari,
)


class _FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"status={self.status_code}")

    def json(self):
        return self._payload


class _FakeSession:
    def __init__(self):
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append(("get", url, kwargs))
        if url == "https://www.mercari.com/":
            return _FakeResponse(200, {})
        if url.startswith("https://www.mercari.com/search/"):
            return _FakeResponse(200, {})
        if url == "https://www.mercari.com/v1/initialize":
            return _FakeResponse(
                200,
                {
                    "csrf": "test-csrf-token",
                    "accessToken": "test-access-token",
                    "isBot": False,
                },
            )
        if url == "https://www.mercari.com/v1/api":
            return _FakeResponse(
                200,
                {
                    "data": {
                        "search": {
                            "items": [
                                # category/nav noise -> should be filtered (non-m id)
                                {"id": 4, "name": "Home", "price": None},
                                # real listing
                                {
                                    "id": "m91282978869",
                                    "name": "RTX 3080",
                                    "price": 38000,
                                    "thumbnails": ["https://example.com/img1.jpg"],
                                },
                                # real listing with path URL
                                {
                                    "id": "m59843646014",
                                    "name": "Nvidia GeForce RTX 3080 10GB Founders Edition GPU",
                                    "price": 39500,
                                    "url": "/item/m59843646014/",
                                },
                                # outlier title -> should be filtered by query relevance
                                {
                                    "id": "m11111111111",
                                    "name": "Asus Rog 32 oled",
                                    "price": 80000,
                                    "url": "/item/m11111111111/",
                                },
                                # keep broken/parts listing if title remains query-relevant
                                {
                                    "id": "m22222222222",
                                    "name": "MSI Rtx 3080 Ti Gaming Trio Not Working",
                                    "price": 12000,
                                    "url": "/item/m22222222222/",
                                },
                                # malformed/no title -> should skip
                                {"id": "m00000000000", "price": 10000},
                            ]
                        }
                    }
                },
            )
        return _FakeResponse(404, {})


class MercariAdapterTests(unittest.TestCase):
    def test_is_relevant_to_query_examples(self):
        self.assertFalse(_is_relevant_to_query("Asus Rog 32 oled", "rtx 3080"))
        self.assertTrue(_is_relevant_to_query("RTX 3080", "rtx 3080"))
        self.assertTrue(
            _is_relevant_to_query(
                "Nvidia GeForce RTX 3080 10GB Founders Edition GPU",
                "rtx 3080",
            )
        )
        self.assertTrue(
            _is_relevant_to_query(
                "MSI Rtx 3080 Ti Gaming Trio Not Working",
                "rtx 3080",
            )
        )

    def test_normalize_price_minor_units_to_dollars(self):
        self.assertEqual(_normalize_price(38000), 380)
        self.assertEqual(_normalize_price(40613), 406)
        self.assertEqual(_normalize_price(999), 999)
        self.assertIsNone(_normalize_price(None))
        self.assertIsNone(_normalize_price("38000"))

    def test_normalize_listing_filters_noise_and_builds_link(self):
        self.assertIsNone(_normalize_listing({"id": 4, "name": "Home"}))
        good = _normalize_listing({"id": "m123", "name": "RTX 3080", "price": 38000})
        self.assertIsNotNone(good)
        self.assertEqual(good.title, "RTX 3080")
        self.assertEqual(good.price, 380)
        self.assertEqual(good.link, "https://www.mercari.com/item/m123/")

        good2 = _normalize_listing(
            {"id": "m555", "name": "GPU", "price": 10000, "url": "/item/m555/"}
        )
        self.assertEqual(good2.link, "https://www.mercari.com/item/m555/")

    def test_extract_items_prefers_data_search_items(self):
        payload = {"data": {"search": {"items": [{"id": "m1", "name": "x"}]}}}
        items = _extract_items(payload)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["id"], "m1")

    @patch("adapters.mercari.time.sleep", return_value=None)
    @patch("adapters.mercari.requests.Session")
    def test_search_mercari_full_flow_with_mocked_session(self, session_cls, _sleep):
        fake = _FakeSession()
        session_cls.return_value = fake
        results = search_mercari("rtx 3080", limit=5)

        # Query-relevant listings should survive filtering.
        self.assertEqual(len(results), 3)
        self.assertEqual(results[0].source, "mercari")
        self.assertEqual(results[0].title, "RTX 3080")
        self.assertEqual(results[0].price, 380)
        self.assertTrue(results[0].link.startswith("https://www.mercari.com/item/m"))
        self.assertTrue(all("asus rog 32 oled" not in r.title.lower() for r in results))

        # Verify API request uses csrf header and excludes Brotli.
        api_calls = [c for c in fake.calls if c[1] == "https://www.mercari.com/v1/api"]
        self.assertEqual(len(api_calls), 1)
        api_headers = api_calls[0][2]["headers"]
        self.assertEqual(api_headers["x-csrf-token"], "test-csrf-token")
        self.assertEqual(api_headers["Accept-Encoding"], "gzip, deflate")
        self.assertEqual(api_headers["Content-Type"], "application/json")


if __name__ == "__main__":
    unittest.main()
