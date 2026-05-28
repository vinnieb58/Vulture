"""
tests/test_source_selection.py

Source selection and experimental-adapter gating.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from engine.llm_translator import translate
from engine.source_selection import (
    experimental_adapters_enabled,
    resolve_source_sites,
)
def _expand_hunt_sources(hunt: dict) -> list[dict]:
    """Mirror main._expand_hunt_sources for fan-out regression."""
    source_sites = hunt.get("source_sites")
    if not source_sites or len(source_sites) <= 1:
        return [hunt]
    return [{**hunt, "source": site} for site in source_sites]


@pytest.fixture(autouse=True)
def _clear_experimental_env(monkeypatch):
    monkeypatch.delenv("VULTURE_ENABLE_EXPERIMENTAL_ADAPTERS", raising=False)


class TestExperimentalGating:
    def test_default_is_craigslist_only(self):
        assert resolve_source_sites("computer_parts") == ["craigslist"]
        assert resolve_source_sites("vehicles") == ["craigslist"]

    def test_env_flag_enables_multi_source(self, monkeypatch):
        monkeypatch.setenv("VULTURE_ENABLE_EXPERIMENTAL_ADAPTERS", "true")
        assert experimental_adapters_enabled()
        assert resolve_source_sites("computer_parts") == [
            "craigslist", "mercari", "offerup"
        ]

    def test_carsdotcom_not_on_non_vehicle_verticals(self, monkeypatch):
        monkeypatch.setenv("VULTURE_ENABLE_EXPERIMENTAL_ADAPTERS", "true")
        sites = resolve_source_sites("computer_parts")
        assert "carsdotcom" not in sites

    def test_mercari_not_on_tv_vertical(self, monkeypatch):
        monkeypatch.setenv("VULTURE_ENABLE_EXPERIMENTAL_ADAPTERS", "true")
        sites = resolve_source_sites("tv_home_theater")
        assert sites == ["craigslist", "offerup"]

    def test_explicit_sources_bypass_env(self, monkeypatch):
        monkeypatch.delenv("VULTURE_ENABLE_EXPERIMENTAL_ADAPTERS", raising=False)
        sites = resolve_source_sites(
            "general",
            explicit_sources=["offerup", "mercari"],
        )
        assert sites == ["offerup", "mercari"]


class TestVerticalProfiles:
    @pytest.fixture(autouse=True)
    def _enable_experimental(self, monkeypatch):
        monkeypatch.setenv("VULTURE_ENABLE_EXPERIMENTAL_ADAPTERS", "true")

    def test_vehicles_profile(self):
        assert resolve_source_sites("vehicles") == [
            "craigslist", "carsdotcom", "offerup"
        ]

    def test_general_profile(self):
        assert resolve_source_sites("general") == ["craigslist", "offerup"]


class TestTranslatorIntegration:
    def test_gpu_default_craigslist(self):
        t = translate("rtx 3080 under $400")
        assert t.source_sites == ["craigslist"]

    def test_gpu_experimental_multi_source(self, monkeypatch):
        monkeypatch.setenv("VULTURE_ENABLE_EXPERIMENTAL_ADAPTERS", "true")
        t = translate("rtx 3080 under $400")
        assert t.source_sites == ["craigslist", "mercari", "offerup"]

    def test_vehicle_experimental(self, monkeypatch):
        monkeypatch.setenv("VULTURE_ENABLE_EXPERIMENTAL_ADAPTERS", "true")
        t = translate("toyota sequoia under 50k miles under $30k")
        assert "carsdotcom" in t.source_sites
        assert t.source_sites[0] == "craigslist"


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
