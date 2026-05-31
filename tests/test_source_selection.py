"""
tests/test_source_selection.py

Vertical source selection (production profiles for personal deployment).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from adapters.registry import list_sources
from engine.llm_translator import translate
from engine.source_selection import (
    INCLUDE_EXPERIMENTAL_COMPUTER_RETAIL_DEFAULTS,
    _VERTICAL_PROFILES,
    resolve_source_sites,
)

os.environ.setdefault("VULTURE_TRANSLATOR", "rules")


def _expand_hunt_sources(hunt: dict) -> list[dict]:
    """Mirror main._expand_hunt_sources for fan-out regression."""
    source_sites = hunt.get("source_sites")
    if not source_sites or len(source_sites) <= 1:
        return [hunt]
    return [{**hunt, "source": site} for site in source_sites]


_COMPUTER_ELECTRONICS_EXPECTED = [
    "craigslist",
    "mercari",
    "offerup",
    "microcenter",
    "newegg",
    "bestbuy",
    "swappa",
]


class TestExperimentalDefaultsFlag:
    def test_include_experimental_computer_retail_defaults_enabled(self):
        assert INCLUDE_EXPERIMENTAL_COMPUTER_RETAIL_DEFAULTS is True

    def test_computer_profile_lists_retail_sources(self):
        profile = _VERTICAL_PROFILES["computer_parts"]
        for src in ("newegg", "bestbuy", "swappa"):
            assert src in profile


class TestVerticalProfiles:
    def test_computer_parts_includes_retail_computer_sources(self):
        sites = resolve_source_sites("computer_parts")
        for src in _COMPUTER_ELECTRONICS_EXPECTED:
            if src in list_sources():
                assert src in sites, f"{src} registered but missing from computer_parts"

    def test_gaming_includes_newegg_and_bestbuy(self):
        sites = resolve_source_sites("gaming")
        assert "newegg" in sites
        assert "bestbuy" in sites
        assert "swappa" in sites

    def test_electronics_includes_retail_computer_sources(self):
        sites = resolve_source_sites("electronics")
        assert "newegg" in sites
        assert "bestbuy" in sites
        assert "swappa" in sites

    def test_laptops_computers_includes_newegg_and_bestbuy(self):
        sites = resolve_source_sites("laptops_computers")
        assert "newegg" in sites
        assert "bestbuy" in sites
        assert "swappa" in sites
        assert "microcenter" in sites

    def test_retail_defaults_newegg_and_bestbuy(self):
        sites = resolve_source_sites("retail")
        assert sites == ["newegg", "bestbuy"]

    def test_vehicles_excludes_retail_computer_sources(self):
        sites = resolve_source_sites("vehicles")
        for src in ("newegg", "bestbuy", "swappa", "microcenter"):
            assert src not in sites
        assert sites == ["craigslist", "carsdotcom", "offerup"]

    def test_tv_no_retail_computer_sources(self):
        sites = resolve_source_sites("tv_home_theater")
        for src in ("newegg", "bestbuy", "swappa", "microcenter", "mercari"):
            assert src not in sites
        assert sites == ["craigslist", "offerup"]

    def test_general_excludes_retail_computer_sources(self):
        sites = resolve_source_sites("general")
        for src in ("newegg", "bestbuy", "swappa", "microcenter"):
            assert src not in sites
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
    def test_gpu_multi_source_includes_retail(self):
        t = translate("rtx 3080 under $400")
        assert "newegg" in t.source_sites
        assert "bestbuy" in t.source_sites
        assert "swappa" in t.source_sites
        assert "microcenter" in t.source_sites

    def test_ryzen_cpu_includes_retail_sources(self):
        t = translate("ryzen 7 7800x3d under $400")
        for src in ("microcenter", "newegg", "bestbuy", "swappa"):
            assert src in t.source_sites

    def test_gaming_laptop_includes_retail_sources(self):
        t = translate("gaming laptop under $800")
        for src in ("microcenter", "newegg", "bestbuy", "swappa"):
            assert src in t.source_sites

    def test_nvme_ssd_includes_retail_sources(self):
        t = translate("2tb nvme ssd under $300")
        for src in ("newegg", "bestbuy", "swappa"):
            assert src in t.source_sites

    def test_vehicle_multi_source_unchanged(self):
        t = translate("toyota sequoia under 50k miles under $30k")
        for src in ("newegg", "bestbuy", "swappa", "microcenter"):
            assert src not in t.source_sites
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
