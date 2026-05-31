"""
tests/test_source_selection.py

Vertical source selection (production profiles for personal deployment).
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from engine.llm_translator import translate
from engine.source_selection import resolve_source_sites


def _expand_hunt_sources(hunt: dict) -> list[dict]:
    """Mirror main._expand_hunt_sources for fan-out regression."""
    source_sites = hunt.get("source_sites")
    if not source_sites or len(source_sites) <= 1:
        return [hunt]
    return [{**hunt, "source": site} for site in source_sites]


class TestVerticalProfiles:
    def test_computer_parts_multi_source(self):
        assert resolve_source_sites("computer_parts") == [
            "craigslist", "mercari", "offerup"
        ]

    def test_vehicles_profile(self):
        assert resolve_source_sites("vehicles") == [
            "craigslist", "carsdotcom", "offerup"
        ]

    def test_tv_no_mercari(self):
        assert resolve_source_sites("tv_home_theater") == ["craigslist", "offerup"]

    def test_general_includes_mercari(self):
        assert resolve_source_sites("general") == [
            "craigslist", "offerup", "mercari"
        ]

    def test_carsdotcom_not_on_gpu_vertical(self):
        sites = resolve_source_sites("computer_parts")
        assert "carsdotcom" not in sites

    def test_explicit_sources_override(self):
        sites = resolve_source_sites(
            "general",
            explicit_sources=["craigslist", "offerup"],
        )
        assert sites == ["craigslist", "offerup"]

    def test_bestbuy_not_in_default_computer_parts_profile(self):
        sites = resolve_source_sites("computer_parts")
        assert "bestbuy" not in sites

    def test_bestbuy_not_in_default_laptops_profile(self):
        sites = resolve_source_sites("laptops_computers")
        assert "bestbuy" not in sites

    def test_explicit_bestbuy_source_works(self):
        sites = resolve_source_sites(
            "computer_parts",
            explicit_sources=["bestbuy"],
        )
        assert sites == ["bestbuy"]

    def test_explicit_bestbuy_allowed_for_gaming_vertical_metadata(self):
        from adapters.registry import get_capabilities

        caps = get_capabilities("bestbuy")
        assert caps is not None
        assert "gaming" in caps.get("verticals", [])
        sites = resolve_source_sites(
            "computer_parts",
            explicit_sources=["bestbuy", "craigslist"],
        )
        assert sites == ["bestbuy", "craigslist"]


class TestTranslatorIntegration:
    def test_gpu_multi_source_by_default(self):
        t = translate("rtx 3080 under $400")
        assert t.source_sites == ["craigslist", "mercari", "offerup"]

    def test_vehicle_multi_source(self):
        t = translate("toyota sequoia under 50k miles under $30k")
        assert t.source_sites == ["craigslist", "carsdotcom", "offerup"]


class TestMultiSourceFanOut:
    def test_expand_creates_one_dict_per_source(self):
        hunt = {
            "name": "test",
            "source_sites": ["craigslist", "offerup"],
            "source": "craigslist",
        }
        expanded = _expand_hunt_sources(hunt)
        assert len(expanded) == 2
        assert {h["source"] for h in expanded} == {"craigslist", "offerup"}

    def test_single_source_unchanged(self):
        hunt = {"source_sites": ["craigslist"], "source": "craigslist"}
        assert _expand_hunt_sources(hunt) == [hunt]
