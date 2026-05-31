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
            "craigslist", "mercari", "offerup", "microcenter",
        ]

    def test_laptops_computers_includes_microcenter(self):
        # Craigslist/OfferUp omit laptops_computers in registry verticals (pre-existing).
        sites = resolve_source_sites("laptops_computers")
        assert sites == ["mercari", "microcenter"]

    def test_vehicles_profile(self):
        sites = resolve_source_sites("vehicles")
        assert "microcenter" not in sites
        assert sites == ["craigslist", "carsdotcom", "offerup"]

    def test_tv_no_mercari(self):
        sites = resolve_source_sites("tv_home_theater")
        assert "microcenter" not in sites
        assert sites == ["craigslist", "offerup"]

    def test_general_includes_mercari(self):
        sites = resolve_source_sites("general")
        assert "microcenter" not in sites
        assert sites == ["craigslist", "offerup", "mercari"]

    def test_carsdotcom_not_on_gpu_vertical(self):
        sites = resolve_source_sites("computer_parts")
        assert "carsdotcom" not in sites

    def test_explicit_sources_override(self):
        sites = resolve_source_sites(
            "general",
            explicit_sources=["craigslist", "offerup"],
        )
        assert sites == ["craigslist", "offerup"]


class TestTranslatorIntegration:
    def test_gpu_multi_source_by_default(self):
        t = translate("rtx 3080 under $400")
        assert t.source_sites == [
            "craigslist", "mercari", "offerup", "microcenter",
        ]

    def test_ryzen_cpu_includes_microcenter(self):
        t = translate("ryzen 7 7800x3d under $400")
        assert "microcenter" in t.source_sites

    def test_gaming_laptop_includes_microcenter(self):
        t = translate("gaming laptop under $800")
        assert "microcenter" in t.source_sites

    def test_vehicle_multi_source(self):
        t = translate("toyota sequoia under 50k miles under $30k")
        assert "microcenter" not in t.source_sites
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
