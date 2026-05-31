"""
tests/test_source_selection.py

Vertical source selection (production profiles for personal deployment).
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from adapters.registry import (
    get_probe_capabilities,
    get_source_metadata,
    is_registered_source,
    list_probe_sources,
)
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

    def test_pc_components_alias_matches_computer_parts(self):
        assert resolve_source_sites("pc_components") == resolve_source_sites(
            "computer_parts"
        )

    def test_gaming_vertical_runtime_sources(self):
        assert resolve_source_sites("gaming") == [
            "craigslist", "mercari", "offerup",
        ]

    def test_electronics_vertical_runtime_sources(self):
        assert resolve_source_sites("electronics") == [
            "craigslist", "mercari", "offerup",
        ]

    def test_phones_tablets_no_mercari(self):
        assert resolve_source_sites("phones_tablets") == ["craigslist", "offerup"]

    def test_retail_includes_microcenter(self):
        assert resolve_source_sites("retail") == ["microcenter"]


class TestCandidateMappings:
    def test_computer_parts_candidates_include_retail_probe_and_runtime(self):
        candidates = resolve_candidate_sources("computer_parts")
        assert "swappa" in candidates
        assert "bestbuy" in candidates
        assert "newegg" in candidates
        assert "microcenter" in candidates

    def test_retail_candidates_include_probe_stores(self):
        assert resolve_candidate_sources("retail") == [
            "bestbuy", "microcenter", "newegg",
        ]

    def test_phones_tablets_candidates_include_swappa(self):
        candidates = resolve_candidate_sources("phones_tablets")
        assert "swappa" in candidates
        assert "bestbuy" not in candidates

    def test_probe_only_sources_not_in_runtime_defaults(self):
        for vertical in ("computer_parts", "gaming", "electronics"):
            runtime = resolve_source_sites(vertical)
            assert "bestbuy" not in runtime
            assert "newegg" not in runtime
            assert "swappa" not in runtime

    def test_microcenter_in_computer_parts_runtime_defaults(self):
        assert "microcenter" in resolve_source_sites("computer_parts")

    def test_probe_metadata_marked_probe_only(self):
        for name in ("bestbuy", "newegg"):
            caps = get_probe_capabilities(name)
            assert caps is not None
            assert caps["probe_only"] is True
            assert caps["stable"] is False
            assert is_registered_source(name) is False

    def test_swappa_registered_experimental_not_stable(self):
        caps = get_source_metadata("swappa")
        assert caps is not None
        assert caps["stable"] is False
        assert caps["experimental"] is True
        assert is_registered_source("swappa") is True
        assert get_probe_capabilities("swappa") is None

    def test_microcenter_registered_not_probe_only(self):
        caps = get_source_metadata("microcenter")
        assert caps is not None
        assert caps["stable"] is True
        assert is_registered_source("microcenter") is True
        assert get_probe_capabilities("microcenter") is None

    def test_list_probe_sources(self):
        assert set(list_probe_sources()) == {"bestbuy", "newegg"}

    def test_is_executable_source_registered_only(self):
        assert is_executable_source("craigslist") is True
        assert is_executable_source("microcenter") is True
        assert is_executable_source("swappa") is True
        assert is_executable_source("bestbuy") is False
        assert is_executable_source("newegg") is False


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

    def test_tv_home_theater_unchanged(self):
        t = translate("75 inch 4K TV under $500")
        assert t.source_sites == ["craigslist", "offerup"]

    def test_swappa_not_in_translated_defaults(self):
        t = translate("rtx 3080 under $400")
        assert "swappa" not in t.source_sites

    def test_explicit_swappa_allowed_when_registered(self):
        sites = resolve_source_sites(
            "computer_parts",
            explicit_sources=["craigslist", "swappa"],
        )
        assert sites == ["craigslist", "swappa"]

    def test_explicit_newegg_filtered_when_unregistered(self):
        sites = resolve_source_sites(
            "computer_parts",
            explicit_sources=["craigslist", "newegg"],
        )
        assert sites == ["craigslist"]


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
