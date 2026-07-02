"""Tests for Vulture Concerts area presets."""

from __future__ import annotations

import pytest

from engine.concerts.areas import (
    SUPPORTED_AREAS,
    normalize_area_name,
    resolve_geo_searches,
)


class TestAreaPresets:
    def test_houston_radius(self):
        searches = resolve_geo_searches(area="houston")
        assert len(searches) == 1
        assert searches[0].radius_miles == 75
        assert searches[0].state == "TX"
        assert searches[0].lat is not None

    def test_dallas_radius(self):
        searches = resolve_geo_searches(area="dallas")
        assert searches[0].radius_miles == 75

    def test_austin_radius(self):
        searches = resolve_geo_searches(area="austin")
        assert searches[0].radius_miles == 60

    def test_east_texas_multi_city(self):
        searches = resolve_geo_searches(area="east texas")
        cities = {s.city for s in searches}
        assert "Tyler" in cities
        assert "Beaumont" in cities
        assert len(searches) >= 5

    def test_louisiana_multi_city(self):
        searches = resolve_geo_searches(area="louisiana")
        cities = {s.city for s in searches}
        assert "New Orleans" in cities
        assert "Baton Rouge" in cities

    def test_texas_fans_out(self):
        searches = resolve_geo_searches(area="texas")
        labels = " ".join(s.label for s in searches).lower()
        assert "houston" in labels
        assert "dallas" in labels
        assert "tyler" in labels or "east texas" in labels

    def test_nationwide_empty(self):
        assert resolve_geo_searches(area="nationwide") == []

    def test_explicit_city_radius(self):
        searches = resolve_geo_searches(city="New Orleans", state="LA", radius_miles=100)
        assert len(searches) == 1
        assert searches[0].city == "New Orleans"
        assert searches[0].radius_miles == 100

    def test_supported_areas_complete(self):
        assert "houston" in SUPPORTED_AREAS
        assert "nationwide" in SUPPORTED_AREAS

    def test_normalize_area(self):
        assert normalize_area_name("  East   Texas  ") == "east texas"

    def test_unknown_area_raises(self):
        with pytest.raises(ValueError, match="Unknown area"):
            resolve_geo_searches(area="mars")
