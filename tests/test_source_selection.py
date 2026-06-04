"""
tests/test_source_selection.py

Vertical source selection (production profiles for personal deployment).
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from adapters.registry import get_source_metadata, is_registered_source, list_probe_sources
from engine.llm_translator import translate
from engine.source_selection import (
    is_executable_source,
    resolve_candidate_sources,
    resolve_source_sites,
)


def _expand_hunt_sources(hunt: dict) -> list[dict]:
    """Mirror main._expand_hunt_sources for fan-out regression."""
    source_sites = hunt.get("source_sites")
    if not source_sites or len(source_sites) <= 1:
        return [hunt]
    return [{**hunt, "source": site} for site in source_sites]


_COMPUTER_PARTS_DEFAULTS = [
    "craigslist",
    "mercari",
    "offerup",
    "microcenter",
    "swappa",
    "bestbuy",
    "newegg",
]

_GAMING_DEFAULTS = [
    "craigslist",
    "mercari",
    "offerup",
    "swappa",
    "bestbuy",
    "newegg",
]


class TestVerticalProfiles:
    def test_computer_parts_includes_retail_and_swappa(self):
        assert resolve_source_sites("computer_parts") == _COMPUTER_PARTS_DEFAULTS

    def test_laptops_computers_includes_retail_and_swappa(self):
        sites = resolve_source_sites("laptops_computers")
        assert sites == ["mercari", "microcenter", "swappa", "bestbuy", "newegg"]

    def test_gaming_includes_swappa_bestbuy_newegg(self):
        assert resolve_source_sites("gaming") == _GAMING_DEFAULTS

    def test_electronics_includes_all_computer_retail(self):
        assert resolve_source_sites("electronics") == _COMPUTER_PARTS_DEFAULTS

    def test_phones_tablets_includes_swappa(self):
        assert resolve_source_sites("phones_tablets") == [
            "craigslist", "offerup", "swappa",
        ]

    def test_retail_includes_all_retail_adapters(self):
        assert resolve_source_sites("retail") == [
            "bestbuy", "microcenter", "newegg",
        ]

    def test_vehicles_profile_unchanged(self):
        sites = resolve_source_sites("vehicles")
        assert "microcenter" not in sites
        assert "swappa" not in sites
        assert "bestbuy" not in sites
        assert "newegg" not in sites
        assert sites == ["craigslist", "carsdotcom", "offerup"]

    def test_tv_no_retail_sources(self):
        sites = resolve_source_sites("tv_home_theater")
        assert sites == ["craigslist", "offerup"]
        assert "microcenter" not in sites
        assert "swappa" not in sites

    def test_general_includes_mercari_not_retail(self):
        sites = resolve_source_sites("general")
        assert sites == ["craigslist", "offerup", "mercari"]
        assert "bestbuy" not in sites

    def test_pc_components_alias_matches_computer_parts(self):
        assert resolve_source_sites("pc_components") == resolve_source_sites(
            "computer_parts"
        )

    def test_explicit_sources_override(self):
        sites = resolve_source_sites(
            "general",
            explicit_sources=["craigslist", "offerup"],
        )
        assert sites == ["craigslist", "offerup"]


class TestCandidateMappings:
    def test_candidates_match_runtime_profiles(self):
        for vertical in (
            "computer_parts",
            "gaming",
            "electronics",
            "retail",
            "phones_tablets",
        ):
            assert resolve_candidate_sources(vertical) == resolve_source_sites(vertical)

    def test_retail_adapters_registered(self):
        for name in ("swappa", "bestbuy", "newegg", "microcenter"):
            assert is_registered_source(name) is True
            assert is_executable_source(name) is True

    def test_experimental_sources_not_marked_stable(self):
        for name in ("swappa", "bestbuy", "newegg"):
            caps = get_source_metadata(name)
            assert caps is not None
            assert caps["stable"] is False
            assert caps["experimental"] is True

    def test_list_probe_sources_empty(self):
        assert list_probe_sources() == []


class TestTranslatorIntegration:
    def test_gpu_includes_swappa_bestbuy_newegg(self):
        t = translate("rtx 3080 under $400")
        assert t.source_sites == _COMPUTER_PARTS_DEFAULTS

    def test_m2_storage_same_sources_as_ddr4(self):
        ddr4 = translate("32GB DDR4 RAM 3200mhz")
        m2 = translate("M.2 SATA SSD 256GB under $50")
        assert m2.source_sites == ddr4.source_sites == _COMPUTER_PARTS_DEFAULTS

    def test_gaming_laptop_includes_retail_sources(self):
        t = translate("gaming laptop under $800")
        for source in ("microcenter", "swappa", "bestbuy", "newegg"):
            assert source in t.source_sites

    def test_vehicle_multi_source(self):
        t = translate("toyota sequoia under 50k miles under $30k")
        assert t.source_sites == ["craigslist", "carsdotcom", "offerup"]

    def test_tv_home_theater_unchanged(self):
        t = translate("75 inch 4K TV under $500")
        assert t.source_sites == ["craigslist", "offerup"]


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
