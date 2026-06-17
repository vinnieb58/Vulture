import unittest
from pathlib import Path
from unittest import mock

from experiments.adapters.facebook_marketplace_probe import (
    BLOCKER_CAPTCHA,
    BLOCKER_EMPTY,
    BLOCKER_LOGIN_WALL,
    BLOCKER_LOCATION_RESOLUTION,
    BLOCKER_UNSUPPORTED,
    build_search_url,
    detect_blockers,
    extract_raw_listings,
    normalize_listing,
    resolve_location_slug,
    run_playwright_probe,
)

FIXTURES = Path(__file__).parent / "fixtures" / "facebook_marketplace"


class FacebookMarketplaceProbeTests(unittest.TestCase):
    def test_resolve_location_slug_known_city(self):
        slug, warning = resolve_location_slug("Houston, TX")
        self.assertEqual(slug, "houston")
        self.assertIsNone(warning)

    def test_resolve_location_slug_best_effort_warning(self):
        slug, warning = resolve_location_slug("Plano, TX")
        self.assertEqual(slug, "plano")
        self.assertIn("known slug table", warning or "")

    def test_build_search_url(self):
        url = build_search_url("steam deck", "houston")
        self.assertEqual(
            url,
            "https://www.facebook.com/marketplace/houston/search/?query=steam+deck",
        )

    def test_extract_listings_from_fixture(self):
        html = (FIXTURES / "search_ssr_sample.html").read_text(encoding="utf-8")
        raw, method = extract_raw_listings(html, limit=5)
        self.assertEqual(method, "json_script_blob")
        self.assertGreaterEqual(len(raw), 2)
        normalized = [normalize_listing(item, "steam deck") for item in raw]
        self.assertEqual(normalized[0].source, "facebook_marketplace")
        self.assertEqual(normalized[0].title, "Valve Steam Deck OLED 1TB")
        self.assertEqual(normalized[0].price, 425)
        self.assertEqual(normalized[0].location, "Houston, Texas")
        self.assertTrue(normalized[0].link.endswith("/marketplace/item/1234567890123456/"))
        self.assertIn("example-steam-deck.jpg", normalized[0].image or "")

    def test_detect_login_wall_blocker(self):
        html = (FIXTURES / "login_wall.html").read_text(encoding="utf-8")
        blockers = detect_blockers(
            html=html,
            final_url="https://www.facebook.com/login/",
            page_title="Log in to Facebook",
            requested_slug="houston",
            listing_count=0,
        )
        self.assertIn(BLOCKER_LOGIN_WALL, blockers)

    def test_detect_location_resolution_failure(self):
        html = '{"params":{"location_id":"category"}}'
        blockers = detect_blockers(
            html=html,
            final_url="https://www.facebook.com/marketplace/category/search/?query=steam+deck",
            page_title="Marketplace",
            requested_slug="houston",
            listing_count=0,
        )
        self.assertIn(BLOCKER_LOCATION_RESOLUTION, blockers)
        self.assertIn(BLOCKER_EMPTY, blockers)

    def test_detect_captcha_blocker(self):
        blockers = detect_blockers(
            html="<html>checkpoint security check</html>",
            final_url="https://www.facebook.com/checkpoint/",
            page_title="Security Check",
            requested_slug="houston",
            listing_count=0,
        )
        self.assertIn(BLOCKER_CAPTCHA, blockers)

    def test_detect_unsupported_page_shape(self):
        blockers = detect_blockers(
            html="<html><body>facebook</body></html>",
            final_url="https://www.facebook.com/",
            page_title="Facebook",
            requested_slug="houston",
            listing_count=0,
        )
        self.assertIn(BLOCKER_UNSUPPORTED, blockers)

    def test_run_playwright_probe_missing_dependency_is_non_fatal(self):
        with mock.patch.dict("sys.modules", {"playwright": None, "playwright.sync_api": None}):
            report = run_playwright_probe("steam deck", "Houston, TX", limit=2)
        self.assertIn("playwright is not installed", report.error or "")
        self.assertIn(BLOCKER_UNSUPPORTED, report.blockers)

    def test_main_json_output(self):
        from experiments.adapters import facebook_marketplace_probe as probe

        with mock.patch.object(
            probe,
            "run_playwright_probe",
            return_value=probe.ProbeReport(
                query="steam deck",
                location="Houston, TX",
                location_slug="houston",
                search_url=build_search_url("steam deck", "houston"),
                listings=[
                    probe.NormalizedListing(
                        source="facebook_marketplace",
                        query="steam deck",
                        title="Steam Deck",
                        price=400,
                        location="Houston, TX",
                        link="https://www.facebook.com/marketplace/item/1/",
                        image=None,
                    )
                ],
            ),
        ):
            with mock.patch("sys.argv", ["probe", "--query", "steam deck", "--json"]):
                exit_code = probe.main()
                self.assertEqual(exit_code, 0)


if __name__ == "__main__":
    unittest.main()
