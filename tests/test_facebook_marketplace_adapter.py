"""Unit tests for the experimental Facebook Marketplace adapter."""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from adapters.facebook_marketplace import (
    _normalized_to_listing,
    parse_search_html,
    search_facebook_marketplace,
)
from adapters.registry import get_adapter, get_capabilities, list_sources
from engine.source_selection import resolve_source_sites
from experiments.adapters.facebook_marketplace_probe import (
    BLOCKER_CAPTCHA,
    BLOCKER_LOGIN_WALL,
    normalize_listing,
    extract_raw_listings,
)

FIXTURES = Path(__file__).parent / "fixtures" / "facebook_marketplace"


class TestFacebookMarketplaceExtraction:
    def test_normalized_extraction_from_ssr_fixture(self):
        html = (FIXTURES / "search_ssr_sample.html").read_text(encoding="utf-8")
        listings, blockers = parse_search_html(
            html,
            "steam deck",
            final_url="https://www.facebook.com/marketplace/houston/search/?query=steam+deck",
            page_title="Facebook Marketplace",
            requested_slug="houston",
            limit=5,
        )
        assert len(listings) >= 2
        first = listings[0]
        assert first.source == "facebook_marketplace"
        assert first.title == "Valve Steam Deck OLED 1TB"
        assert first.price == 425
        assert first.location == "Houston, Texas"
        assert first.link.endswith("/marketplace/item/1234567890123456/")
        assert not blockers or BLOCKER_LOGIN_WALL not in blockers

    def test_probe_normalize_includes_optional_image_and_query(self):
        html = (FIXTURES / "search_ssr_sample.html").read_text(encoding="utf-8")
        raw, method = extract_raw_listings(html, limit=5)
        assert method == "json_script_blob"
        normalized = normalize_listing(raw[0], "steam deck")
        assert normalized.query == "steam deck"
        assert normalized.image is not None
        assert "example-steam-deck.jpg" in normalized.image

    def test_missing_location_allowed_in_listing(self):
        normalized = normalize_listing(
            {
                "id": "999",
                "marketplace_listing_title": "Generic item",
                "listing_price": {"formatted_amount": "$10"},
            },
            "test query",
        )
        listing = _normalized_to_listing(normalized)
        assert listing is not None
        assert listing.location is None
        assert listing.title == "Generic item"


class TestFacebookMarketplaceBlockers:
    def test_blockers_with_listings_returns_listings_and_logs_warning(self, caplog):
        html = (FIXTURES / "search_ssr_sample.html").read_text(encoding="utf-8")
        with caplog.at_level(logging.WARNING):
            listings, blockers = parse_search_html(
                html,
                "steam deck",
                final_url="https://www.facebook.com/login/",
                page_title="Log in to Facebook",
                requested_slug="houston",
                limit=5,
            )
        assert BLOCKER_LOGIN_WALL in blockers
        assert len(listings) >= 2

        with caplog.at_level(logging.WARNING):
            with patch(
                "adapters.facebook_marketplace._fetch_search_html",
                return_value=(
                    html,
                    "https://www.facebook.com/login/",
                    "Log in to Facebook",
                    "houston",
                ),
            ):
                result = search_facebook_marketplace("steam deck", city="Houston, TX")
        assert len(result) >= 2
        assert any("login_wall" in rec.message for rec in caplog.records)

    def test_blockers_without_listings_returns_empty(self, caplog):
        html = (FIXTURES / "login_wall.html").read_text(encoding="utf-8")
        with caplog.at_level(logging.WARNING):
            listings, blockers = parse_search_html(
                html,
                "steam deck",
                final_url="https://www.facebook.com/login/",
                page_title="Log in to Facebook",
                requested_slug="houston",
                limit=5,
            )
        assert BLOCKER_LOGIN_WALL in blockers
        assert listings == []

        with caplog.at_level(logging.WARNING):
            with patch(
                "adapters.facebook_marketplace._fetch_search_html",
                return_value=(
                    html,
                    "https://www.facebook.com/login/",
                    "Log in to Facebook",
                    "houston",
                ),
            ):
                result = search_facebook_marketplace("steam deck", city="Houston, TX")
        assert result == []
        assert any("login_wall" in rec.message for rec in caplog.records)

    def test_captcha_blocker_with_no_listings_returns_empty(self):
        html = "<html>checkpoint security check</html>"
        listings, blockers = parse_search_html(
            html,
            "rtx 4070",
            final_url="https://www.facebook.com/checkpoint/",
            page_title="Security Check",
            requested_slug="houston",
            limit=5,
        )
        assert BLOCKER_CAPTCHA in blockers
        assert listings == []


class TestFacebookMarketplaceRegistry:
    def test_registered_as_experimental(self):
        assert "facebook_marketplace" in list_sources()
        assert get_adapter("facebook_marketplace") is search_facebook_marketplace

    def test_capabilities_experimental_flaky_browser_sensitive(self):
        caps = get_capabilities("facebook_marketplace")
        assert caps is not None
        assert caps["stable"] is False
        assert caps["experimental"] is True
        assert caps["flaky"] is True
        assert caps["browser_sensitive"] is True
        assert caps["blocking_risk"] == "login_captcha_checkpoint"
        assert caps["requires_browser"] is True
        assert caps["requires_login"] is False
        assert caps.get("default_profile_allowed") is True
        assert caps.get("explicit_opt_in_only") is not True
        assert caps["failure_mode"] == "returns_empty_list"

    def test_in_default_vertical_profiles(self):
        for vertical in (
            "computer_parts",
            "gaming",
            "electronics",
            "vehicles",
            "tv_home_theater",
            "general",
            "general_marketplace",
            "phones_tablets",
            "laptops_computers",
            "furniture_home",
        ):
            assert "facebook_marketplace" in resolve_source_sites(vertical)

    def test_excluded_from_retail_profile(self):
        assert "facebook_marketplace" not in resolve_source_sites("retail")

    def test_explicit_source_sites_override(self):
        sites = resolve_source_sites(
            "general",
            explicit_sources=["facebook_marketplace", "craigslist"],
        )
        assert sites == ["facebook_marketplace", "craigslist"]

    def test_no_credentials_or_session_support(self):
        import inspect

        from adapters import facebook_marketplace as mod

        fetch_source = inspect.getsource(mod._fetch_search_html)
        assert "storage_state" not in fetch_source
        assert "add_cookies" not in fetch_source
        caps = get_capabilities("facebook_marketplace")
        assert caps is not None
        assert caps["requires_login"] is False


class TestFacebookMarketplaceAdapterKwargs:
    def test_accepts_common_hunt_kwargs_without_raising(self):
        html = (FIXTURES / "search_ssr_sample.html").read_text(encoding="utf-8")
        with patch(
            "adapters.facebook_marketplace._fetch_search_html",
            return_value=(
                html,
                "https://www.facebook.com/marketplace/houston/search/?query=steam+deck",
                "Facebook Marketplace",
                "houston",
            ),
        ):
            result = search_facebook_marketplace(
                "steam deck",
                city="Houston, TX",
                limit=5,
                max_price=500,
                min_price=50,
                radius=25,
                condition="used",
            )
        assert len(result) >= 2


class TestFacebookMarketplaceAdapterKwargs:
    def test_accepts_common_hunt_kwargs_without_raising(self):
        html = (FIXTURES / "search_ssr_sample.html").read_text(encoding="utf-8")
        with patch(
            "adapters.facebook_marketplace._fetch_search_html",
            return_value=(
                html,
                "https://www.facebook.com/marketplace/houston/search/?query=steam+deck",
                "Facebook Marketplace",
                "houston",
            ),
        ):
            result = search_facebook_marketplace(
                "steam deck",
                city="Houston, TX",
                limit=5,
                max_price=500,
                min_price=50,
                radius=25,
                condition="used",
            )
        assert len(result) >= 2


class TestFacebookMarketplaceGracefulFailure:
    @patch("adapters.facebook_marketplace._fetch_search_html", return_value=(None, None, None, None))
    def test_fetch_failure_returns_empty_list(self, _fetch):
        assert search_facebook_marketplace("steam deck") == []

    @patch("adapters.facebook_marketplace._fetch_search_html", return_value=(None, None, None, None))
    def test_fetch_failure_does_not_raise(self, _fetch):
        search_facebook_marketplace("steam deck", limit=3)
