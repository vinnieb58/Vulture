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

    def test_pc_components_alias_matches_computer_parts(self):
        assert resolve_source_sites("pc_components") == resolve_source_sites(
            "computer_parts"
        )

    def test_gaming_vertical_runtime_sources(self):
        assert resolve_source_sites("gaming") == [
            "craigslist", "mercari", "offerup"
        ]

    def test_electronics_vertical_runtime_sources(self):
        assert resolve_source_sites("electronics") == [
            "craigslist", "mercari", "offerup"
        ]

    def test_phones_tablets_no_mercari(self):
        assert resolve_source_sites("phones_tablets") == ["craigslist", "offerup"]

    def test_retail_defaults_to_craigslist_only(self):
        assert resolve_source_sites("retail") == ["craigslist"]


class TestCandidateMappings:
    def test_computer_parts_candidates_include_retail_and_swappa(self):
        candidates = resolve_candidate_sources("computer_parts")
        assert "swappa" in candidates
        assert "bestbuy" in candidates
        assert "microcenter" in candidates

    def test_retail_candidates_are_probe_only_stores(self):
        assert resolve_candidate_sources("retail") == ["bestbuy", "microcenter"]

    def test_phones_tablets_candidates_include_swappa(self):
        candidates = resolve_candidate_sources("phones_tablets")
        assert "swappa" in candidates
        assert "bestbuy" not in candidates

    def test_probe_sources_not_executable_defaults(self):
        for vertical in ("computer_parts", "gaming", "electronics", "retail"):
            runtime = resolve_source_sites(vertical)
            assert "bestbuy" not in runtime
            assert "microcenter" not in runtime
            assert "swappa" not in runtime

    def test_probe_metadata_marked_probe_only(self):
        for name in ("bestbuy", "microcenter"):
            caps = get_probe_capabilities(name)
            assert caps is not None
            assert caps["probe_only"] is True
            assert caps["stable"] is False
            assert is_registered_source(name) is False

    def test_swappa_not_stable_in_metadata(self):
        caps = get_source_metadata("swappa")
        assert caps is not None
        assert caps["stable"] is False
        assert caps.get("probe_only") is True
        assert is_registered_source("swappa") is False

    def test_list_probe_sources(self):
        assert set(list_probe_sources()) == {"bestbuy", "microcenter", "swappa"}

    def test_is_executable_source_registered_only(self):
        assert is_executable_source("craigslist") is True
        assert is_executable_source("bestbuy") is False


class TestTranslatorIntegration:
    def test_gpu_multi_source_by_default(self):
        t = translate("rtx 3080 under $400")
        assert t.source_sites == ["craigslist", "mercari", "offerup"]

    def test_vehicle_multi_source(self):
        t = translate("toyota sequoia under 50k miles under $30k")
        assert t.source_sites == ["craigslist", "carsdotcom", "offerup"]

    def test_tv_home_theater_unchanged(self):
        t = translate("75 inch 4K TV under $500")
        assert t.source_sites == ["craigslist", "offerup"]

    def test_explicit_swappa_filtered_when_unregistered(self):
        sites = resolve_source_sites(
            "computer_parts",
            explicit_sources=["craigslist", "swappa"],
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
